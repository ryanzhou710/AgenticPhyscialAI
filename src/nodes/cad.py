"""SpaceClaim preparation, selection, extraction, and explicit grouping nodes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.config import config_from_state
from src.services import selection
from src.services.contracts import clarification_step
from src.services.execution import (
    HumanInterventionRequired,
    _copy_runtime_evidence,
    _failed,
    _run_dir,
    _succeeded,
)
from src.services.geometry_catalog import GeometryCatalog
from src.services.spaceclaim_runtime import open_spaceclaim_reader
from src.state import PipelineState


def _build_adapter(state: PipelineState) -> SpaceClaimBuildAdapter:
    return SpaceClaimBuildAdapter(
        runtime_dir=state["runtime_dir"],
        ui_mode=state["ui_mode"],
        config=config_from_state(state),
    )


def _clarification(
    state: PipelineState,
    *,
    stage: str,
    message: str,
    evidence: dict[str, Any],
    required_action: str,
    resume_step: str,
) -> HumanInterventionRequired:
    return HumanInterventionRequired(
        {
            "kind": "clarification",
            "failed_step": stage,
            "message": message,
            "evidence": evidence,
            "attempted_repairs": [],
            "required_action": required_action,
            "resume_step": clarification_step(resume_step),
        }
    )


def _catalog_for(state: PipelineState, geometry: Path, artifact_name: str) -> GeometryCatalog:
    with open_spaceclaim_reader(state, _run_dir(state) / "artifacts" / artifact_name) as runner:
        catalog, _ = runner.catalog(geometry, render_candidates=False)
    return catalog


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
                "extraction": {},
                "extraction_catalog": {},
                "target_body": {},
                "target_catalog": {},
                "boundary_group_plan": {},
                "labeling": {},
                "artifacts": {"original_geometry": str(source)},
            },
        )
    except Exception as error:
        return _failed(state, "prepare", error)


def query_geometry(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = _catalog_for(state, Path(state["working_geometry"]), "catalog")
        return _succeeded(state, "query_geometry", {"catalog": catalog.model_dump(mode="json")})
    except Exception as error:
        return _failed(state, "query_geometry", error)


def understand_prompt(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["catalog"])
        with open_spaceclaim_reader(
            state, _run_dir(state) / "artifacts" / "selection-details"
        ) as runner:

            def render_details(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return runner.render_candidate_details(Path(state["working_geometry"]), catalog, requests)

            plan = selection.plan_cad_selection(
                catalog=catalog,
                user_prompt=state["prompt"],
                audit_dir=_run_dir(state) / "llm",
                config=config_from_state(state),
                detail_renderer=render_details,
            )
        if plan.status != "selected":
            raise _clarification(
                state,
                stage="understand_prompt",
                message="CAD intent cannot be resolved without additional information.",
                evidence={
                    "selection_status": plan.status,
                    "explanation": plan.explanation,
                    "missing_information": plan.missing_information,
                    "fluid_domain_action": plan.fluid_domain_action,
                    "fluid_domain_evidence": plan.fluid_domain_evidence,
                },
                required_action="Clarify the extraction objects or the reusable target body.",
                resume_step="understand_prompt",
            )
        return _succeeded(
            state,
            "understand_prompt",
            {"selection_plan": plan.model_dump(mode="json")},
        )
    except Exception as error:
        return _failed(state, "understand_prompt", error)


def verify_selection(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["catalog"])
        plan = state["selection_plan"]
        if plan["fluid_domain_action"] == "reuse":
            ids = [plan["fluid_body_id"]]
        else:
            ids = [value for opening in plan["openings"] for value in opening["object_ids"]]
            ids.append(plan["seed_inner_wall_id"])
        with open_spaceclaim_reader(state, _run_dir(state) / "artifacts" / "selection") as runner:
            execution = runner.select(
                Path(state["working_geometry"]),
                catalog,
                ids,
                views=list(dict.fromkeys([plan["reference_view"], "Isometric"])),
            )
        if not execution.active_selection_verified:
            raise RuntimeError("SpaceClaim did not preserve the exact model-selected native objects")
        return _succeeded(
            state,
            "verify_selection",
            {"native_selection": execution.model_dump(mode="json")},
        )
    except Exception as error:
        return _failed(state, "verify_selection", error)


def extract_volume(state: PipelineState) -> dict[str, Any]:
    try:
        plan = state["selection_plan"]
        source = Path(state["runtime_dir"]) / "original.scdoc"
        catalog = GeometryCatalog.model_validate(state["catalog"])
        if plan["fluid_domain_action"] == "reuse":
            candidate_catalog = catalog
            extraction = {
                "source_mode": "reuse",
                "candidate_geometry": str(source),
                "candidate_body_monikers": [catalog.by_id()[plan["fluid_body_id"]].moniker],
            }
        else:
            output = Path(state["runtime_dir"]) / "extraction-candidates.scdoc"
            result = _build_adapter(state).extract_volume(
                source=source,
                output=output,
                catalog=catalog.native_catalog,
                selection_plan=plan,
            )
            candidate_catalog = _catalog_for(state, output, "extraction-candidates")
            extraction = {
                **result,
                "candidate_geometry": str(output),
                "candidate_body_monikers": [
                    row["moniker"] for row in result["transfer"]["candidate_bodies"]
                ],
            }
        return _succeeded(
            state,
            "extract_volume",
            {
                "extraction": extraction,
                "extraction_catalog": candidate_catalog.model_dump(mode="json"),
                "target_body": {},
                "target_catalog": {},
                "boundary_group_plan": {},
                "labeling": {},
            },
        )
    except Exception as error:
        return _failed(state, "extract_volume", error)


def select_fluid_body(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["extraction_catalog"])
        plan = state["selection_plan"]
        candidate_monikers = set(state["extraction"].get("candidate_body_monikers", []))
        allowed_ids = {
            body.id for body in catalog.bodies if body.moniker in candidate_monikers
        }
        if not allowed_ids:
            raise RuntimeError("The extraction candidate catalog has no bodies matching the native result")
        def render_details(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
            with open_spaceclaim_reader(
                state, _run_dir(state) / "artifacts" / "target-body-details"
            ) as runner:
                return runner.render_candidate_details(
                    Path(state["extraction"]["candidate_geometry"]), catalog, requests
                )

        choice = selection.select_fluid_body(
            catalog=catalog,
            user_prompt=state["prompt"],
            selection_plan=plan,
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
            candidate_body_ids=allowed_ids,
            detail_renderer=render_details,
        )
        if choice.status != "selected":
            raise _clarification(
                state,
                stage="select_fluid_body",
                message="The target fluid body cannot be identified uniquely.",
                evidence={
                    "selection_status": choice.status,
                    "explanation": choice.explanation,
                    "missing_information": choice.missing_information,
                },
                required_action="Clarify which positive-volume body is the intended fluid domain.",
                resume_step="select_fluid_body",
            )
        output = Path(state["runtime_dir"]) / "target-fluid.scdoc"
        isolated = _build_adapter(state).isolate_body(
            source=state["extraction"]["candidate_geometry"],
            output=output,
            catalog=catalog.native_catalog,
            target_body_id=str(choice.body_id),
        )
        target_catalog = _catalog_for(state, output, "target-catalog")
        target_moniker = isolated["target_body"]["moniker"]
        target = next(body for body in target_catalog.bodies if body.moniker == target_moniker)
        return _succeeded(
            state,
            "select_fluid_body",
            {
                "working_geometry": str(output),
                "target_body": {
                    "body_id": target.id,
                    "moniker": target.moniker,
                    "volume_m3": target.volume_m3,
                    "selection_reason": choice.explanation,
                },
                "target_catalog": target_catalog.model_dump(mode="json"),
                "boundary_group_plan": {},
                "labeling": {},
            },
        )
    except Exception as error:
        return _failed(state, "select_fluid_body", error)


def plan_boundary_groups(state: PipelineState) -> dict[str, Any]:
    try:
        catalog = GeometryCatalog.model_validate(state["target_catalog"])
        target_id = state["target_body"]["body_id"]
        def render_details(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
            with open_spaceclaim_reader(
                state, _run_dir(state) / "artifacts" / "boundary-details"
            ) as runner:
                return runner.render_candidate_details(Path(state["working_geometry"]), catalog, requests)

        groups = selection.plan_boundary_groups(
            catalog=catalog,
            target_body_id=target_id,
            user_prompt=state["prompt"],
            selection_plan=state["selection_plan"],
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
            detail_renderer=render_details,
        )
        if groups.status != "selected":
            raise _clarification(
                state,
                stage="plan_boundary_groups",
                message="The target boundary groups cannot be identified unambiguously.",
                evidence={
                    "selection_status": groups.status,
                    "explanation": groups.explanation,
                    "missing_information": groups.missing_information,
                },
                required_action="Clarify the boundary groups on the isolated target fluid body.",
                resume_step="plan_boundary_groups",
            )
        requirements = selection.extract_mesh_requirements(
            catalog=catalog,
            user_prompt=state["prompt"],
            boundary_names=[group.name for group in groups.groups],
            audit_dir=_run_dir(state) / "llm",
            config=config_from_state(state),
        )
        if requirements.missing_information or requirements.unsupported_requirements:
            raise _clarification(
                state,
                stage="plan_boundary_groups",
                message="Meshing requirements need clarification or exceed supported capabilities.",
                evidence={
                    "missing_information": requirements.missing_information,
                    "unsupported_requirements": requirements.unsupported_requirements,
                },
                required_action="Clarify units or revise unsupported meshing requirements.",
                resume_step="plan_boundary_groups",
            )
        return _succeeded(
            state,
            "plan_boundary_groups",
            {
                "boundary_group_plan": groups.model_dump(mode="json"),
                "parsed_mesh_requirements": requirements.model_dump(mode="json"),
                "mesh_requirements": requirements.model_dump(mode="json"),
            },
        )
    except Exception as error:
        return _failed(state, "plan_boundary_groups", error)


def label_faces(state: PipelineState) -> dict[str, Any]:
    try:
        output = Path(state["runtime_dir"]) / "labeled.scdoc"
        catalog = GeometryCatalog.model_validate(state["target_catalog"])
        groups = state["boundary_group_plan"]["groups"]
        result = _build_adapter(state).label_faces(
            source=Path(state["runtime_dir"]) / "target-fluid.scdoc",
            output=output,
            catalog=catalog.native_catalog,
            target_body_id=state["target_body"]["body_id"],
            groups=groups,
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
        target = state["target_body"]
        labeling = state["labeling"]
        checks = {
            "positive_volume": (target.get("volume_m3") or 0.0) > 0.0,
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
