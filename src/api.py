"""Public API used by the command-line interface and Python callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.types import Command

from src.adapters.fluent import close_client, has_live_client
from src.adapters.spaceclaim import SpaceClaimRunner
from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.config import RuntimeConfig, config_from_state
from src.graph import build_graph as _build_graph
from src.services.artifacts import RUN_FORMAT_VERSION, create_run_directory, load_json, write_json
from src.services.boundaries import confirm_roles, named_groups
from src.services.execution import _persist


def build_graph(checkpoint_path: str | Path):
    return _build_graph(checkpoint_path)


def _outcome(result: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    interrupts = result.get("__interrupt__", ())
    if interrupts:
        pause = {
            "status": "paused",
            "run_id": result["run_id"],
            "run_dir": str(run_dir),
            "interrupt": interrupts[0].value,
            "repair_rounds": result.get("repair_rounds", 0),
            "total_repair_rounds": result.get("total_repair_rounds", 0),
            "input_version": result.get("input_version", 0),
        }
        write_json(run_dir / "pause.json", pause)
        return pause
    return {
        "status": result.get("status", "unknown"),
        "run_id": result["run_id"],
        "run_dir": str(run_dir),
        "result": result.get("result", {}),
        "fluent_session_open": has_live_client(result["run_id"]),
        "repair_rounds": result.get("repair_rounds", 0),
        "total_repair_rounds": result.get("total_repair_rounds", 0),
        "input_version": result.get("input_version", 0),
    }


def run_pipeline(
    *,
    geometry: str | Path,
    prompt_path: str | Path,
    output_dir: str | Path | None = None,
    overwrite: bool = False,
    ui_mode: str = "hidden",
    keep_open: bool = False,
    max_repair_rounds: int = 10,
    runtime_config: RuntimeConfig | dict | None = None,
) -> dict[str, Any]:
    """Start from a CAD file and UTF-8 prompt file; pause for human confirmation."""
    source = Path(geometry).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".scdoc":
        raise ValueError("geometry must be an existing .scdoc file")
    prompt_file = Path(prompt_path).expanduser().resolve()
    user_prompt = prompt_file.read_text(encoding="utf-8").strip()
    if not user_prompt:
        raise ValueError("prompt is empty")
    if ui_mode not in {"gui", "hidden"}:
        raise ValueError("ui_mode must be gui or hidden")
    if keep_open and ui_mode != "gui":
        raise ValueError("keep_open requires ui_mode=gui")
    if not 0 <= max_repair_rounds <= 100:
        raise ValueError("max_repair_rounds must be between 0 and 100")

    settings = RuntimeConfig.model_validate(runtime_config or {})
    protected = (source, prompt_file)
    run_id, run_dir, runtime_dir = create_run_directory(
        output_dir,
        settings,
        overwrite=overwrite,
        protected_paths=protected,
    )
    checkpoint = run_dir / "checkpoints.sqlite"
    (run_dir / "prompt.txt").write_text(user_prompt + "\n", encoding="utf-8")
    metadata = {
        "run_id": run_id,
        "run_format_version": RUN_FORMAT_VERSION,
        "run_dir": str(run_dir),
        "runtime_dir": str(runtime_dir),
        "source_geometry": str(source),
        "checkpoint": str(checkpoint),
        "ui_mode": ui_mode,
        "keep_open": keep_open,
        "max_repair_rounds": max_repair_rounds,
        "max_total_repair_rounds": 100,
        "overwrite": overwrite,
        "runtime_config": settings.model_dump(mode="json"),
    }
    write_json(run_dir / "run-metadata.json", metadata)
    initial = {
        **metadata,
        "prompt": user_prompt,
        "status": "created",
        "input_version": 0,
        "repair_rounds": 0,
        "total_repair_rounds": 0,
    }
    graph = build_graph(checkpoint)
    result = graph.invoke(
        initial,
        {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 32 + 24 * max_repair_rounds,
        },
    )
    return _outcome(result, run_dir)


def _load_supported_metadata(root: Path) -> dict[str, Any]:
    metadata = load_json(root / "run-metadata.json")
    if metadata.get("run_format_version") != RUN_FORMAT_VERSION:
        raise ValueError(
            "This run was created by an incompatible CFD Agent version; rerun it with that version or start a new run."
        )
    return metadata


def inspect_confirmation(run_dir: str | Path, *, save_current: bool = False) -> dict[str, Any]:
    """Reread the user-edited CAD and report its actual named groups before resume."""
    root = Path(run_dir).expanduser().resolve()
    metadata = _load_supported_metadata(root)
    graph = build_graph(metadata["checkpoint"])
    snapshot = graph.get_state({"configurable": {"thread_id": metadata["run_id"]}})
    state = snapshot.values
    geometry = Path(state["working_geometry"])
    receipt = None
    if save_current:
        try:
            if state["ui_mode"] == "gui":
                receipt = SpaceClaimBuildAdapter.save_current_document(
                    state["labeling"],
                    geometry,
                    timeout_s=config_from_state(state).spaceclaim_timeout_s,
                )
            else:
                receipt = {"ok": True, "saved": False, "mode": "hidden", "path": str(geometry)}
            write_json(root / "artifacts" / "confirmation-save.json", receipt)
        except Exception as error:
            write_json(
                root / "artifacts" / "confirmation-error.json",
                {
                    "stage": "save_current_document",
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            raise
    runner = SpaceClaimRunner(
        output_dir=root / "artifacts" / "confirmation-inspection",
        ui_mode="hidden",
        config=config_from_state(state),
    )
    try:
        catalog, _ = runner.catalog(geometry, render_candidates=False)
    finally:
        runner.close()
    groups = named_groups(catalog)
    inspection = {
        "working_geometry": str(geometry),
        "groups": groups,
        "previous_roles": state.get("boundary_roles", {}),
        "save_receipt": receipt,
    }
    if save_current:
        try:
            inspection["roles"] = confirm_roles(
                catalog=catalog, proposed={}, previous=state.get("boundary_roles", {})
            )
        except Exception as error:
            write_json(
                root / "artifacts" / "confirmation-error.json",
                {
                    "stage": "confirm_saved_groups",
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            raise
    return inspection


def fail_confirmation(run_dir: str | Path, error: Exception) -> dict[str, Any]:
    """End a failed CAD handoff without resuming software or leaving a live Fluent session."""
    root = Path(run_dir).expanduser().resolve()
    metadata = _load_supported_metadata(root)
    graph = build_graph(metadata["checkpoint"])
    config = {"configurable": {"thread_id": metadata["run_id"]}}
    snapshot = graph.get_state(config)
    if "human_confirmation" not in snapshot.next:
        raise ValueError("This run has no pending CAD confirmation")
    close_client(metadata["run_id"])
    result = {
        "status": "failed",
        "run_id": metadata["run_id"],
        "failed_step": "CAD save and confirmation",
        "error": f"{type(error).__name__}: {error}",
        "repair_rounds": snapshot.values.get("repair_rounds", 0),
    }
    update = _persist(
        snapshot.values,
        "failed",
        {
            "status": "failed",
            "failed_step": result["failed_step"],
            "error": result["error"],
            "result": result,
        },
    )
    write_json(root / "result.json", result)
    write_json(root / "artifacts" / "confirmation-failure.json", result)
    graph.update_state(config, update, as_node="failed")
    (root / "pause.json").unlink(missing_ok=True)
    return _outcome({**snapshot.values, **update}, root)


def get_paused_run(run_dir: str | Path) -> dict[str, Any]:
    """Read the current checkpoint interrupt, not an old pause.json file."""
    root = Path(run_dir).expanduser().resolve()
    metadata = _load_supported_metadata(root)
    graph = build_graph(metadata["checkpoint"])
    snapshot = graph.get_state({"configurable": {"thread_id": metadata["run_id"]}})
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    if not interrupts:
        raise ValueError("This run has no pending confirmation")
    return _outcome({**snapshot.values, "__interrupt__": interrupts}, root)


def resume_pipeline(
    *,
    run_dir: str | Path,
    action: str,
    boundary_roles: dict[str, str] | None = None,
    parameter_value: int | float | None = None,
    clarification: str | None = None,
    boundary_replacement: str | None = None,
    boundary_replacements: list[str] | None = None,
) -> dict[str, Any]:
    """Resume a paused run from its SQLite checkpoint."""
    root = Path(run_dir).expanduser().resolve()
    metadata = _load_supported_metadata(root)
    graph = build_graph(metadata["checkpoint"])
    snapshot = graph.get_state({"configurable": {"thread_id": metadata["run_id"]}})
    if "human_intervention" in snapshot.next:
        payload = {
            "action": action,
            "clarification": clarification,
            "parameter_value": parameter_value,
            "boundary_replacement": boundary_replacement,
            "boundary_replacements": boundary_replacements,
        }
    else:
        payload = {"action": action, "boundary_roles": boundary_roles or {}}
    result = graph.invoke(
        Command(resume=payload),
        {
            "configurable": {"thread_id": metadata["run_id"]},
            "recursion_limit": 32 + 24 * metadata["max_repair_rounds"],
        },
    )
    return _outcome(result, root)


def close_run_sessions(run_dir: str | Path) -> None:
    metadata = _load_supported_metadata(Path(run_dir))
    close_client(metadata["run_id"])
