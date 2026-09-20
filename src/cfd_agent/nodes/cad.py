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
from cfd_agent.services.geometry_models import GeometryCatalog, is_closed_single_solid
from cfd_agent.services.grounding import extract_mesh_requirements, plan_cad_selection
from cfd_agent.state import PipelineState


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
                candidate_collections=["faces", "edges"],
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
        direct = is_closed_single_solid(catalog)
        adapter = SpaceClaimBuildAdapter(
            runtime_dir=state["runtime_dir"],
            ui_mode=state["ui_mode"],
            config=config_from_state(state),
        )
        if direct:
            result = adapter.use_existing_fluid(
                source=state["working_geometry"],
                output=output,
                catalog=catalog.native_catalog,
                selection_plan=state["selection_plan"],
            )
            mode = "existing_solid"
        else:
            result = adapter.extract_volume(
                source=state["working_geometry"],
                output=output,
                catalog=catalog.native_catalog,
                selection_plan=state["selection_plan"],
            )
            mode = "volume_extract"
        return _persist(
            state,
            "extract_volume",
            {
                "extraction": result,
                "fluid_domain_mode": mode,
                "working_geometry": str(output),
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
