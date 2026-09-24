"""Runtime failure diagnosis and declared run-data tool application."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.adapters.fluent import close_client, get_client
from src.adapters.llm import GroundingLLMClient
from src.config import config_from_state
from src.services.contracts import RepairDecision, repair_tool_catalog
from src.services.execution import _copy_runtime_evidence, _persist, _run_dir
from src.services.geometry_models import GeometryCatalog
from src.services.prompts import load_prompt
from src.state import PipelineState
from src.workers.repair_protocol import STEP_ORDER

FLUENT_STEPS = STEP_ORDER[:-1]
CAD_STEPS = (
    "prepare",
    "query_geometry",
    "understand_prompt",
    "verify_selection",
    "extract_volume",
    "label_faces",
    "validate_cad",
)


def _repair_resume_step(state: PipelineState, decision: RepairDecision) -> str:
    """Validate model routing before any repair can mutate run or native state."""
    action, target = decision.action, decision.target_step
    failed = state.get("failed_step")
    if action == "stop":
        return "failed"
    if state.get("error_evidence", {}).get("session_lost"):
        raise ValueError("A lost Fluent session cannot be repaired")
    if action == "retry_step":
        if target != failed or failed not in (
            *CAD_STEPS,
            "reload_confirmed_cad",
            "launch_fluent",
            *STEP_ORDER,
        ):
            raise ValueError("retry_step must repeat the current failed operation")
        return target
    if action == "replace_object_reference":
        if (
            failed not in CAD_STEPS[3:]
            or target not in CAD_STEPS[3:]
            or CAD_STEPS.index(target) > CAD_STEPS.index(failed)
            or state.get("confirmed_geometry")
            or state.get("human_response", {}).get("action") == "approve"
        ):
            raise ValueError("Object references can only be repaired before CAD confirmation")
        return "verify_selection"
    if action == "return_to_human":
        request = state.get("human_request", {})
        if request.get("kind") in {"clarification", "locked_parameter", "boundary_mapping"}:
            return "human_intervention"
        if (
            failed not in ("reload_confirmed_cad", "launch_fluent", *STEP_ORDER)
            or target not in {failed, "human_confirmation"}
            or not state.get("working_geometry")
            or not state.get("labeling")
        ):
            raise ValueError("Return to human requires a concrete pending intervention")
        return "human_confirmation"
    resume = {
        "set_global_size": "surface_mesh",
        "set_local_size": "local_sizing",
        "set_growth_rate": "boundary_layers",
        "set_layer_count": "boundary_layers",
        "set_first_layer_height": "boundary_layers",
        "set_layer_targets": "boundary_layers",
        "enable_quality_improvement": "surface_mesh",
    }.get(action)
    if action == "replace_zone_reference":
        category = decision.parameters["category"]
        resume = (
            "update_boundaries"
            if category.startswith("boundaries.")
            else "local_sizing"
            if category == "local_refinements"
            else "boundary_layers"
        )
    if (
        failed not in STEP_ORDER
        or resume not in STEP_ORDER
        or STEP_ORDER.index(resume) > STEP_ORDER.index(failed)
        or target not in {failed, resume}
    ):
        raise ValueError("Fluent repair must resume at an affected step no later than the failure")
    return resume


@dataclass
class RepairOutcome:
    goto: str
    update: dict[str, Any] = field(default_factory=dict)


def _human_request(
    state: PipelineState,
    *,
    kind: str,
    message: str,
    required_action: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "failed_step": state.get("failed_step"),
        "message": message,
        "evidence": evidence,
        "attempted_repairs": list(state.get("repair_history", [])),
        "required_action": required_action,
    }


def _locked_control(state: PipelineState, decision: RepairDecision) -> bool:
    requirements = state.get("mesh_requirements", {})
    layers = requirements.get("boundary_layers") or {}
    if decision.action == "set_global_size":
        return bool((requirements.get("global_size") or {}).get("locked"))
    if decision.action == "set_local_size":
        zone = decision.parameters["zone"]
        return any(
            row.get("boundary_name") == zone and bool((row.get("size") or {}).get("locked"))
            for row in requirements.get("local_refinements", [])
        )
    if decision.action == "set_first_layer_height":
        return bool((layers.get("first_layer_height") or {}).get("locked"))
    if decision.action == "set_layer_count":
        return bool(layers.get("layers_locked"))
    if decision.action == "set_growth_rate":
        return bool(layers.get("growth_rate_locked"))
    return False


def _repeated_without_progress(state: PipelineState) -> bool:
    history = state.get("repair_history", [])
    if len(history) < 2:
        return False
    previous, current = history[-2:]
    previous_decision = previous.get("decision", {})
    current_decision = current.get("decision", {})
    return (
        previous.get("failure", {}).get("error") == current.get("failure", {}).get("error")
        and previous_decision.get("action") == current_decision.get("action")
        and previous_decision.get("parameters") == current_decision.get("parameters")
    )


def fluent_review_inputs(state: PipelineState) -> tuple[dict, dict]:
    """Do not carry free-form pre-edit CAD instructions into Fluent diagnosis."""
    requirements = copy.deepcopy(state.get("mesh_requirements") or {})
    job = copy.deepcopy(state.get("fluent_job") or {})
    requirements.pop("notes", None)
    if isinstance(job.get("parameter_sources"), dict):
        job["parameter_sources"].pop("notes", None)
    return requirements, job


def diagnose_failure(state: PipelineState) -> dict[str, Any]:
    requested_intervention = state.get("error_evidence", {}).get("human_request")
    if requested_intervention:
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "return_to_human",
                    "target_step": state["failed_step"],
                    "diagnosis": "A user clarification is required",
                    "evidence": state["error"],
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "",
                "human_request": requested_intervention,
            },
        )
    if state.get("error_evidence", {}).get("session_lost"):
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "stop",
                    "target_step": state["failed_step"],
                    "diagnosis": "Fluent session was lost",
                    "evidence": state["error"],
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "fluent_session_lost",
            },
        )
    if state.get("total_repair_rounds", 0) >= state.get("max_total_repair_rounds", 100):
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "stop",
                    "target_step": state["failed_step"],
                    "diagnosis": "Cumulative repair limit exhausted",
                    "evidence": state["error"],
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "cumulative_repair_budget_exhausted",
            },
        )
    if state.get("repair_rounds", 0) >= state["max_repair_rounds"]:
        can_revise_cad = bool(state.get("working_geometry") and state.get("labeling"))
        if can_revise_cad:
            request = _human_request(
                state,
                kind="cad_revision",
                message="Automatic repair reached its configured limit; inspect or revise the confirmed CAD.",
                required_action="Edit the working CAD if needed, then confirm its groups and boundary roles.",
                evidence={"error": state.get("error"), "repair_rounds": state.get("repair_rounds")},
            )
            return _persist(
                state,
                "review_failure",
                {
                    "repair_decision": {
                        "action": "return_to_human",
                        "target_step": "human_confirmation",
                        "diagnosis": "Automatic repair budget exhausted",
                        "evidence": state["error"],
                        "parameters": {},
                    },
                    "repair_decision_source": "system",
                    "repair_stop_reason": "",
                    "human_request": request,
                },
            )
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "stop",
                    "target_step": state["failed_step"],
                    "diagnosis": "Repair budget exhausted",
                    "evidence": state["error"],
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "repair_budget_exhausted",
            },
        )
    if _repeated_without_progress(state):
        if state.get("working_geometry") and state.get("labeling"):
            request = _human_request(
                state,
                kind="cad_revision",
                message="The same repair has failed twice without progress; further automatic retries are stopped.",
                required_action="Inspect or revise the confirmed CAD, then confirm its groups and boundary roles.",
                evidence={"error": state.get("error"), "recent_repairs": state.get("repair_history", [])[-2:]},
            )
            return _persist(
                state,
                "review_failure",
                {
                    "repair_decision": {
                        "action": "return_to_human",
                        "target_step": "human_confirmation",
                        "diagnosis": "Repeated repair made no progress",
                        "evidence": state["error"],
                        "parameters": {},
                    },
                    "repair_decision_source": "system",
                    "repair_stop_reason": "",
                    "human_request": request,
                },
            )
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "stop",
                    "target_step": state["failed_step"],
                    "diagnosis": "Repeated repair made no progress",
                    "evidence": state["error"],
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "repeated_repair_without_progress",
            },
        )
    try:
        fluent_failure = state["failed_step"] in (
            *FLUENT_STEPS,
            "final_validation",
            "launch_fluent",
        )
        evidence = {
            "available_tools": repair_tool_catalog(),
            "failed_step": state["failed_step"],
            "error": state["error"],
            "error_evidence": state.get("error_evidence", {}),
            "confirmed_geometry": state.get("confirmed_geometry"),
            "confirmed_boundary_roles": state.get("boundary_roles"),
            "mesh_requirements": state.get("mesh_requirements"),
            "fluent_job": state.get("fluent_job"),
            "fluent_steps": state.get("fluent_steps", {}),
        }
        if fluent_failure:
            evidence["mesh_requirements"], evidence["fluent_job"] = fluent_review_inputs(state)
        images: list[Path] = []
        if not fluent_failure and state.get("catalog"):
            evidence["selection_plan"] = state.get("selection_plan")
            evidence["candidate_catalog"] = GeometryCatalog.model_validate(
                state["catalog"]
            ).public_dict()
            images.extend(
                Path(row["path"])
                for row in state.get("native_selection", {}).get("images", [])
                if row.get("path") and Path(row["path"]).is_file()
            )
        if fluent_failure:
            try:
                worker = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
                evidence["fluent_observation"] = worker.call("observe")
                picture = worker.call("picture")
                evidence["fluent_picture"] = picture
                if picture.get("path") and Path(picture["path"]).is_file():
                    images.append(Path(picture["path"]))
            except Exception as observation_error:
                evidence["observation_error"] = str(observation_error)
        settings = config_from_state(state)
        client = GroundingLLMClient.from_runtime_config(
            config=settings,
            audit_dir=_run_dir(state) / "llm" / "reviewer",
        )
        if images:
            probe = client.probe_vision()
            if probe.status.value != "verified":
                evidence["image_capability"] = probe.reason
                images = []
        decision = client.invoke(
            system_prompt=load_prompt("reviewer"),
            user_prompt=json.dumps(evidence, ensure_ascii=False),
            images=images[:6],
            response_model=RepairDecision,
        )
        rounds = state.get("repair_rounds", 0) + 1
        total_rounds = state.get("total_repair_rounds", 0) + 1
        history = list(state.get("repair_history", []))
        history.append(
            {"round": rounds, "decision": decision.model_dump(mode="json"), "failure": evidence}
        )
        return _persist(
            state,
            "review_failure",
            {
                "repair_rounds": rounds,
                "total_repair_rounds": total_rounds,
                "repair_decision": decision.model_dump(mode="json"),
                "repair_decision_source": "llm",
                "repair_stop_reason": (
                    "llm_requested_stop" if decision.action == "stop" else ""
                ),
                "repair_history": history,
            },
        )
    except Exception as error:
        return _persist(
            state,
            "review_failure",
            {
                "repair_decision": {
                    "action": "stop",
                    "target_step": state["failed_step"],
                    "diagnosis": "Reviewer failed",
                    "evidence": f"{type(error).__name__}: {error}",
                    "parameters": {},
                },
                "repair_decision_source": "system",
                "repair_stop_reason": "reviewer_exception",
            },
        )


def execute_repair(state: PipelineState) -> RepairOutcome:
    try:
        decision = RepairDecision.model_validate(state["repair_decision"])
        resume = _repair_resume_step(state, decision)
    except ValueError as error:
        return RepairOutcome(update={"error": f"Invalid repair route: {error}"}, goto="failed")
    target = decision.target_step
    fluent_targets = {*FLUENT_STEPS, "final_validation"}
    if decision.action == "retry_step" and target not in fluent_targets:
        return RepairOutcome(update={"error": "", "status": "running"}, goto=target)
    if decision.action == "replace_object_reference":
        if target not in {"verify_selection", "extract_volume", "label_faces", "validate_cad"}:
            return RepairOutcome(
                update={"error": "replace_object_reference is not valid for this step"},
                goto="failed",
            )
        parameters = decision.parameters
        candidate_id = str(parameters.get("candidate_id", ""))
        field = str(parameters.get("field", ""))
        catalog = GeometryCatalog.model_validate(state["catalog"])
        if candidate_id not in catalog.by_id():
            return RepairOutcome(update={"error": "Reviewer candidate_id is absent"}, goto="failed")
        plan = copy.deepcopy(state["selection_plan"])
        if field == "seed_inner_wall_id":
            plan["seed_inner_wall_id"] = candidate_id
        elif field.startswith("opening:"):
            name = field.split(":", 1)[1]
            matches = [item for item in plan["openings"] if item["name"] == name]
            if len(matches) != 1:
                return RepairOutcome(
                    update={"error": "Reviewer opening name is not unique"}, goto="failed"
                )
            matches[0]["candidate_id"] = candidate_id
        else:
            return RepairOutcome(
                update={"error": "Reviewer object field is invalid"}, goto="failed"
            )
        original = str(Path(state["runtime_dir"]) / "original.scdoc")
        return RepairOutcome(
            update={
                "selection_plan": plan,
                "working_geometry": original,
                "extraction": {},
                "labeling": {},
                "error": "",
                "status": "running",
            },
            goto="verify_selection",
        )
    if decision.action == "return_to_human":
        close_client(state["run_id"])
        request = state.get("human_request") or _human_request(
            state,
            kind="cad_revision",
            message="The mesh cannot be repaired without revising the confirmed CAD or its boundary roles.",
            required_action="Edit the working CAD if needed, then confirm its groups and boundary roles.",
            evidence={"error": state.get("error"), "reviewer_evidence": decision.evidence},
        )
        return RepairOutcome(
            update={"error": "", "status": "human_confirmation_required", "human_request": request},
            goto="human_intervention" if request["kind"] in {"clarification", "locked_parameter", "boundary_mapping"} else "human_confirmation",
        )
    if decision.action == "stop":
        return RepairOutcome(goto="failed")
    if _locked_control(state, decision) and not state.get("repair_override"):
        close_client(state["run_id"])
        evidence = _copy_runtime_evidence(state, "intervention-locked-parameter")
        return RepairOutcome(
            update={
                "human_request": _human_request(
                    state,
                    kind="locked_parameter",
                    message="The proposed repair would change a parameter the user marked as locked.",
                    required_action="Approve the proposed value, optionally provide a replacement value, or cancel.",
                    evidence={
                        "repair_action": decision.action,
                        "proposed_parameters": decision.parameters,
                        "diagnosis": decision.diagnosis,
                    },
                ),
                "artifacts": {
                    **state.get("artifacts", {}),
                    **{"intervention:" + name: path for name, path in evidence.items()},
                },
                "fluent_steps": {},
                "final_execution": {},
            },
            goto="human_intervention",
        )
    if (
        decision.action in {"replace_zone_reference", "set_layer_targets"}
        and not state.get("repair_override")
    ):
        close_client(state["run_id"])
        evidence = _copy_runtime_evidence(state, "intervention-boundary-mapping")
        return RepairOutcome(
            update={
                "human_request": _human_request(
                    state,
                    kind="boundary_mapping",
                    message="The requested label replacement cannot prove that it preserves the confirmed physical boundary.",
                    required_action="Provide the exact replacement label after reviewing the confirmed boundary mapping, or cancel.",
                    evidence={
                        "repair_action": decision.action,
                        "proposed_parameters": decision.parameters,
                        "diagnosis": decision.diagnosis,
                    },
                ),
                "artifacts": {
                    **state.get("artifacts", {}),
                    **{"intervention:" + name: path for name, path in evidence.items()},
                },
                "fluent_steps": {},
                "final_execution": {},
            },
            goto="human_intervention",
        )
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        repair_request = decision.model_dump(mode="json")
        repair_request["manual_approved"] = bool(state.get("repair_override"))
        repair_request["skip_revert"] = bool(state.get("pending_repair_after_rebuild"))
        applied = client.call("repair", repair_request)
        if applied["resume"] != resume:
            raise ValueError("Fluent worker returned an unexpected repair resume step")
        history = list(state.get("repair_history", []))
        history[-1]["application"] = applied
        return RepairOutcome(
            update={
                "repair_history": history,
                "repair_override": False,
                "pending_repair_after_rebuild": False,
                "final_execution": {
                    "controls": applied.get("controls", {}),
                    "boundary_mapping": applied.get("controls", {}).get("boundaries", {}),
                },
                "error": "",
                "status": "running",
            },
            goto=applied["resume"],
        )
    except Exception as error:
        return RepairOutcome(update={"error": f"Repair application failed: {error}"}, goto="failed")
