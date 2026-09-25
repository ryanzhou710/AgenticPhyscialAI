"""Durable human confirmation and actual saved-CAD handoff."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from langgraph.types import Command, interrupt

from src.services.boundaries import (
    build_fluent_job,
    confirm_roles,
    rebind_mesh_targets,
    validate_confirmed_cad,
)
from src.services.contracts import ConfirmationPayload, HumanInterventionPayload
from src.services.execution import _failed, _run_dir, _succeeded
from src.services.spaceclaim_runtime import open_spaceclaim_reader
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
            "human_request": request,
        }
    )
    payload = ConfirmationPayload.model_validate(response)
    return _succeeded(
        state,
        "human_confirmation",
        {
            "human_response": payload.model_dump(mode="json"),
            "status": "running",
        },
    )


def _approved_repair(decision: dict[str, Any]) -> Command:
    return Command(
        update={
            "repair_decision": decision,
            "repair_approved": True,
            "human_request": {},
        },
        goto="apply_repair",
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
                "repair_rounds": 0,
                "human_request": {},
            },
            goto="understand_prompt",
        )
    if kind == "parameter_change":
        if payload.action != "approve":
            raise ValueError("User parameter changes require action=approve or cancel")
        decision = dict(state["repair_decision"])
        parameters = dict(decision["parameters"])
        if payload.parameter_value is not None:
            parameters["value"] = payload.parameter_value
        decision["parameters"] = parameters
        return _approved_repair(decision)
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
        return _approved_repair(decision)
    raise ValueError("Unsupported human intervention kind: " + str(kind))


def reload_confirmed_cad(state: PipelineState) -> dict[str, Any]:
    try:
        response = ConfirmationPayload.model_validate(state["human_response"])
        with open_spaceclaim_reader(
            state, _run_dir(state) / "artifacts" / "confirmed-catalog", ui_mode="hidden"
        ) as runner:
            catalog, path = runner.catalog(Path(state["working_geometry"]), render_candidates=False)
        roles = confirm_roles(
            catalog=catalog,
            proposed=response.boundary_roles,
            previous=state["boundary_roles"],
        )
        confirmed_validation = validate_confirmed_cad(catalog=catalog, roles=roles)
        confirmed_path = _run_dir(state) / "artifacts" / "confirmed.scdoc"
        shutil.copy2(state["working_geometry"], confirmed_path)
        # CAD readers run outside Python; use the same ASCII staging convention
        # as SpaceClaim instead of handing a Unicode archive path to Fluent.
        runtime_confirmed = Path(state["runtime_dir"]) / "confirmed.scdoc"
        shutil.copy2(confirmed_path, runtime_confirmed)
        requirements = rebind_mesh_targets(
            requirements=state["mesh_requirements"],
            previous_groups=state["labeling"]["groups"],
            confirmed_catalog=catalog,
            roles=roles,
        )
        job = build_fluent_job(
            geometry=str(runtime_confirmed),
            roles=roles,
            requirements=requirements,
        )
        return _succeeded(
            state,
            "reload_confirmed_cad",
            {
                "confirmed_geometry": str(confirmed_path),
                "boundary_roles": roles,
                "fluent_job": job,
                "mesh_requirements": requirements,
                "cad_validation": confirmed_validation,
                # A saved CAD or role mapping is a new downstream input.  Do
                # not retain observations or final controls from its earlier
                # Fluent session.
                "fluent_steps": {},
                "final_execution": {},
                "repair_rounds": 0 if state.get("human_request") else state.get("repair_rounds", 0),
                "human_request": {},
                "repair_approved": False,
                "artifacts": {
                    **state["artifacts"],
                    "confirmed_geometry": str(confirmed_path),
                    "confirmed_catalog": str(path),
                },
            },
        )
    except Exception as error:
        return _failed(state, "reload_confirmed_cad", error)

def confirmation_route(state: PipelineState) -> str:
    return "cancelled" if state["human_response"]["action"] == "cancel" else "reload_confirmed_cad"
