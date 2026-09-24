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
    """Return whether topology permits reuse of one declared fluid body."""
    if len(catalog.bodies) != 1:
        return False
    body = catalog.bodies[0]
    if body.solid_or_sheet != "solid" or (body.volume_m3 or 0.0) <= 0.0:
        return False
    edges = [edge for edge in catalog.edges if edge.body_id == body.id]
    return bool(edges) and all(len(edge.face_ids) == 2 for edge in edges)


def _prompt_explicitly_declares_fluid_body(prompt: str) -> bool:
    """Return true only when the user explicitly identifies the CAD as fluid."""

    text = " ".join(prompt.casefold().split())
    phrases = (
        "already the fluid domain",
        "already a fluid domain",
        "existing fluid domain",
        "existing fluid body",
        "input is the fluid domain",
        "input is a fluid domain",
        "input is already fluid",
        "geometry is already fluid",
        "输入就是流体域",
        "输入为流体域",
        "已有流体域",
        "已经是流体域",
        "无需体积抽取",
        "跳过体积抽取",
        "skip volume extract",
        "skip volume extraction",
    )
    return any(phrase in text for phrase in phrases)


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
        declared_fluid_body = _prompt_explicitly_declares_fluid_body(state.get("prompt", ""))
        reusable_fluid_body = _is_existing_fluid_body(catalog)
        if declared_fluid_body and not reusable_fluid_body:
            raise ValueError(
                "Prompt declares that the CAD is already a fluid domain, but the topology "
                "is not one closed positive-volume solid body without free edges"
            )
        # A closed body only proves that it can be reused. User intent decides
        # whether it represents fluid; otherwise Volume Extract remains the
        # default path.
        existing_fluid_body = declared_fluid_body and reusable_fluid_body
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
