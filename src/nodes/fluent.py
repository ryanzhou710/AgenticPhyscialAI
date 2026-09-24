"""Fluent task nodes using a persistent worker."""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from src.adapters.fluent import get_client
from src.config import config_from_state
from src.services.execution import _failed, _persist
from src.state import PipelineState
from src.workers.repair_protocol import STEP_ORDER

FLUENT_STEPS = STEP_ORDER[:-1]


def launch_fluent(state: PipelineState) -> dict[str, Any]:
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        client.call(
            "initialize",
            {
                "ui_mode": state["ui_mode"],
                "job": state["fluent_job"],
                "runtime_config": state.get("runtime_config", {}),
            },
        )
        result = client.call("launch")
        steps = dict(state.get("fluent_steps", {}))
        steps["launch"] = result
        return _persist(state, "launch_fluent", {"fluent_steps": steps, "error": ""})
    except Exception as error:
        return _failed(state, "launch_fluent", error)


def rebuild_fluent(state: PipelineState) -> Command:
    """Launch a fresh Fluent session before replaying steps for a paused repair."""
    result = launch_fluent(state)
    return Command(update=result, goto="import_geometry" if not result.get("error") else "review_failure")


def fluent_step(step: str):
    def execute(state: PipelineState) -> dict[str, Any] | Command:
        if state.get("pending_repair_after_rebuild") and state.get("failed_step") == step:
            return Command(goto="apply_repair")
        try:
            client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
            result = client.call("execute_step", {"step": step})
            steps = dict(state.get("fluent_steps", {}))
            steps[step] = result
            return _persist(state, step, {"fluent_steps": steps, "error": ""})
        except Exception as error:
            return _failed(state, step, error)

    execute.__name__ = step
    return execute


def validate_mesh(state: PipelineState) -> dict[str, Any] | Command:
    step = "final_validation"
    if state.get("pending_repair_after_rebuild") and state.get("failed_step") == step:
        return Command(goto="apply_repair")
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        result = client.call("execute_step", {"step": step})
        picture = client.call("picture")
        steps = dict(state.get("fluent_steps", {}))
        steps[step] = result
        steps["picture"] = picture
        return _persist(
            state,
            step,
            {
                "fluent_steps": steps,
                "final_execution": result.get("final_execution", {}),
                "error": "",
            },
        )
    except Exception as error:
        return _failed(state, step, error)
