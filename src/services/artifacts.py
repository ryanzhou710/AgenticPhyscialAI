"""Atomic result persistence and run-directory helpers."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import RuntimeConfig

RUN_FORMAT_VERSION = 2

RUN_OUTPUT_NAMES = (
    "artifacts",
    "llm",
    "logs",
    "state",
    "checkpoints.sqlite",
    "checkpoints.sqlite-wal",
    "checkpoints.sqlite-shm",
    "checkpoints.sqlite-journal",
    "run-metadata.json",
    "prompt.txt",
    "pause.json",
    "latest-state.json",
    "result.json",
)


def _clear_run_outputs(run_root: Path, protected_paths: tuple[Path, ...]) -> None:
    targets = [run_root / name for name in RUN_OUTPUT_NAMES]
    targets += [run_root / (name + ".tmp") for name in RUN_OUTPUT_NAMES if name.endswith(".json")]
    protected = [path.resolve() for path in protected_paths]
    # Validate the entire cleanup before removing any output.
    for target in targets:
        resolved = target.resolve()
        if not resolved.is_relative_to(run_root):
            raise ValueError(f"Output cleanup escapes the selected directory: {target}")
        for path in protected:
            if (
                path == resolved
                or path.is_relative_to(resolved)
                or (path.is_dir() and resolved.is_relative_to(path))
            ):
                raise ValueError(f"Output overwrite conflicts with an input or source: {path}")
    for target in targets:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def write_json(path: str | Path, value: Any) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def create_run_directory(
    root: str | Path | None = None,
    config: RuntimeConfig | None = None,
    *,
    overwrite: bool = False,
    protected_paths: tuple[Path, ...] = (),
) -> tuple[str, Path, Path]:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_root = Path(root).expanduser().resolve() if root else Path.cwd() / "runs" / run_id
    if overwrite and root is not None and run_root.is_dir():
        _clear_run_outputs(run_root, (*protected_paths, Path(__file__).resolve().parents[1]))
    else:
        run_root.mkdir(parents=True, exist_ok=False)
    runtime_base = (config or RuntimeConfig()).staging_root() / "runs"
    runtime = runtime_base / run_id
    runtime.mkdir(parents=True, exist_ok=False)
    for name in ("artifacts", "llm", "logs"):
        (run_root / name).mkdir()
    return run_id, run_root, runtime


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
