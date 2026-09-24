"""Runtime failure diagnosis and declared run-data tool application."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cfd_agent.adapters.fluent import get_client
from cfd_agent.adapters.llm import GroundingLLMClient
from cfd_agent.config import config_from_state
from cfd_agent.services.contracts import RepairDecision, repair_tool_catalog
from cfd_agent.services.execution import _persist, _run_dir
from cfd_agent.services.geometry_models import GeometryCatalog
from cfd_agent.services.prompts import load_prompt
from cfd_agent.state import PipelineState
from cfd_agent.workers.repair_protocol import STEP_ORDER

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
        if (
            failed not in ("reload_confirmed_cad", "launch_fluent", *STEP_ORDER)
            or target not in {failed, "human_confirmation"}
            or not state.get("working_geometry")
            or not state.get("labeling")
        ):
            raise ValueError("Return to human requires an existing CAD handoff")
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


def fluent_review_inputs(state: PipelineState) -> tuple[dict, dict]:
    """Do not carry free-form pre-edit CAD instructions into Fluent diagnosis."""
    requirements = copy.deepcopy(state.get("mesh_requirements") or {})
    job = copy.deepcopy(state.get("fluent_job") or {})
    requirements.pop("notes", None)
    if isinstance(job.get("parameter_sources"), dict):
        job["parameter_sources"].pop("notes", None)
    return requirements, job


def diagnose_failure(state: PipelineState) -> dict[str, Any]:
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
    if state.get("repair_rounds", 0) >= state["max_repair_rounds"]:
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
        history = list(state.get("repair_history", []))
        history.append(
            {"round": rounds, "decision": decision.model_dump(mode="json"), "failure": evidence}
        )
        return _persist(
            state,
            "review_failure",
            {
                "repair_rounds": rounds,
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


def parameter_change(
    state: PipelineState, decision: RepairDecision, controls: dict | None = None
) -> dict[str, Any] | None:
    """Describe a proposed numeric change using the original request's authority."""
    requirements = state.get("mesh_requirements", {})
    layers = requirements.get("boundary_layers") or {}
    control = None
    parameter = decision.action
    if decision.action == "set_global_size":
        control = requirements.get("global_size")
        parameter = "global_size"
    elif decision.action == "set_local_size":
        zone = decision.parameters["zone"]
        # The worker preserves request identity when an execution label is repaired.
        # Match the same first control that RepairState.apply will modify.
        current = next(
            (row for row in (controls or {}).get("local_refinements", []) if row["zone"] == zone),
            {},
        )
        source_name = current.get("source_boundary_name", zone)
        control = next(
            (
                row["size"]
                for row in requirements.get("local_refinements", [])
                if source_name is not None and row.get("boundary_name") == source_name
            ),
            None,
        )
        parameter = "local_size:" + zone
    elif decision.action == "set_first_layer_height":
        control = layers.get("first_layer_height")
        parameter = "boundary_layers.first_layer_height"
    elif decision.action in {"set_layer_count", "set_growth_rate"}:
        field_name = "layers" if decision.action == "set_layer_count" else "growth_rate"
        control = {"value": layers.get(field_name), "source": layers.get(field_name + "_source")}
        parameter = "boundary_layers." + field_name
    else:
        return None
    control = control or {}
    return {
        "parameter": parameter,
        "source": control.get("source", "native_default"),
        "original_value": control.get("value"),
        "original_unit": control.get("unit"),
        "proposed_value": decision.parameters["value"],
        "value_type": "integer" if decision.action == "set_layer_count" else "number",
        "unit": None,
        "error": state.get("error", ""),
        "reason": decision.diagnosis,
    }


def execute_repair(state: PipelineState, *, user_approved: bool = False) -> RepairOutcome:
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
        return RepairOutcome(
            update={"error": "", "status": "human_confirmation_required"}, goto="human_confirmation"
        )
    if decision.action == "stop":
        return RepairOutcome(goto="failed")
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        observation = client.call("observe") if decision.action == "set_local_size" else None
        change = parameter_change(state, decision, (observation or {}).get("controls"))
        if change is not None and change["source"] == "user" and not user_approved:
            if observation is None:
                observation = client.call("observe")
            change["attempted_controls"] = observation.get("controls", {})
            if decision.action in {"set_global_size", "set_local_size", "set_first_layer_height"}:
                change["unit"] = observation["controls"]["length_unit"]
            return RepairOutcome(
                update={"parameter_confirmation": change}, goto="parameter_confirmation"
            )
        applied = client.call("repair", decision.model_dump(mode="json"))
        if applied["resume"] != resume:
            raise ValueError("Fluent worker returned an unexpected repair resume step")
        history = list(state.get("repair_history", []))
        history[-1]["application"] = applied
        return RepairOutcome(
            update={"repair_history": history, "error": "", "status": "running"},
            goto=applied["resume"],
        )
    except Exception as error:
        return RepairOutcome(update={"error": f"Repair application failed: {error}"}, goto="failed")
