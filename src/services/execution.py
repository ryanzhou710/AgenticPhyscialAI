"""Persistence and evidence shared by graph stages."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.services.artifacts import write_json
from src.state import PipelineState


class HumanInterventionRequired(RuntimeError):
    """Signal a concrete, user-actionable pause rather than a guessed repair."""

    def __init__(self, request: dict[str, Any]):
        self.request = request
        self.evidence = {"human_request": request}
        super().__init__(request["message"])


def _run_dir(state: PipelineState) -> Path:
    return Path(state["run_dir"])


def _persist(state: PipelineState, stage: str, update: dict[str, Any]) -> dict[str, Any]:
    record = {**state, **update, "current_step": stage}
    write_json(_run_dir(state) / "state" / f"{stage}.json", record)
    write_json(_run_dir(state) / "latest-state.json", record)
    return {**update, "current_step": stage}


def _failed(state: PipelineState, stage: str, error: BaseException) -> dict[str, Any]:
    evidence = getattr(error, "evidence", None) or getattr(error, "observation", None) or {}
    return _persist(
        state,
        stage,
        {
            "status": "repair_pending",
            "failed_step": stage,
            "error": f"{type(error).__name__}: {error}",
            "error_evidence": evidence,
        },
    )


def _copy_runtime_evidence(state: PipelineState, folder_name: str) -> dict[str, str]:
    source_root = Path(state["runtime_dir"])
    destination_root = _run_dir(state) / "artifacts" / folder_name
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    suffixes = {".json", ".log", ".png", ".trn", ".scdoc", ".h5"}
    for source in source_root.iterdir():
        if source.is_file() and source.suffix.lower() in suffixes:
            destination = destination_root / source.name
            shutil.copy2(source, destination)
            copied[source.name] = str(destination)
    return copied
