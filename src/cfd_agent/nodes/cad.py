"""SpaceClaim preparation, grounding and modeling nodes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from cfd_agent.adapters.spaceclaim import SpaceClaimRunner
from cfd_agent.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from cfd_agent.config import config_from_state
from cfd_agent.services.execution import _copy_runtime_evidence, _failed, _persist, _run_dir
from cfd_agent.services.geometry_models import GeometryCatalog
from cfd_agent.services.grounding import extract_mesh_requirements, plan_cad_selection
from cfd_agent.state import PipelineState


def _is_existing_fluid_body(catalog: GeometryCatalog) -> bool:
    """Return whether the input already contains one closed positive-volume body.

    A closed solid body can be sent directly to boundary grouping and meshing;
    sheet bodies or bodies with free edges still need volume extraction.
    """
    if len(catalog.bodies) != 1:
        return False
    body = catalog.bodies[0]
    if body.solid_or_sheet != "solid" or (body.volume_m3 or 0.0) <= 0.0:
        return False
    edges = {edge.id: edge for edge in catalog.edges}
    return all(
        edge_id in edges and len(edges[edge_id].face_ids) == 2
        for edge_id in body.edge_ids
    )


def prepare(state: PipelineState) -> dict[str, Any]:
    try:
        source = Path(state["source_geometry"]).resolve()
        working = Path(state["runtime_dir"]) / "original.scdoc"
        shutil.copy2(source, working)
        return _persist(
            state,
            "prepare",
            {
                "working_geometry": str(working),
                "status": "running",
                "repair_rounds": state.get("repair_rounds", 0),
                "repair_history": list(state.get("repair_history", [])),
                "fluent_steps": {},
                "artifacts": {"original_geometry": str(source)},
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "prepare", error)


def query_geometry(state: PipelineState) -> dict[str, Any]:
    try:
        runner = SpaceClaimRunner(
            output_dir=_run_dir(state) / "artifacts" / "catalog",
            ui_mode=state["ui_mode"],
            config=config_from_state(state),
        )
        try:
            catalog, path = runner.catalog(
                Path(state["working_geometry"]),
                render_candidates=True,
                # Loops expose arbitrary multi-edge opening contours (rectangles,
                # polygons, splines) that cannot be represented by one edge.
                candidate_collections=["faces", "edges", "loops"],
            )
        finally:
            runner.close()
        return _persist(
            state,
            "query_geometry",
            {
                "catalog": catalog.model_dump(mode="json"),
                "catalog_path": str(path),
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "query_geometry", error)


def understand_prompt(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["catalog"])
        plan = plan_cad_selection(
            catalog=catalog,
            user_prompt=state["prompt"],
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
        )
        requirements = extract_mesh_requirements(
            catalog=catalog,
            user_prompt=state["prompt"],
            selection_plan=plan,
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
        )
        return _persist(
            state,
            "understand_prompt",
            {
                "selection_plan": plan.model_dump(mode="json"),
                "mesh_requirements": requirements.model_dump(mode="json"),
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "understand_prompt", error)


def verify_selection(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["catalog"])
        plan = state["selection_plan"]
        ids = [item["candidate_id"] for item in plan["openings"]]
        ids.append(plan["seed_inner_wall_id"])
        runner = SpaceClaimRunner(
            output_dir=_run_dir(state) / "artifacts" / "selection",
            ui_mode=state["ui_mode"],
            config=config_from_state(state),
        )
        try:
            execution = runner.select(
                Path(state["working_geometry"]),
                catalog,
                ids,
                views=list(dict.fromkeys([plan["reference_view"], "Isometric"])),
            )
        finally:
            runner.close()
        if not execution.active_selection_verified:
            raise RuntimeError(
                "SpaceClaim did not preserve the exact model-selected native objects"
            )
        return _persist(
            state,
            "verify_selection",
            {
                "native_selection": execution.model_dump(mode="json"),
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "verify_selection", error)


def extract_volume(state: PipelineState) -> dict[str, Any]:
    try:
        output = Path(state["runtime_dir"]) / "extracted.scdoc"
        catalog = GeometryCatalog.model_validate(state["catalog"])
        # The model selects boundary candidates, but it cannot decide whether
        # the input is a reusable fluid body.  That decision is based on the
        # SpaceClaim topology catalog: one solid, positive volume, no free edge.
        existing_fluid_body = _is_existing_fluid_body(catalog)
        adapter = SpaceClaimBuildAdapter(
            runtime_dir=state["runtime_dir"],
            ui_mode=state["ui_mode"],
            config=config_from_state(state),
        )
        result = adapter.extract_volume(
            source=state["working_geometry"],
            output=output,
            catalog=catalog.native_catalog,
            selection_plan=state["selection_plan"],
            existing_fluid_body=existing_fluid_body,
        )
        return _persist(
            state,
            "extract_volume",
            {
                "extraction": result,
                "working_geometry": str(output),
                "fluid_volume_mode": result.get("transfer", {}).get(
                    "source_mode", result.get("transfer", {}).get("mode", "extracted")
                ),
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "extract_volume", error)


def label_faces(state: PipelineState) -> dict[str, Any]:
    try:
        output = Path(state["runtime_dir"]) / "labeled.scdoc"
        adapter = SpaceClaimBuildAdapter(
            runtime_dir=state["runtime_dir"],
            ui_mode=state["ui_mode"],
            config=config_from_state(state),
        )
        result = adapter.label_faces(
            source=state["working_geometry"],
            output=output,
            extraction=state["extraction"],
            keep_open=bool(state["ui_mode"] == "gui"),
        )
        roles = {item["name"]: item["role"] for item in result["groups"]}
        return _persist(
            state,
            "label_faces",
            {
                "labeling": result,
                "working_geometry": str(output),
                "boundary_roles": roles,
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "label_faces", error)


def validate_cad(state: PipelineState) -> dict[str, Any]:
    try:
        extraction = state["extraction"]["transfer"]
        labeling = state["labeling"]
        checks = {
            "one_positive_volume": extraction["volume_m3"] > 0,
            "closed_topology": not extraction["free_edges"],
            "all_faces_grouped": labeling["coverage"] == labeling["total_faces"],
            "group_count": len(labeling["groups"]),
        }
        if not all(value for key, value in checks.items() if key != "group_count"):
            raise RuntimeError("SpaceClaim CAD validation failed: " + json.dumps(checks))
        evidence = _copy_runtime_evidence(state, "spaceclaim-build")
        return _persist(
            state,
            "validate_cad",
            {
                "cad_validation": checks,
                "artifacts": {
                    **state["artifacts"],
                    **{"spaceclaim:" + name: path for name, path in evidence.items()},
                },
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "validate_cad", error)
