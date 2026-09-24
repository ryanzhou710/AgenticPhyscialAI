"""Durable human confirmation and actual saved-CAD handoff."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from langgraph.types import Command, interrupt

from src.adapters.fluent import close_client
from src.adapters.spaceclaim import SpaceClaimRunner
from src.config import config_from_state
from src.services.artifacts import write_json
from src.services.boundaries import build_fluent_job, confirm_roles, rebind_mesh_targets
from src.services.contracts import ConfirmationPayload, HumanInterventionPayload
from src.services.execution import _failed, _persist, _run_dir
from src.state import PipelineState


def human_confirmation(state: PipelineState) -> dict[str, Any]:
    request = state.get("human_request", {})
    message = (
        request.get("message")
        or "SpaceClaim processing is complete. Review or edit the working CAD, then answer yes/no in the CLI."
    )
    response = interrupt(
        {
            "kind": "cad_confirmation",
            "message": message,
            "working_geometry": state["working_geometry"],
            "boundary_roles": state["boundary_roles"],
            "spaceclaim_process_id": state["labeling"].get("process_id"),
            "human_request": request,
        }
    )
    payload = ConfirmationPayload.model_validate(response)
    return _persist(
        state,
        "human_confirmation",
        {
            "human_response": payload.model_dump(mode="json"),
            "status": "running",
            "error": "",
        },
    )


def human_intervention(state: PipelineState) -> Command:
    """Pause only when an actionable user decision is required."""
    request = state["human_request"]
    response = interrupt(request)
    payload = HumanInterventionPayload.model_validate(response)
    if payload.action == "cancel":
        return Command(update={"human_request": {}}, goto="cancelled")
    kind = request["kind"]
    if kind == "clarification":
        if payload.action != "clarify":
            raise ValueError("Clarification requests require action=clarify or cancel")
        clarified = state["prompt"].rstrip() + "\n\nUSER CLARIFICATION:\n" + payload.clarification.strip()
        return Command(
            update={
                "prompt": clarified,
                "input_version": state.get("input_version", 0) + 1,
                "repair_rounds": 0,
                "human_request": {},
                "error": "",
                "failed_step": "",
            },
            goto="understand_prompt",
        )
    if kind == "locked_parameter":
        if payload.action != "approve":
            raise ValueError("Locked parameter changes require action=approve or cancel")
        decision = dict(state["repair_decision"])
        parameters = dict(decision["parameters"])
        if payload.parameter_value is not None:
            parameters["value"] = payload.parameter_value
        decision["parameters"] = parameters
        return Command(
            update={
                "repair_decision": decision,
                "repair_override": True,
                "pending_repair_after_rebuild": True,
                "human_request": {},
            },
            goto="rebuild_fluent",
        )
    if kind == "boundary_mapping":
        if payload.action != "approve":
            raise ValueError("Boundary mapping requires action=approve")
        decision = dict(state["repair_decision"])
        parameters = dict(decision["parameters"])
        if decision["action"] == "set_layer_targets":
            replacements = payload.boundary_replacements or (
                [payload.boundary_replacement.strip()]
                if (payload.boundary_replacement or "").strip()
                else []
            )
            if not replacements:
                raise ValueError("Boundary-layer mapping requires one or more replacement labels")
            parameters["zones"] = replacements
        elif (payload.boundary_replacement or "").strip():
            parameters["new"] = payload.boundary_replacement.strip()
        else:
            raise ValueError("Boundary mapping requires boundary_replacement")
        decision["parameters"] = parameters
        return Command(
            update={
                "repair_decision": decision,
                "repair_override": True,
                "pending_repair_after_rebuild": True,
                "human_request": {},
            },
            goto="rebuild_fluent",
        )
    raise ValueError("Unsupported human intervention kind: " + str(kind))


def reload_confirmed_cad(state: PipelineState) -> dict[str, Any]:
    try:
        response = ConfirmationPayload.model_validate(state["human_response"])
        runner = SpaceClaimRunner(
            output_dir=_run_dir(state) / "artifacts" / "confirmed-catalog",
            ui_mode="hidden",
            config=config_from_state(state),
        )
        try:
            catalog, path = runner.catalog(Path(state["working_geometry"]), render_candidates=False)
        finally:
            runner.close()
        roles = confirm_roles(
            catalog=catalog,
            proposed=response.boundary_roles,
            previous=state["boundary_roles"],
        )
        confirmed = _run_dir(state) / "artifacts" / "confirmed.scdoc"
        shutil.copy2(state["working_geometry"], confirmed)
        # CAD readers run outside Python; use the same ASCII staging convention
        # as SpaceClaim instead of handing a Unicode archive path to Fluent.
        runtime_confirmed = Path(state["runtime_dir"]) / "confirmed.scdoc"
        shutil.copy2(confirmed, runtime_confirmed)
        requirements = rebind_mesh_targets(
            requirements=state["mesh_requirements"],
            previous_groups=state["labeling"]["groups"],
            confirmed=catalog,
            roles=roles,
        )
        job = build_fluent_job(
            geometry=str(runtime_confirmed),
            roles=roles,
            requirements=requirements,
        )
        return _persist(
            state,
            "reload_confirmed_cad",
            {
                "confirmed_geometry": str(confirmed),
                "confirmed_catalog": catalog.model_dump(mode="json"),
                "boundary_roles": roles,
                "fluent_job": job,
                "mesh_requirements": requirements,
                # A saved CAD or role mapping is a new downstream input.  Do
                # not retain observations or final controls from its earlier
                # Fluent session.
                "fluent_steps": {},
                "final_execution": {},
                "input_version": state.get("input_version", 0) + int(bool(state.get("human_request"))),
                "repair_rounds": 0 if state.get("human_request") else state.get("repair_rounds", 0),
                "human_request": {},
                "repair_override": False,
                "artifacts": {
                    **state["artifacts"],
                    "confirmed_geometry": str(confirmed),
                    "confirmed_catalog": str(path),
                },
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, "reload_confirmed_cad", error)


def confirmation_route(state: PipelineState) -> str:
    return "cancelled" if state["human_response"]["action"] == "cancel" else "reload_confirmed_cad"


def cancelled(state: PipelineState) -> dict[str, Any]:
    close_client(state["run_id"])
    result = {
        "status": "cancelled",
        "run_id": state["run_id"],
        "working_geometry": state["working_geometry"],
        "reason": "User cancelled",
    }
    write_json(_run_dir(state) / "result.json", result)
    return _persist(state, "cancelled", {"status": "cancelled", "result": result, "error": ""})
