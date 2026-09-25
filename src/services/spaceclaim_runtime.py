"""Small SpaceClaim construction helpers shared by CAD workflow nodes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.adapters.spaceclaim import SpaceClaimRunner
from src.config import config_from_state
from src.state import PipelineState


@contextmanager
def open_spaceclaim_reader(
    state: PipelineState, output_dir: Path, *, ui_mode: str | None = None
) -> Iterator[SpaceClaimRunner]:
    """Create a reader for one operation and always close its owned process."""

    runner = SpaceClaimRunner(
        output_dir=output_dir,
        ui_mode=ui_mode or state["ui_mode"],
        config=config_from_state(state),
    )
    try:
        yield runner
    finally:
        runner.close()
