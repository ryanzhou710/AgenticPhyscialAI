"""Graph routing around the failure diagnosis and repair service."""

from __future__ import annotations

from langgraph.types import Command

from src.services.reviewer import diagnose_failure, execute_repair
from src.state import PipelineState


def review_failure(state: PipelineState) -> dict:
    return diagnose_failure(state)


def apply_repair(state: PipelineState) -> Command:
    outcome = execute_repair(state)
    return Command(update=outcome.update, goto=outcome.goto)
