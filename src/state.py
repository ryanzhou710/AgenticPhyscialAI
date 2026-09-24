"""Serializable LangGraph state. Native SpaceClaim/Fluent sessions never enter it."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

UiMode = Literal["gui", "hidden"]
BoundaryRole = Literal["inlet", "outlet", "wall", "symmetry"]


class PipelineState(TypedDict, total=False):
    run_id: str
    run_format_version: int
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
    max_total_repair_rounds: int
    repair_rounds: int
    total_repair_rounds: int
    input_version: int
    status: str
    current_step: str
    failed_step: str
    error: str
    error_evidence: dict[str, Any]
    catalog_path: str
    catalog: dict[str, Any]
    selection_plan: dict[str, Any]
    requested_mesh_requirements: dict[str, Any]
    mesh_requirements: dict[str, Any]
    native_selection: dict[str, Any]
    fluid_volume_mode: Literal["existing_fluid_body", "volume_extract"]
    extraction: dict[str, Any]
    labeling: dict[str, Any]
    cad_validation: dict[str, Any]
    human_response: dict[str, Any]
    human_request: dict[str, Any]
    repair_override: bool
    pending_repair_after_rebuild: bool
    boundary_roles: dict[str, BoundaryRole]
    confirmed_catalog: dict[str, Any]
    fluent_job: dict[str, Any]
    fluent_steps: dict[str, Any]
    repair_decision: dict[str, Any]
    repair_decision_source: Literal["llm", "system"]
    repair_stop_reason: str
    repair_history: list[dict[str, Any]]
    final_execution: dict[str, Any]
    artifacts: dict[str, str]
    result: dict[str, Any]
