"""Persistence and evidence shared by graph stages."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from src.services.artifacts import write_json
from src.services.errors import make_error_detail
from src.state import PipelineState


class HumanInterventionRequired(RuntimeError):
    """Signal a concrete, user-actionable pause rather than a guessed repair."""

    def __init__(self, request: dict[str, Any]):
        self.request = request
        self.evidence = {"human_request": request}
        super().__init__(request["message"])


def _run_dir(state: PipelineState) -> Path:
    return Path(state["run_dir"])


def cad_restart_update(state: PipelineState, stage: str) -> dict[str, Any]:
    """Invalidate the rerun's products, preserving its inputs and failure history."""
    stages = (
        ("prepare", ()),
        ("query_geometry", ("catalog",)),
        ("understand_prompt", ("selection_plan",)),
        ("verify_selection", ("native_selection",)),
        ("extract_volume", ("extraction", "extraction_catalog")),
        ("select_fluid_body", ("target_body", "target_catalog")),
        ("plan_boundary_groups", ("boundary_group_plan", "mesh_requirements", "parsed_mesh_requirements")),
        ("label_faces", ("labeling", "boundary_roles")),
        ("validate_cad", ("cad_validation",)),
    )
    names = [name for name, _ in stages]
    if stage not in names:
        return {}
    index = names.index(stage)
    update = {field: {} for _, fields in stages[index:] for field in fields}
    update.update({
        "confirmed_geometry": "", "human_response": {}, "human_request": {},
        "repair_approved": False, "fluent_job": {},
        "fluent_steps": {}, "final_execution": {}, "result": {},
    })
    if index <= names.index("extract_volume"):
        update["working_geometry"] = str(Path(state["runtime_dir"]) / "original.scdoc")
    elif stage == "select_fluid_body":
        update["working_geometry"] = state["extraction"]["candidate_geometry"]
    elif stage in {"plan_boundary_groups", "label_faces"}:
        update["working_geometry"] = str(Path(state["runtime_dir"]) / "target-fluid.scdoc")
    return update


def _persist(state: PipelineState, stage: str, update: dict[str, Any]) -> dict[str, Any]:
    record = {**state, **update, "current_step": stage}
    write_json(
        _run_dir(state) / "state" / f"{stage}.json",
        {"current_step": stage, "update": update},
    )
    write_json(_run_dir(state) / "latest-state.json", record)
    return {**update, "current_step": stage}


def _succeeded(state: PipelineState, stage: str, update: dict[str, Any]) -> dict[str, Any]:
    """Persist a completed stage without leaving stale failure diagnostics active."""

    successful_update = {
        **update,
        "error": "",
        "error_detail": {},
        "error_evidence": {},
    }
    if state.get("failed_step") == stage:
        successful_update["failed_step"] = ""
        successful_update["repair_stop_reason"] = ""
    return _persist(state, stage, successful_update)


def _failed(state: PipelineState, stage: str, error: BaseException) -> dict[str, Any]:
    evidence = getattr(error, "evidence", None) or getattr(error, "observation", None) or {}
    artifact_path = _run_dir(state) / "artifacts" / f"{stage}-error.json"
    detail = make_error_detail(stage, error, evidence=evidence, evidence_path=artifact_path)
    try:
        write_json(
            artifact_path,
            {
                "error_detail": detail,
                "error": f"{type(error).__name__}: {error}",
                "error_evidence": evidence,
            },
        )
    except Exception as persistence_error:
        detail["evidence_write_error"] = f"{type(persistence_error).__name__}: {persistence_error}"
    update = {
        "status": "repair_pending",
        "failed_step": stage,
        "error": f"{type(error).__name__}: {error}",
        "error_evidence": evidence,
        "error_detail": detail,
    }
    try:
        return _persist(state, stage, update)
    except Exception as persistence_error:
        # The graph can still route the failure even when the run directory is
        # unavailable.  Do not lose the original error because its artifact
        # could not be written.
        detail["state_write_error"] = f"{type(persistence_error).__name__}: {persistence_error}"
        print(
            "[error-record-write-failed] " + detail["state_write_error"],
            file=sys.stderr,
            flush=True,
        )
        return {**update, "error_detail": detail, "current_step": stage}


def _copy_runtime_evidence(
    state: PipelineState, folder_name: str, *, exclude_names: set[str] | None = None
) -> dict[str, str]:
    source_root = Path(state["runtime_dir"])
    artifact_root = _run_dir(state) / "artifacts"
    destination_root = artifact_root / folder_name
    copy_index = 2
    while destination_root.exists():
        destination_root = artifact_root / f"{folder_name}-{copy_index}"
        copy_index += 1
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    suffixes = {".json", ".log", ".png", ".trn", ".scdoc", ".h5"}
    excluded = exclude_names or set()
    for source in source_root.iterdir():
        if (
            source.is_file()
            and source.name not in excluded
            and source.suffix.lower() in suffixes
        ):
            destination = destination_root / source.name
            shutil.copy2(source, destination)
            copied[source.name] = str(destination)
    return copied
