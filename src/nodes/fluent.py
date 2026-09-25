"""Fluent task nodes using a persistent worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.adapters.fluent import get_client
from src.config import config_from_state
from src.services.artifacts import write_json
from src.services.errors import PipelineError, make_error_detail
from src.services.execution import _failed, _run_dir, _succeeded
from src.state import PipelineState
from src.workers.fluent.repair import STEP_ORDER

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
        return _succeeded(state, "launch_fluent", {"fluent_steps": steps})
    except Exception as error:
        return _failed(state, "launch_fluent", error)


def fluent_step(step: str):
    def execute(state: PipelineState) -> dict[str, Any]:
        try:
            client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
            result = client.call("execute_step", {"step": step})
            steps = dict(state.get("fluent_steps", {}))
            steps[step] = result
            return _succeeded(state, step, {"fluent_steps": steps})
        except Exception as error:
            return _failed(state, step, error)

    execute.__name__ = step
    return execute


def validate_mesh(state: PipelineState) -> dict[str, Any]:
    step = "final_validation"
    try:
        client = get_client(state["run_id"], state["runtime_dir"], config_from_state(state))
        result = client.call("execute_step", {"step": step})
    except Exception as error:
        return _failed(state, step, error)

    steps = dict(state.get("fluent_steps", {}))
    steps[step] = result
    warnings = list(state.get("warnings", []))
    try:
        steps["picture"] = client.call("picture")
    except Exception as error:
        artifact = _run_dir(state) / "artifacts" / "mesh-preview-error.json"
        preview_error = PipelineError(
            "MESH_PREVIEW_FAILED",
            str(error),
            stage=step,
            substep="mesh preview",
            suggested_action="Inspect the preview-error record; the validated mesh artifact is still available.",
        )
        detail = make_error_detail(step, preview_error, evidence_path=artifact)
        try:
            write_json(
                artifact,
                {
                    "error": f"{type(error).__name__}: {error}",
                    "error_detail": detail,
                },
            )
        except Exception as write_error:
            detail["evidence_write_error"] = f"{type(write_error).__name__}: {write_error}"
        preview_path = Path(state["runtime_dir"]) / "final-mesh.png"
        try:
            preview_path.unlink(missing_ok=True)
        except Exception as remove_error:
            detail["preview_remove_error"] = f"{type(remove_error).__name__}: {remove_error}"
        warnings.append(
            "Mesh preview could not be created; the mesh passed validation. Detailed record: "
            + str(artifact)
        )
    return _succeeded(
        state,
        step,
        {
            "fluent_steps": steps,
            "final_execution": result.get("final_execution", {}),
            "warnings": warnings,
        },
    )
