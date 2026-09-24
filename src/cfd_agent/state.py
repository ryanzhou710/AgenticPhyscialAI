"""Serializable LangGraph state. Native SpaceClaim/Fluent sessions never enter it."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

UiMode = Literal["gui", "hidden"]
BoundaryRole = Literal["inlet", "outlet", "wall", "symmetry"]


class PipelineState(TypedDict, total=False):
    run_id: str
    runtime_config: dict[str, Any]
    run_dir: str
    runtime_dir: str
    source_geometry: str
    working_geometry: str
    confirmed_geometry: str
    prompt: str
    ui_mode: UiMode
    keep_open: bool
    max_repair_rounds: int
    repair_rounds: int
    status: str
    current_step: str
    failed_step: str
    error: str
    error_evidence: dict[str, Any]
    catalog_path: str
    catalog: dict[str, Any]
    selection_plan: dict[str, Any]
    mesh_requirements: dict[str, Any]
    native_selection: dict[str, Any]
    fluid_volume_mode: Literal["existing_fluid_body", "volume_extract"]
    extraction: dict[str, Any]
    labeling: dict[str, Any]
    cad_validation: dict[str, Any]
    human_response: dict[str, Any]
    boundary_roles: dict[str, BoundaryRole]
    confirmed_catalog: dict[str, Any]
    fluent_job: dict[str, Any]
    fluent_steps: dict[str, Any]
    repair_decision: dict[str, Any]
    repair_decision_source: Literal["llm", "system"]
    repair_stop_reason: str
    repair_history: list[dict[str, Any]]
    parameter_confirmation: dict[str, Any]
    artifacts: dict[str, str]
    result: dict[str, Any]
