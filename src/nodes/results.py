"""Terminal results, archive handling, and conditional routing."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from src.adapters.fluent import close_client
from src.adapters.windows_process import request_window_close
from src.services.artifacts import write_json
from src.services.errors import make_error_detail
from src.services.execution import _copy_runtime_evidence, _persist, _run_dir, _succeeded
from src.state import PipelineState

_PRIMARY_ARTIFACTS = (
    "mesh.msh.h5",
    "final-mesh.png",
    "fluent.trn",
    "fluent-worker.log",
    "execution-parameters.json",
    "fluent-worker-stderr.log",
)
_REQUIRED_PRIMARY_ARTIFACTS = {"mesh.msh.h5"}


def _warning(message: str, warnings: list[str]) -> None:
    warnings.append(message)
    print("[Warning] " + message, file=sys.stderr, flush=True)


def _copy_primary_artifacts(state: PipelineState) -> tuple[dict[str, str], list[str]]:
    runtime = Path(state["runtime_dir"])
    root = _run_dir(state) / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    artifacts = dict(state.get("artifacts", {}))
    warnings: list[str] = []
    for name in _PRIMARY_ARTIFACTS:
        source = runtime / name
        if name == "final-mesh.png" and not state.get("fluent_steps", {}).get("picture"):
            continue
        if not source.is_file():
            if name in _REQUIRED_PRIMARY_ARTIFACTS:
                raise FileNotFoundError(f"Required mesh artifact is missing: {source}")
            continue
        try:
            destination = root / name
            shutil.copy2(source, destination)
            artifacts[name] = str(destination)
        except Exception as error:
            if name in _REQUIRED_PRIMARY_ARTIFACTS:
                raise
            _warning(f"Optional artifact was not archived ({name}): {error}", warnings)
    return artifacts, warnings


def _cleanup(
    state: PipelineState, *, close_fluent: bool, close_editor: bool
) -> tuple[dict[str, Any], list[str]]:
    details: dict[str, Any] = {}
    warnings: list[str] = []
    if close_fluent:
        try:
            close_client(state["run_id"])
        except Exception as error:
            _warning(f"Fluent session could not be closed normally: {error}", warnings)
    if close_editor and state.get("labeling", {}).get("kept_open"):
        try:
            details["spaceclaim_close"] = request_window_close(state["labeling"])
        except Exception as error:
            _warning(f"SpaceClaim editor could not be closed normally: {error}", warnings)
    return details, warnings


def _archive_failure(
    state: PipelineState,
    *,
    operation: str,
    error: BaseException,
    result: dict[str, Any],
) -> dict[str, Any]:
    """End a terminal node when a required archive/result write cannot complete."""

    root = _run_dir(state)
    target = root / "result.json"
    detail_path = root / "artifacts" / f"{operation}-error.json"
    archive_detail = make_error_detail(operation, error, evidence_path=detail_path)
    archive_record = {
        "operation": operation,
        "error": f"{type(error).__name__}: {error}",
        "error_detail": archive_detail,
        "target": str(target),
    }
    try:
        write_json(detail_path, archive_record)
    except Exception as evidence_error:
        archive_record["evidence_write_error"] = (
            f"{type(evidence_error).__name__}: {evidence_error}"
        )

    original_error = state.get("error", "")
    original_detail = state.get("error_detail", {})
    original_evidence = state.get("error_evidence", {})
    failure_result = {
        **result,
        "status": "failed",
        "run_id": state["run_id"],
        "failed_step": state.get("failed_step") or operation,
        "error": original_error or archive_record["error"],
        "error_detail": original_detail or archive_detail,
        "error_evidence": original_evidence,
        "archive_error": archive_record,
    }
    print("[Finalization failed] " + archive_record["error"], file=sys.stderr, flush=True)
    if original_error:
        print("Original error: " + original_error, file=sys.stderr, flush=True)
    print("Result target: " + str(target), file=sys.stderr, flush=True)
    try:
        write_json(target, failure_result)
    except Exception as result_error:
        print(
            "[Finalization failed] Could not write final result: "
            + f"{type(result_error).__name__}: {result_error}",
            file=sys.stderr,
            flush=True,
        )

    update = {
        "status": "failed",
        "result": failure_result,
        "repair_stop_reason": "archive_failure",
    }
    if not original_error:
        update.update(
            {
                "failed_step": operation,
                "error": archive_record["error"],
                "error_detail": archive_detail,
                "error_evidence": {},
            }
        )
    try:
        return _persist(state, operation, update)
    except Exception as state_error:
        print(
            "[Finalization failed] Could not write final state: "
            + f"{type(state_error).__name__}: {state_error}",
            file=sys.stderr,
            flush=True,
        )
        return {**update, "current_step": operation}


def _finish(
    state: PipelineState,
    *,
    stage: str,
    result: dict[str, Any],
    artifacts: dict[str, str] | None = None,
    success: bool = False,
    close_fluent: bool = False,
    close_editor: bool = False,
) -> dict[str, Any]:
    close_details, cleanup_warnings = _cleanup(
        state, close_fluent=close_fluent, close_editor=close_editor
    )
    result.update(close_details)
    warnings = [*result.get("warnings", []), *state.get("warnings", []), *cleanup_warnings]
    if warnings:
        result["warnings"] = warnings
    try:
        write_json(_run_dir(state) / "result.json", result)
    except Exception as error:
        return _archive_failure(state, operation="archive_results", error=error, result=result)

    update: dict[str, Any] = {"status": result["status"], "result": result}
    if artifacts is not None:
        update["artifacts"] = artifacts
    try:
        return _succeeded(state, stage, update) if success else _persist(state, stage, update)
    except Exception as error:
        return _archive_failure(state, operation="archive_results", error=error, result=result)


def completed(state: PipelineState) -> dict[str, Any]:
    try:
        artifacts, warnings = _copy_primary_artifacts(state)
    except Exception as error:
        close_details, cleanup_warnings = _cleanup(
            state,
            close_fluent=not state.get("keep_open", False),
            close_editor=not state.get("keep_open", False),
        )
        return _archive_failure(
            state,
            operation="archive_mesh",
            error=error,
            result={
                "status": "success",
                "run_id": state["run_id"],
                **close_details,
                **({"warnings": cleanup_warnings} if cleanup_warnings else {}),
            },
        )

    try:
        runtime_evidence = _copy_runtime_evidence(
            state, "success-runtime", exclude_names=set(_PRIMARY_ARTIFACTS)
        )
    except Exception as error:
        runtime_evidence = {}
        _warning(f"Optional runtime evidence was not fully archived: {error}", warnings)
    artifacts.update({"success-runtime:" + name: path for name, path in runtime_evidence.items()})
    final_validation = state.get("fluent_steps", {}).get("final_validation", {})
    result = {
        "status": "success",
        "run_id": state["run_id"],
        "confirmed_geometry": state["confirmed_geometry"],
        "boundary_roles": state["boundary_roles"],
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
    if warnings:
        result["warnings"] = warnings
    return _finish(
        state,
        stage="completed",
        result=result,
        artifacts=artifacts,
        success=True,
        close_fluent=not state.get("keep_open", False),
        close_editor=not state.get("keep_open", False),
    )


def failed(state: PipelineState) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        evidence = _copy_runtime_evidence(state, "failure-runtime")
    except Exception as error:
        evidence = {}
        _warning(f"Failure runtime evidence was not fully archived: {error}", warnings)
    result = {
        "status": "failed",
        "run_id": state["run_id"],
        "failed_step": state.get("failed_step"),
        "error": state.get("error"),
        "error_evidence": state.get("error_evidence", {}),
        "error_detail": state.get("error_detail", {}),
        "repair_decision_source": state.get("repair_decision_source", "unknown"),
        "repair_stop_reason": state.get("repair_stop_reason", ""),
        "repair_rounds": state.get("repair_rounds", 0),
        "total_repair_rounds": state.get("total_repair_rounds", 0),
        "repair_history": state.get("repair_history", []),
        "boundary_roles": state.get("boundary_roles", {}),
        "mesh_requirements": state.get("mesh_requirements", {}),
        "final_execution": state.get("final_execution", {}),
        "quality": state.get("fluent_steps", {}).get("final_validation", {}).get("quality", {}),
        "runtime_evidence": evidence,
    }
    if warnings:
        result["warnings"] = warnings
    return _finish(
        state,
        stage="failed",
        result=result,
        close_fluent=not state.get("keep_open", False),
    )


def cancelled(state: PipelineState) -> dict[str, Any]:
    return _finish(
        state,
        stage="cancelled",
        result={
            "status": "cancelled",
            "run_id": state["run_id"],
            "working_geometry": state["working_geometry"],
            "reason": "User cancelled",
        },
        success=True,
        close_fluent=True,
    )


def has_error(state: PipelineState) -> str:
    return "review_failure" if state.get("error") else "continue"


def review_route(state: PipelineState) -> str:
    action = state["repair_decision"]["action"]
    return "failed" if action == "stop" else "apply_repair"
