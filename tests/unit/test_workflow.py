"""Graph structure, CAD interruption, cancellation and repair routing."""

import json
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from src.graph import build_graph
from src.nodes import confirmation, fluent, review
from src.nodes.confirmation import human_confirmation
from src.nodes.review import review_failure
from src.services.contracts import RepairDecision
from src.state import PipelineState
from src.workers.mesh_job import MeshJob
from src.workers.repair_protocol import RepairState


def test_graph_contains_spaceclaim_and_fluent_steps(tmp_path: Path):
    graph = build_graph(tmp_path / "checkpoints.sqlite")
    names = set(graph.get_graph().nodes)
    assert {"extract_volume", "label_faces", "human_confirmation", "rebuild_fluent"} <= names
    assert {
        "import_geometry",
        "local_sizing",
        "surface_mesh",
        "describe_geometry",
        "update_boundaries",
        "update_regions",
        "boundary_layers",
        "volume_mesh",
    } <= names


def test_human_confirmation_interrupt_can_resume(tmp_path: Path):
    (tmp_path / "state").mkdir()
    builder = StateGraph(PipelineState)
    builder.add_node("confirm", human_confirmation)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    graph = builder.compile(
        checkpointer=SqliteSaver(
            sqlite3.connect(tmp_path / "checkpoint.sqlite", check_same_thread=False)
        )
    )
    config = {"configurable": {"thread_id": "interrupt-test"}}
    initial = {
        "run_id": "interrupt-test",
        "run_dir": str(tmp_path),
        "working_geometry": str(tmp_path / "labeled.scdoc"),
        "boundary_roles": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
        "labeling": {"process_id": 1},
    }
    paused = graph.invoke(initial, config)
    assert paused["__interrupt__"]
    resumed = graph.invoke(Command(resume={"action": "approve", "boundary_roles": {}}), config)
    assert resumed["human_response"]["action"] == "approve"


def test_intervention_rebuild_replays_preceding_fluent_steps_before_repair(monkeypatch):
    from src.nodes import fluent as fluent_nodes

    monkeypatch.setattr(fluent_nodes, "launch_fluent", lambda state: {"error": ""})
    assert fluent_nodes.rebuild_fluent({}).goto == "import_geometry"
    pending = fluent_nodes.fluent_step("boundary_layers")(
        {"pending_repair_after_rebuild": True, "failed_step": "boundary_layers"}
    )
    assert pending.goto == "apply_repair"
    assert fluent_nodes.validate_mesh(
        {"pending_repair_after_rebuild": True, "failed_step": "final_validation"}
    ).goto == "apply_repair"


def test_zero_repair_budget_stops_without_calling_model(tmp_path: Path):
    (tmp_path / "state").mkdir()
    state = {
        "run_id": "budget-test",
        "run_dir": str(tmp_path),
        "max_repair_rounds": 0,
        "repair_rounds": 0,
        "failed_step": "surface_mesh",
        "error": "failed",
    }
    result = review_failure(state)
    assert result["repair_decision"]["action"] == "stop"
    assert result["repair_decision_source"] == "system"
    assert result["repair_stop_reason"] == "repair_budget_exhausted"


def test_llm_requested_stop_records_its_decision_source(tmp_path: Path, monkeypatch):
    from src.services import reviewer

    class Client:
        def invoke(self, **kwargs):
            return RepairDecision(
                action="stop",
                target_step="query_geometry",
                diagnosis="Need a clearer CAD direction.",
                evidence="The requested direction is ambiguous.",
            )

    monkeypatch.setattr(reviewer.GroundingLLMClient, "from_runtime_config", lambda **kwargs: Client())
    result = reviewer.diagnose_failure(
        {
            "run_id": "llm-stop",
            "run_dir": str(tmp_path),
            "runtime_dir": str(tmp_path),
            "max_repair_rounds": 1,
            "repair_rounds": 0,
            "failed_step": "query_geometry",
            "error": "ambiguous direction",
        }
    )

    assert result["repair_decision_source"] == "llm"
    assert result["repair_stop_reason"] == "llm_requested_stop"


def test_failed_result_keeps_compatibility_with_old_state_without_stop_fields(tmp_path: Path):
    from src.nodes.results import failed

    result = failed(
        {
            "run_id": "old-state",
            "run_dir": str(tmp_path),
            "runtime_dir": str(tmp_path),
            "keep_open": True,
            "failed_step": "extract_volume",
            "error": "legacy failure",
        }
    )

    assert result["result"]["repair_decision_source"] == "unknown"
    assert result["result"]["repair_stop_reason"] == ""


def test_fluent_repair_reports_earliest_invalidated_step(tmp_path: Path):
    geometry = tmp_path / "confirmed.scdoc"
    geometry.write_bytes(b"placeholder")
    job = MeshJob.from_dict(
        {
            "geometry_path": str(geometry),
            "length_unit": "mm",
            "boundaries": {"inlet": ["inlet"], "outlet": ["outlet"], "wall": ["wall"]},
            "global_size": 10.0,
            "local_refinements": [],
            "boundary_layers": {},
            "quality": {"min_orthogonal_quality": 0.1, "max_skewness": 0.95},
        }
    )
    controls = RepairState(job)
    step, _ = controls.apply("set_global_size", {"value": 5.0}, set())
    assert step == "surface_mesh"
    assert controls.global_size == 5.0


def test_reviewer_retry_cannot_silently_ignore_parameters():
    with pytest.raises(ValidationError):
        RepairDecision(
            diagnosis="retry",
            evidence="error",
            action="retry_step",
            target_step="verify_selection",
            parameters={"views": ["Isometric"]},
        )


def test_reviewer_reference_requires_the_executable_fields():
    with pytest.raises(ValidationError):
        RepairDecision(
            diagnosis="reference",
            evidence="error",
            action="replace_object_reference",
            target_step="verify_selection",
            parameters={"reference_view": "Isometric"},
        )
    decision = RepairDecision(
        diagnosis="reference",
        evidence="error",
        action="replace_object_reference",
        target_step="verify_selection",
        parameters={"field": "seed_inner_wall_id", "candidate_id": "F0001"},
    )
    assert decision.parameters["field"] == "seed_inner_wall_id"


def test_fluent_review_uses_structured_requirements_without_old_cad_notes():
    from src.services.reviewer import fluent_review_inputs

    state = {
        "mesh_requirements": {"global_size": {"value": 10}, "notes": ["old CAD instructions"]},
        "fluent_job": {
            "parameter_sources": {"notes": ["old CAD instructions"]},
            "boundaries": {"inlet": ["edited_name"]},
        },
    }
    requirements, job = fluent_review_inputs(state)
    assert "notes" not in requirements
    assert "notes" not in job["parameter_sources"]
    assert job["boundaries"]["inlet"] == ["edited_name"]
    assert state["mesh_requirements"]["notes"]


def test_gui_editing_window_is_kept_without_final_keep_open(tmp_path, monkeypatch):
    from src.nodes import cad

    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def label_faces(self, **kwargs):
            calls.append(kwargs)
            return {"groups": []}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    result = cad.label_faces(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "ui_mode": "gui",
            "keep_open": False,
            "working_geometry": "extracted.scdoc",
            "extraction": {},
        }
    )
    assert not result["error"]
    assert calls[0]["keep_open"] is True


def test_cancel_ends_graph_without_review_or_cad_reload(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("cancel must not invoke software or Reviewer")

    monkeypatch.setattr(confirmation, "reload_confirmed_cad", unexpected)
    monkeypatch.setattr(fluent, "launch_fluent", unexpected)
    monkeypatch.setattr(review, "review_failure", unexpected)
    checkpoint = tmp_path / "checkpoint.sqlite"
    config = {"configurable": {"thread_id": "cancel"}}
    graph = build_graph(checkpoint)
    initial = {
        "run_id": "cancel",
        "run_dir": str(tmp_path),
        "working_geometry": "edited.scdoc",
        "boundary_roles": {},
        "labeling": {},
        "error": "",
    }
    graph.update_state(config, initial, as_node="validate_cad")
    assert graph.invoke(None, config)["__interrupt__"]
    resumed = build_graph(checkpoint).invoke(Command(resume={"action": "cancel"}), config)
    assert resumed["status"] == "cancelled"
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "cancelled"


@pytest.mark.parametrize(
    "failed,action,target,parameters",
    [
        ("surface_mesh", "retry_step", "prepare", {}),
        ("verify_selection", "retry_step", "human_confirmation", {}),
        ("surface_mesh", "retry_step", "final_validation", {}),
        ("surface_mesh", "retry_step", "import_geometry", {}),
        (
            "surface_mesh",
            "replace_object_reference",
            "verify_selection",
            {"field": "seed_inner_wall_id", "candidate_id": "F1"},
        ),
        ("extract_volume", "set_global_size", "surface_mesh", {"value": 1}),
        ("surface_mesh", "set_layer_count", "boundary_layers", {"value": 3}),
        ("query_geometry", "return_to_human", "human_confirmation", {}),
    ],
)
def test_invalid_repair_routes_stop_before_software(
    monkeypatch, failed, action, target, parameters
):
    from src.services import reviewer

    monkeypatch.setattr(reviewer, "get_client", lambda *args: pytest.fail("no software call"))
    outcome = reviewer.execute_repair(
        {
            "failed_step": failed,
            "repair_rounds": 2,
            "repair_decision": RepairDecision(
                action=action,
                target_step=target,
                parameters=parameters,
                diagnosis="offline",
                evidence="offline",
            ).model_dump(),
        }
    )
    assert outcome.goto == "failed"
    assert "Invalid repair route" in outcome.update["error"]


@pytest.mark.parametrize(
    "action,target,parameters,expected",
    [
        ("retry_step", "volume_mesh", {}, "volume_mesh"),
        ("set_global_size", "surface_mesh", {"value": 1}, "surface_mesh"),
        ("set_global_size", "volume_mesh", {"value": 1}, "surface_mesh"),
    ],
)
def test_fluent_repairs_resume_failed_or_affected_step(
    monkeypatch, action, target, parameters, expected
):
    from types import SimpleNamespace

    from src.services import reviewer

    calls = []

    def call(operation, data):
        calls.append(operation)
        return {"resume": expected}

    monkeypatch.setattr(reviewer, "get_client", lambda *args: SimpleNamespace(call=call))
    outcome = reviewer.execute_repair(
        {
            "run_id": "route",
            "runtime_dir": "unused",
            "failed_step": "volume_mesh",
            "repair_history": [{}],
            "repair_decision": RepairDecision(
                action=action,
                target_step=target,
                parameters=parameters,
                diagnosis="offline",
                evidence="offline",
            ).model_dump(),
        }
    )
    assert outcome.goto == expected
    assert calls == ["repair"]


def test_prepare_retry_preserves_budget_until_exhaustion(tmp_path, monkeypatch):
    from src.nodes import cad
    from src.services import reviewer
    from src.services.execution import _failed

    source = tmp_path / "input.scdoc"
    source.write_bytes(b"offline")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    copies = []
    original_copy = cad.shutil.copy2

    def copy(source, destination):
        copies.append(destination)
        if len(copies) == 1:
            raise OSError("transient copy failure")
        return original_copy(source, destination)

    monkeypatch.setattr(cad.shutil, "copy2", copy)
    monkeypatch.setattr(
        cad,
        "query_geometry",
        lambda state: _failed(state, "query_geometry", RuntimeError("query failed")),
    )
    decisions = []

    class Model:
        def invoke(self, **kwargs):
            failed = json.loads(kwargs["user_prompt"])["failed_step"]
            decisions.append(failed)
            return RepairDecision(
                action="retry_step", target_step=failed, diagnosis="retry", evidence="offline"
            )

    monkeypatch.setattr(reviewer.GroundingLLMClient, "from_codex_oauth", lambda **kwargs: Model())
    graph = build_graph(tmp_path / "checkpoint.sqlite")
    result = graph.invoke(
        {
            "run_id": "budget",
            "run_dir": str(tmp_path),
            "runtime_dir": str(runtime),
            "source_geometry": str(source),
            "max_repair_rounds": 2,
            "keep_open": False,
        },
        {"configurable": {"thread_id": "budget"}},
    )
    assert result["status"] == "failed"
    assert decisions == ["prepare", "query_geometry"]
    assert copies.count(runtime / "original.scdoc") == 2
    assert result["repair_rounds"] == 2
    assert [row["round"] for row in result["repair_history"]] == [1, 2]
    assert result["repair_decision"]["diagnosis"] == "Repair budget exhausted"


@pytest.mark.parametrize("keep_open", [False, True])
def test_cancel_after_fluent_returns_to_human_closes_owned_session(
    tmp_path, monkeypatch, keep_open
):
    from types import SimpleNamespace

    from src.adapters import fluent as transport

    closed = []
    client = SimpleNamespace(_broken=False, process=SimpleNamespace(poll=lambda: None))

    def close():
        closed.append("returned")
        client._broken = True

    client.close = close
    monkeypatch.setattr(transport, "_CLIENTS", {"returned": client})

    def unexpected(*args, **kwargs):
        pytest.fail("cancel must not restart software, reload CAD or diagnose")

    monkeypatch.setattr(confirmation, "reload_confirmed_cad", unexpected)
    monkeypatch.setattr(fluent, "launch_fluent", unexpected)
    monkeypatch.setattr(review, "review_failure", unexpected)
    graph = build_graph(tmp_path / "checkpoint.sqlite")
    config = {"configurable": {"thread_id": "returned"}}
    graph.update_state(
        config,
        {
            "run_id": "returned",
            "run_dir": str(tmp_path),
            "keep_open": keep_open,
            "working_geometry": "edited.scdoc",
            "labeling": {"groups": []},
            "boundary_roles": {},
            "failed_step": "surface_mesh",
            "error": "native failure",
            "repair_rounds": 1,
            "repair_history": [{"round": 1}],
            "repair_decision": RepairDecision(
                action="return_to_human",
                target_step="human_confirmation",
                diagnosis="edit CAD",
                evidence="offline",
            ).model_dump(),
        },
        as_node="review_failure",
    )
    assert graph.invoke(None, config)["__interrupt__"]
    # A CAD-revision pause must archive and close the old Fluent session.  A
    # later confirmation starts a fresh session from the saved CAD.
    assert not transport.has_live_client("returned")
    result = graph.invoke(Command(resume={"action": "cancel"}), config)
    assert result["status"] == "cancelled"
    assert closed == ["returned"]
    assert not transport.has_live_client("returned")
    assert result["repair_rounds"] == 1
