"""SpaceClaim preparation, grounding and modeling nodes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.config import config_from_state
from src.services.execution import (
    HumanInterventionRequired,
    _copy_runtime_evidence,
    _failed,
    _run_dir,
    _succeeded,
)
from src.services.geometry_catalog import GeometryCatalog
from src.services.selection import extract_mesh_requirements, plan_cad_selection
from src.services.spaceclaim_runtime import open_spaceclaim_reader
from src.state import PipelineState


def _build_adapter(state: PipelineState) -> SpaceClaimBuildAdapter:
    return SpaceClaimBuildAdapter(
        runtime_dir=state["runtime_dir"],
        ui_mode=state["ui_mode"],
        config=config_from_state(state),
    )


def prepare(state: PipelineState) -> dict[str, Any]:
    try:
        source = Path(state["source_geometry"]).resolve()
        working = Path(state["runtime_dir"]) / "original.scdoc"
        shutil.copy2(source, working)
        return _succeeded(
            state,
            "prepare",
            {
                "working_geometry": str(working),
                "status": "running",
                "repair_rounds": state.get("repair_rounds", 0),
                "repair_history": list(state.get("repair_history", [])),
                "fluent_steps": {},
                "artifacts": {"original_geometry": str(source)},
            },
        )
    except Exception as error:
        return _failed(state, "prepare", error)


def query_geometry(state: PipelineState) -> dict[str, Any]:
    try:
        with open_spaceclaim_reader(state, _run_dir(state) / "artifacts" / "catalog") as runner:
            catalog, _ = runner.catalog(
                Path(state["working_geometry"]),
                render_candidates=False,
            )
        return _succeeded(
            state,
            "query_geometry",
            {
                "catalog": catalog.model_dump(mode="json"),
            },
        )
    except Exception as error:
        return _failed(state, "query_geometry", error)


def understand_prompt(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["catalog"])
        with open_spaceclaim_reader(
            state, _run_dir(state) / "artifacts" / "selection-details"
        ) as runner:
            def render_details(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return runner.render_candidate_details(
                    Path(state["working_geometry"]), catalog, requests
                )

            plan = plan_cad_selection(
                catalog=catalog,
                user_prompt=state["prompt"],
                audit_dir=_run_dir(state) / "llm",
                config=config_from_state(state),
                detail_renderer=render_details,
            )
        if plan.status != "selected" or plan.fluid_domain_action == "ambiguous":
            raise HumanInterventionRequired(
                {
                    "kind": "clarification",
                    "failed_step": "understand_prompt",
                    "message": "CAD intent cannot be resolved without additional information.",
                    "evidence": {
                        "selection_status": plan.status,
                        "explanation": plan.explanation,
                        "missing_information": plan.missing_information,
                        "fluid_domain_action": plan.fluid_domain_action,
                        "fluid_domain_evidence": plan.fluid_domain_evidence,
                    },
                    "attempted_repairs": [],
                    "required_action": "Clarify the openings, seed face, or whether the input is already the fluid domain.",
                }
            )
        requirements = extract_mesh_requirements(
            catalog=catalog,
            user_prompt=state["prompt"],
            selection_plan=plan,
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
        )
        if requirements.missing_information or requirements.unsupported_requirements:
            raise HumanInterventionRequired({
                "kind": "clarification",
                "failed_step": "understand_prompt",
                "message": "Meshing requirements need clarification or exceed supported capabilities.",
                "evidence": {
                    "missing_information": requirements.missing_information,
                    "unsupported_requirements": requirements.unsupported_requirements,
                },
                "attempted_repairs": [],
                "required_action": "Clarify units or revise the unsupported meshing requirements.",
            })
        return _succeeded(
            state,
            "understand_prompt",
            {
                "selection_plan": plan.model_dump(mode="json"),
                "parsed_mesh_requirements": requirements.model_dump(mode="json"),
                "mesh_requirements": requirements.model_dump(mode="json"),
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
        with open_spaceclaim_reader(state, _run_dir(state) / "artifacts" / "selection") as runner:
            execution = runner.select(
                Path(state["working_geometry"]),
                catalog,
                ids,
                views=list(dict.fromkeys([plan["reference_view"], "Isometric"])),
            )
        if not execution.active_selection_verified:
            raise RuntimeError(
                "SpaceClaim did not preserve the exact model-selected native objects"
            )
        return _succeeded(
            state,
            "verify_selection",
            {
                "native_selection": execution.model_dump(mode="json"),
            },
        )
    except Exception as error:
        return _failed(state, "verify_selection", error)


def extract_volume(state: PipelineState) -> dict[str, Any]:
    try:
        output = Path(state["runtime_dir"]) / "extracted.scdoc"
        catalog = GeometryCatalog.model_validate(state["catalog"])
        existing_fluid_body = state["selection_plan"].get("fluid_domain_action") == "reuse"
        adapter = _build_adapter(state)
        result = adapter.extract_volume(
            source=state["working_geometry"],
            output=output,
            catalog=catalog.native_catalog,
            selection_plan=state["selection_plan"],
            existing_fluid_body=existing_fluid_body,
        )
        return _succeeded(
            state,
            "extract_volume",
            {
                "extraction": result,
                "working_geometry": str(output),
            },
        )
    except Exception as error:
        return _failed(state, "extract_volume", error)


def label_faces(state: PipelineState) -> dict[str, Any]:
    try:
        output = Path(state["runtime_dir"]) / "labeled.scdoc"
        adapter = _build_adapter(state)
        result = adapter.label_faces(
            source=state["working_geometry"],
            output=output,
            extraction=state["extraction"],
            keep_editor_open=bool(state["ui_mode"] == "gui"),
        )
        roles = {item["name"]: item["role"] for item in result["groups"]}
        return _succeeded(
            state,
            "label_faces",
            {
                "labeling": result,
                "working_geometry": str(output),
                "boundary_roles": roles,
            },
        )
    except Exception as error:
        return _failed(state, "label_faces", error)


def validate_cad(state: PipelineState) -> dict[str, Any]:
    try:
        extraction = state["extraction"]["transfer"]
        labeling = state["labeling"]
        checks = {
            "positive_volume": extraction["volume_m3"] > 0,
            "no_reported_free_edges": not extraction["free_edges"],
            "all_faces_grouped": labeling["coverage"] == labeling["total_faces"],
            "group_count": len(labeling["groups"]),
        }
        if not all(value for key, value in checks.items() if key != "group_count"):
            raise RuntimeError("SpaceClaim CAD validation failed: " + json.dumps(checks))
        evidence = _copy_runtime_evidence(state, "spaceclaim-build")
        return _succeeded(
            state,
            "validate_cad",
            {
                "cad_validation": checks,
                "artifacts": {
                    **state["artifacts"],
                    **{"spaceclaim:" + name: path for name, path in evidence.items()},
                },
            },
        )
    except Exception as error:
        return _failed(state, "validate_cad", error)
