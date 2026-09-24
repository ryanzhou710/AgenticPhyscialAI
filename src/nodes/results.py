"""Terminal results and conditional routing."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.adapters.fluent import close_client
from src.adapters.windows_process import request_window_close
from src.services.artifacts import write_json
from src.services.execution import _copy_runtime_evidence, _persist, _run_dir
from src.state import PipelineState


def completed(state: PipelineState) -> dict[str, Any]:
    runtime = Path(state["runtime_dir"])
    artifacts = dict(state.get("artifacts", {}))
    for name in (
        "mesh.msh.h5",
        "final-mesh.png",
        "fluent.trn",
        "fluent-worker.log",
        "execution-parameters.json",
        "fluent-worker-stderr.log",
    ):
        source = runtime / name
        if source.is_file():
            destination = _run_dir(state) / "artifacts" / name
            shutil.copy2(source, destination)
            artifacts[name] = str(destination)
    runtime_evidence = _copy_runtime_evidence(state, "success-runtime")
    artifacts.update({"success-runtime:" + name: path for name, path in runtime_evidence.items()})
    final_validation = state.get("fluent_steps", {}).get("final_validation", {})
    result = {
        "status": "success",
        "run_id": state["run_id"],
        "confirmed_geometry": state["confirmed_geometry"],
        "confirmed_boundary_roles": state["boundary_roles"],
        "boundary_roles": state["boundary_roles"],
        "requested_mesh_requirements": state.get("requested_mesh_requirements", {}),
        "confirmed_mesh_requirements": state["mesh_requirements"],
        "mesh_requirements": state["mesh_requirements"],
        "final_execution": state.get("final_execution", {}),
        "quality": final_validation.get("quality", {}),
        "mesh": artifacts.get("mesh.msh.h5"),
        "mesh_image": artifacts.get("final-mesh.png"),
        "repair_rounds": state.get("repair_rounds", 0),
        "total_repair_rounds": state.get("total_repair_rounds", 0),
        "repair_history": state.get("repair_history", []),
        "human_confirmation": state.get("human_response", {}),
        "parameter_record": artifacts.get("execution-parameters.json"),
        "runtime_evidence": runtime_evidence,
    }
    if not state.get("keep_open"):
        close_client(state["run_id"])
        if state.get("labeling", {}).get("kept_open"):
            result["spaceclaim_close"] = request_window_close(state["labeling"])
    write_json(_run_dir(state) / "result.json", result)
    return _persist(
        state,
        "completed",
        {"status": "success", "result": result, "artifacts": artifacts, "error": ""},
    )


def failed(state: PipelineState) -> dict[str, Any]:
    evidence = _copy_runtime_evidence(state, "failure-runtime")
    result = {
        "status": "failed",
        "run_id": state["run_id"],
        "failed_step": state.get("failed_step"),
        "error": state.get("error"),
        "error_evidence": state.get("error_evidence", {}),
        "repair_decision_source": state.get("repair_decision_source", "unknown"),
        "repair_stop_reason": state.get("repair_stop_reason", ""),
        "repair_rounds": state.get("repair_rounds", 0),
        "total_repair_rounds": state.get("total_repair_rounds", 0),
        "repair_history": state.get("repair_history", []),
        "requested_mesh_requirements": state.get("requested_mesh_requirements", {}),
        "confirmed_mesh_requirements": state.get("mesh_requirements", {}),
        "confirmed_boundary_roles": state.get("boundary_roles", {}),
        "final_execution": state.get("final_execution", {}),
        "quality": state.get("fluent_steps", {}).get("final_validation", {}).get("quality", {}),
        "runtime_evidence": evidence,
    }
    write_json(_run_dir(state) / "result.json", result)
    if not state["keep_open"]:
        close_client(state["run_id"])
    return _persist(state, "failed", {"status": "failed", "result": result})


def has_error(state: PipelineState) -> str:
    return "review_failure" if state.get("error") else "continue"


def review_route(state: PipelineState) -> str:
    action = state["repair_decision"]["action"]
    return "failed" if action == "stop" else "apply_repair"
