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
from src.services.contracts import RepairDecision, repair_action_spec, repair_tool_catalog
from src.services.execution import _copy_runtime_evidence, _persist, _run_dir
from src.services.geometry_catalog import GeometryCatalog
from src.state import PipelineState
from src.workers.fluent.repair import STEP_ORDER

FLUENT_STEPS = STEP_ORDER[:-1]
VISUAL_REVIEW_STEPS = frozenset({
    "verify_selection", "extract_volume", "label_faces",
    "surface_mesh", "boundary_layers", "volume_mesh", "final_validation",
})
TEXT_ONLY_ERROR_CODES = frozenset({
    "RUNTIME_TIMEOUT", "CAD_EXTRACTION_RUNTIME_FAILED", "LLM_REQUEST_FAILED",
})
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
    spec = repair_action_spec(action)
    failed = state.get("failed_step")
    if spec.route == "stop":
        return "failed"
    if state.get("error_evidence", {}).get("session_lost"):
        raise ValueError("A lost Fluent session cannot be repaired")
    if spec.route == "retry":
        if target != failed or failed not in (
            *CAD_STEPS,
            "reload_confirmed_cad",
            "launch_fluent",
            *STEP_ORDER,
        ):
            raise ValueError("retry_step must repeat the current failed operation")
        return target
    if spec.route == "cad":
        if (
            failed not in CAD_STEPS[3:]
            or target not in CAD_STEPS[3:]
            or CAD_STEPS.index(target) > CAD_STEPS.index(failed)
            or state.get("confirmed_geometry")
            or state.get("human_response", {}).get("action") == "approve"
        ):
            raise ValueError("Object references can only be repaired before CAD confirmation")
        return "verify_selection"
    if spec.route == "human":
        request = state.get("human_request", {})
        if request.get("kind") in {"clarification", "parameter_change", "boundary_mapping"}:
            return "human_intervention"
        if (
            failed not in ("reload_confirmed_cad", "launch_fluent", *STEP_ORDER)
            or target not in {failed, "human_confirmation"}
            or not state.get("working_geometry")
            or not state.get("labeling")
        ):
            raise ValueError("Return to human requires a concrete pending intervention")
        return "human_confirmation"
    resume = spec.resume_for(decision.parameters)
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


def _repair_application_failed(
    state: PipelineState, *, reason: str, error: BaseException | str
) -> RepairOutcome:
    """Stop a repair without replacing the CAD or Fluent failure being repaired."""

    history = list(state.get("repair_history", []))
    record = {"reason": reason, "error": str(error)}
    if history:
        history[-1] = {**history[-1], "application_error": record}
    else:
        history.append({"application_error": record})
    return RepairOutcome(
        update={
            "repair_history": history,
            "repair_approved": False,
            "repair_stop_reason": reason,
        },
        goto="failed",
    )


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


def _user_parameter_change(state: PipelineState, decision: RepairDecision) -> dict | None:
    """Identify the user control, preserving provenance after runtime label changes."""
    parameter = repair_action_spec(decision.action).user_parameter
    if parameter is None:
        return None
    requirements = state.get("mesh_requirements", {})
    layers = requirements.get("boundary_layers") or {}
    controls = (
        state.get("error_evidence", {}).get("controls")
        or state.get("final_execution", {}).get("controls")
        or {}
    )
    source = None
    requested = None
    current = None
    requested_unit = None
    target = "global"
    if parameter == "global_size":
        control = requirements.get("global_size") or {}
        source, requested = control.get("source"), control.get("value")
        requested_unit = control.get("unit")
        current = controls.get("global_size")
    elif parameter == "local_size":
        zone = decision.parameters["zone"]
        runtime = next(
            (row for row in controls.get("local_refinements", []) if row.get("zone") == zone),
            {},
        )
        original_zone = runtime.get("source_boundary_name") or zone
        row = next(
            (row for row in requirements.get("local_refinements", [])
             if row.get("boundary_name") == original_zone),
            {},
        )
        control = row.get("size") or {}
        source, requested = control.get("source"), control.get("value")
        requested_unit = control.get("unit")
        current, target = runtime.get("size"), zone
    else:
        key = {
            "first_layer_height": "first_layer_height",
            "layer_count": "layers",
            "growth_rate": "growth_rate",
        }[parameter]
        runtime_layers = controls.get("boundary_layers") or {}
        target = runtime_layers.get(
            "zones", layers.get("boundary_names") or layers.get("target", "all walls")
        )
        current = runtime_layers.get(key)
        if key == "first_layer_height":
            control = layers.get(key) or {}
            source, requested = control.get("source"), control.get("value")
            requested_unit = control.get("unit")
        else:
            source, requested = layers.get(key + "_source"), layers.get(key)
    disabling_layers = parameter == "layer_count" and decision.parameters["value"] == 0
    if disabling_layers and source == "user" and requested == 0 and current in (None, 0):
        return None
    if source != "user" and not disabling_layers:
        return None
    return {
        "target": target,
        "requested_value": requested,
        "original_expression": (control.get("original_expression", "") if requested_unit else ""),
        "requested_unit": requested_unit or "dimensionless",
        "current_value": current,
        "proposed_value": decision.parameters["value"],
        "unit": (
            controls.get("length_unit") or "unknown (see Fluent controls)"
        ) if requested_unit else "dimensionless",
    }


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
        use_visuals = (
            state["failed_step"] in VISUAL_REVIEW_STEPS
            and state.get("error_detail", {}).get("code") not in TEXT_ONLY_ERROR_CODES
        )
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
            "boundary_roles": state.get("boundary_roles"),
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
            if use_visuals:
                images.extend(
                    Path(row["path"])
                    for row in state.get("native_selection", {}).get("images", [])
                    if row.get("path") and Path(row["path"]).is_file()
                )
        if fluent_failure:
            try:
                worker = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
                evidence["fluent_observation"] = worker.call("observe")
                if use_visuals:
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
            try:
                probe = client.probe_vision()
                if probe.status.value != "verified":
                    evidence["image_capability"] = probe.reason
                    images = []
            except Exception as vision_error:
                evidence["image_capability"] = str(vision_error)
                images = []
        decision = client.invoke(
            system_prompt=(
                "Diagnose one failed SpaceClaim or Fluent operation from the supplied current-run evidence.\n"
                "\n"
                "Choose exactly one declared repair tool. A repair may change only this run's object\n"
                "references or meshing controls, then rerun the affected step and invalid downstream steps.\n"
                "Do not change source code, execute scripts, use a memorized case answer, restore the\n"
                "pre-confirmation CAD, alter an explicit user boundary role, or lower validation criteria.\n"
                "For Fluent, treat the confirmed CAD and confirmed boundary-role table as authoritative.\n"
                "If geometry must change, return to human confirmation. If evidence is insufficient or the\n"
                "available tools cannot resolve the issue, stop and say why.\n"
                "\n"
                "The evidence includes available_tools with the exact JSON parameter schemas. Do\n"
                "not invent parameters. retry_step takes an empty object and repeats the operation\n"
                "unchanged; it cannot change views, source code, or any other configuration.\n"
                "For retry_step, target_step must equal failed_step. Object-reference repairs are\n"
                "allowed only before CAD confirmation. Fluent control repairs must target the failed\n"
                "step or the affected earlier step; they cannot skip a failure or return to CAD construction.\n"
                "return_to_human is available only after reaching the CAD handoff, with target_step\n"
                "set to failed_step or human_confirmation. Invalid repair routes stop execution.\n"
                "replace_object_reference takes field and candidate_id. It changes only the named\n"
                "selection reference and restarts verification on the original CAD. Use only a\n"
                "candidate present in the supplied real catalog, preserving the requested role.\n"
                "Fluent controls use value; set_local_size additionally requires zone.\n"
                "set_layer_count changes the integer boundary-layer count; set_first_layer_height\n"
                "changes its first height. All repair lengths use the current Fluent import unit\n"
                "reported in controls.length_unit. Do not impose project-specific numeric ranges;\n"
                "use the actual software error to propose a correction. Numeric controls, including\n"
                "inferred or default values, can be repaired automatically when the current software\n"
                "evidence supports the change. Every proposed change to a user-specified numeric control\n"
                "requires human approval. Disabling boundary layers also requires approval even for inferred\n"
                "or default layer counts. Original requests remain in the evidence; current attempted\n"
                "values are in the observed controls.\n"
                "For local sizing, source_boundary_name identifies the original request after an\n"
                "execution label is replaced. A label repair does not grant approval to change the size.\n"
                "replace_zone_reference uses category, old and new, preserving boundary purpose. A proposed\n"
                "boundary or scope-label replacement cannot establish physical-surface identity from name\n"
                "similarity or model inference. The application requests a human mapping before applying\n"
                "any label replacement or set_layer_targets change, then verifies the supplied label against\n"
                "Fluent's actual boundary list and, for roles, its reported boundary type. set_layer_targets\n"
                "takes zones (a list of labels), including when the rejected scope was empty or one description\n"
                "needs to resolve to multiple native labels.\n"
                "Never replace an unresolved specific scope with a blanket all-wall scope.\n"
                "For source-code defects that these tools cannot change, stop with the diagnosis.\n"
            ),
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
                    "llm_requested_stop"
                    if repair_action_spec(decision.action).route == "stop"
                    else ""
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
        return _repair_application_failed(
            state, reason="invalid_repair_route", error=error
        )
    spec = repair_action_spec(decision.action)
    target = decision.target_step
    fluent_targets = {*FLUENT_STEPS, "final_validation"}
    if spec.route == "retry" and target not in fluent_targets:
        return RepairOutcome(update={"status": "running"}, goto=target)
    if spec.route == "cad":
        if target not in {"verify_selection", "extract_volume", "label_faces", "validate_cad"}:
            return _repair_application_failed(
                state,
                reason="invalid_object_reference_repair",
                error="replace_object_reference is not valid for this step",
            )
        parameters = decision.parameters
        candidate_id = str(parameters.get("candidate_id", ""))
        field = str(parameters.get("field", ""))
        catalog = GeometryCatalog.model_validate(state["catalog"])
        if candidate_id not in catalog.by_id():
            return _repair_application_failed(
                state,
                reason="invalid_object_reference_repair",
                error="Reviewer candidate_id is absent",
            )
        plan = copy.deepcopy(state["selection_plan"])
        if field == "seed_inner_wall_id":
            plan["seed_inner_wall_id"] = candidate_id
        elif field.startswith("opening:"):
            name = field.split(":", 1)[1]
            matches = [item for item in plan["openings"] if item["name"] == name]
            if len(matches) != 1:
                return _repair_application_failed(
                    state,
                    reason="invalid_object_reference_repair",
                    error="Reviewer opening name is not unique",
                )
            matches[0]["candidate_id"] = candidate_id
        else:
            return _repair_application_failed(
                state,
                reason="invalid_object_reference_repair",
                error="Reviewer object field is invalid",
            )
        original = str(Path(state["runtime_dir"]) / "original.scdoc")
        return RepairOutcome(
            update={
                "selection_plan": plan,
                "working_geometry": original,
                "extraction": {},
                "labeling": {},
                "status": "running",
            },
            goto="verify_selection",
        )
    if spec.route == "human":
        close_client(state["run_id"])
        request = state.get("human_request") or _human_request(
            state,
            kind="cad_revision",
            message="The mesh cannot be repaired without revising the confirmed CAD or its boundary roles.",
            required_action="Edit the working CAD if needed, then confirm its groups and boundary roles.",
            evidence={"error": state.get("error"), "reviewer_evidence": decision.evidence},
        )
        return RepairOutcome(
            update={"status": "human_confirmation_required", "human_request": request},
            goto=(
                "human_intervention"
                if request["kind"] in {"clarification", "parameter_change", "boundary_mapping"}
                else "human_confirmation"
            ),
        )
    if spec.route == "stop":
        return RepairOutcome(goto="failed")
    parameter_change = (
        _user_parameter_change(state, decision)
        if spec.approval == "user_parameter"
        else None
    )
    if parameter_change is not None and not state.get("repair_approved"):
        evidence = _copy_runtime_evidence(state, "intervention-parameter-change")
        return RepairOutcome(
            update={
                "human_request": _human_request(
                    state,
                    kind="parameter_change",
                    message="The proposed repair changes a user-specified parameter or disables boundary layers and requires approval.",
                    required_action="Approve the proposed value, optionally provide a replacement value, or cancel.",
                    evidence={
                        "repair_action": decision.action,
                        "proposed_parameters": decision.parameters,
                        **parameter_change,
                        "diagnosis": decision.diagnosis,
                    },
                ),
                "artifacts": {
                    **state.get("artifacts", {}),
                    **{"intervention:" + name: path for name, path in evidence.items()},
                },
                "final_execution": {},
            },
            goto="human_intervention",
        )
    if spec.approval == "boundary_mapping" and not state.get("repair_approved"):
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
                "final_execution": {},
            },
            goto="human_intervention",
        )
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        repair_request = decision.model_dump(mode="json")
        repair_request["manual_approved"] = bool(state.get("repair_approved"))
        applied = client.call("repair", repair_request)
        if applied["resume"] != resume:
            raise ValueError("Fluent worker returned an unexpected repair resume step")
        history = list(state.get("repair_history", []))
        history[-1]["application"] = applied
        return RepairOutcome(
            update={
                "repair_history": history,
                "repair_approved": False,
                "final_execution": {
                    "controls": applied.get("controls", {}),
                    "boundary_mapping": applied.get("controls", {}).get("boundaries", {}),
                },
                "status": "running",
            },
            goto=applied["resume"],
        )
    except Exception as error:
        return _repair_application_failed(
            state, reason="repair_application_exception", error=error
        )
