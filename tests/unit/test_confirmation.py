"""Targeted terminal/confirmation tests; no model or Ansys is launched."""

import json
from types import SimpleNamespace

import pytest
from langgraph.types import Command

from src import api, cli
from src.adapters import fluent as transport
from src.adapters import spaceclaim_build
from src.services.artifacts import RUN_FORMAT_VERSION
from src.services.terminal import progress_node


def paused(tmp_path):
    return {
        "status": "paused",
        "run_dir": str(tmp_path),
        "interrupt": {
            "kind": "cad_confirmation",
            "message": "Review CAD",
            "working_geometry": str(tmp_path / "working.scdoc"),
        },
    }


@pytest.mark.parametrize("answer", ["no", "NO", " No "])
def test_no_never_saves_or_inspects(monkeypatch, tmp_path, answer):
    calls = []
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setattr(cli, "inspect_confirmation", lambda *a, **kw: pytest.fail("no inspection"))
    monkeypatch.setattr(cli, "close_run_sessions", lambda *a: pytest.fail("no close"))
    monkeypatch.setattr(
        cli, "resume_pipeline", lambda **kw: calls.append(kw) or {"status": "cancelled"}
    )
    assert cli._interactive_resume(paused(tmp_path), True)["status"] == "cancelled"
    assert calls == [{"run_dir": str(tmp_path), "action": "cancel"}]


@pytest.mark.parametrize("saved", [True, False])
def test_yes_saves_before_resume_and_has_no_role_questions(monkeypatch, tmp_path, capsys, saved):
    events = []
    answers = iter(["", "later", "continue", " YES "])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def inspect(run_dir, *, save_current):
        events.append(("save", save_current))
        return {"roles": {"in": "inlet", "out": "outlet"}, "save_receipt": {"saved": saved}}

    def resume(**kwargs):
        events.append(("resume", kwargs))
        return {"status": "success"}

    monkeypatch.setattr(cli, "inspect_confirmation", inspect)
    monkeypatch.setattr(cli, "resume_pipeline", resume)
    assert cli._interactive_resume(paused(tmp_path), False)["status"] == "success"
    assert events[0] == ("save", True)
    assert events[1][1]["boundary_roles"] == {"in": "inlet", "out": "outlet"}
    output = capsys.readouterr().out
    assert output.count("Please enter yes or no") == 3
    assert ("Unsaved CAD changes saved" if saved else "no save needed") in output


@pytest.mark.parametrize(
    "message", ["Native save failed", "Editing session lost", "Boundary roles are missing"]
)
@pytest.mark.parametrize("keep_open", [False, True])
def test_handoff_failure_stops_without_resume(monkeypatch, tmp_path, capsys, message, keep_open):
    checkpoint = tmp_path / "checkpoints.sqlite"
    metadata = {
        "checkpoint": str(checkpoint),
        "run_id": "handoff-test",
        "run_format_version": RUN_FORMAT_VERSION,
    }
    (tmp_path / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    closed = []
    client = SimpleNamespace(_broken=False, process=SimpleNamespace(poll=lambda: None))

    def close():
        closed.append(metadata["run_id"])
        client._broken = True

    client.close = close
    monkeypatch.setattr(transport, "_CLIENTS", {metadata["run_id"]: client})
    graph = api.build_graph(checkpoint)
    config = {"configurable": {"thread_id": metadata["run_id"]}}
    graph.update_state(
        config,
        {
            "run_id": metadata["run_id"],
            "run_dir": str(tmp_path),
            "working_geometry": "working.scdoc",
            "boundary_roles": {},
            "labeling": {},
            "repair_rounds": 2,
            "keep_open": keep_open,
            "error": "",
        },
        as_node="validate_cad",
    )
    assert graph.invoke(None, config)["__interrupt__"]
    outcome = api.get_paused_run(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")

    def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(cli, "inspect_confirmation", fail)
    monkeypatch.setattr(cli, "resume_pipeline", lambda **kw: pytest.fail("must not resume"))
    result = cli._interactive_resume(outcome, keep_open)
    assert result["status"] == "failed"
    assert closed == [metadata["run_id"]]
    assert result["fluent_session_open"] is False
    for path in (tmp_path / "result.json", tmp_path / "artifacts" / "confirmation-failure.json"):
        assert json.loads(path.read_text(encoding="utf-8")) == result["result"]
    assert message in result["result"]["error"]
    snapshot = graph.get_state(config)
    assert snapshot.values["status"] == "failed"
    assert snapshot.values["repair_rounds"] == 2
    assert snapshot.next == ()
    assert not (tmp_path / "pause.json").exists()
    with pytest.raises(ValueError, match="no pending confirmation"):
        api.get_paused_run(tmp_path)
    assert "Fluent was not started" not in capsys.readouterr().out


def test_summary_printed_before_keep_open_wait(monkeypatch, tmp_path, capsys):
    def wait():
        output = capsys.readouterr().out
        assert "Overall status: Success" in output
        assert "mesh.msh.h5" in output
        assert "Diagnosis rounds: 2" in output
        return False

    closed = []
    monkeypatch.setattr(cli, "has_live_client", lambda run_id: wait())
    monkeypatch.setattr(cli, "close_run_sessions", lambda root: closed.append(root))
    cli._interactive_resume(
        {
            "status": "success",
            "run_id": "kept-open",
            "run_dir": str(tmp_path),
            "fluent_session_open": True,
            "result": {"mesh": "mesh.msh.h5", "repair_rounds": 2},
        },
        True,
    )
    assert closed == [str(tmp_path)]


def test_resume_command_uses_same_yes_no(monkeypatch, tmp_path):
    (tmp_path / "run-metadata.json").write_text(json.dumps({"keep_open": False}))
    monkeypatch.setattr(cli, "get_paused_run", lambda path: paused(tmp_path))
    monkeypatch.setattr("builtins.input", lambda message: "no")
    calls = []
    monkeypatch.setattr(
        cli, "resume_pipeline", lambda **kw: calls.append(kw) or {"status": "cancelled"}
    )
    assert cli.main(["resume", "--run-dir", str(tmp_path)]) == 0
    assert calls[0]["action"] == "cancel"


def test_progress_does_not_change_results_or_call_diagnosis_a_repair(capsys):
    diagnosis = {"repair_decision": {"action": "set_layer_count", "diagnosis": "Wrong layer count"}}
    assert (
        progress_node("review_failure", lambda state: diagnosis)({"error": "old error"})
        is diagnosis
    )
    action = Command(update={"error": ""}, goto="boundary_layers")
    assert progress_node("apply_repair", lambda state: action)(diagnosis) is action
    progress_node("boundary_layers", lambda state: {"error": ""})(
        {"failed_step": "boundary_layers", "repair_rounds": 1}
    )
    output = capsys.readouterr().out
    assert "[Done] Diagnosis:" in output and "Recovery is not yet verified" in output
    assert "(rerun)" in output
    assert "repair successful" not in output.lower()


def test_failed_terminal_node_never_says_complete(capsys):
    progress_node("failed", lambda state: {"status": "failed"})({"error": "failure"})
    assert "complete" not in capsys.readouterr().out


def test_inspect_saves_before_fresh_read_and_reuses_roles(monkeypatch, tmp_path):
    from src.services.geometry_models import GeometryCatalog

    catalog = GeometryCatalog.model_construct(
        native_catalog={
            "internal": {
                "raw_groups": [
                    {"raw_name": "in", "member_ids": ["a"]},
                    {"raw_name": "out", "member_ids": ["b"]},
                ]
            }
        }
    )
    events = []
    state = {
        "working_geometry": str(tmp_path / "working.scdoc"),
        "ui_mode": "gui",
        "labeling": {"session": "original"},
        "boundary_roles": {"in": "inlet", "out": "outlet"},
    }
    (tmp_path / "run-metadata.json").write_text(
        json.dumps(
            {"checkpoint": "unused", "run_id": "run", "run_format_version": RUN_FORMAT_VERSION}
        )
    )
    monkeypatch.setattr(
        api,
        "build_graph",
        lambda path: SimpleNamespace(get_state=lambda config: SimpleNamespace(values=state)),
    )
    monkeypatch.setattr(
        api.SpaceClaimBuildAdapter,
        "save_current_document",
        lambda *a, **kw: events.append("save") or {"ok": True, "saved": True},
    )

    class Reader:
        def __init__(self, **kwargs):
            events.append("open reader")

        def catalog(self, *args, **kwargs):
            return catalog, None

        def close(self):
            events.append("close reader")

    monkeypatch.setattr(api, "SpaceClaimRunner", Reader)
    result = api.inspect_confirmation(tmp_path, save_current=True)
    assert events == ["save", "open reader", "close reader"]
    assert result["roles"] == state["boundary_roles"]
    state["boundary_roles"] = {"in": "inlet"}
    with pytest.raises(ValueError, match="roles are missing"):
        api.inspect_confirmation(tmp_path, save_current=True)


def test_save_protocol_failure_is_not_retried(monkeypatch, tmp_path):
    request_path = tmp_path / "request.json"
    monkeypatch.setattr(spaceclaim_build, "process_creation_time", lambda pid: 123)

    def receive(_):
        request = json.loads(request_path.read_text())
        from pathlib import Path

        Path(request["response"]).write_text(
            json.dumps({"id": request["id"], "ok": False, "error": "Native save failed"})
        )

    monkeypatch.setattr(spaceclaim_build.time, "sleep", receive)
    with pytest.raises(spaceclaim_build.SpaceClaimError, match="Native save failed"):
        spaceclaim_build.SpaceClaimBuildAdapter.save_current_document(
            {
                "process_id": 1,
                "process_creation_time": 123,
                "save_bridge": {"request_path": str(request_path)},
            },
            tmp_path / "working.scdoc",
            timeout_s=5,
        )
    assert len(list(tmp_path.glob("*-response.json"))) == 1


def test_save_timeout_sends_only_one_request(monkeypatch, tmp_path):
    ticks = iter([0, 1, 2])
    monkeypatch.setattr(spaceclaim_build.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(spaceclaim_build, "process_creation_time", lambda pid: 123)
    with pytest.raises(TimeoutError, match="Fluent was not started"):
        spaceclaim_build.SpaceClaimBuildAdapter.save_current_document(
            {
                "process_id": 1,
                "process_creation_time": 123,
                "save_bridge": {"request_path": str(tmp_path / "request.json")},
            },
            tmp_path / "working.scdoc",
            timeout_s=1,
        )
    assert len(list(tmp_path.glob("request.json"))) == 1


def test_checkpoint_resume_reads_actual_interrupt_and_no_ends_graph(tmp_path):
    checkpoint = tmp_path / "checkpoints.sqlite"
    metadata = {
        "checkpoint": str(checkpoint),
        "run_id": "pause-test",
        "max_repair_rounds": 10,
        "run_format_version": RUN_FORMAT_VERSION,
    }
    (tmp_path / "run-metadata.json").write_text(json.dumps(metadata))
    graph = api.build_graph(checkpoint)
    config = {"configurable": {"thread_id": metadata["run_id"]}}
    graph.update_state(
        config,
        {
            "run_id": metadata["run_id"],
            "run_dir": str(tmp_path),
            "working_geometry": "actual-working.scdoc",
            "boundary_roles": {},
            "labeling": {},
            "error": "",
            "repair_rounds": 3,
        },
        as_node="validate_cad",
    )
    assert graph.invoke(None, config)["__interrupt__"]
    (tmp_path / "pause.json").write_text(json.dumps({"interrupt": {"working_geometry": "stale"}}))
    outcome = api.get_paused_run(tmp_path)
    assert outcome["interrupt"]["working_geometry"] == "actual-working.scdoc"
    assert outcome["repair_rounds"] == 3
    assert api.resume_pipeline(run_dir=tmp_path, action="cancel")["status"] == "cancelled"
    with pytest.raises(ValueError, match="no pending confirmation"):
        api.get_paused_run(tmp_path)


@pytest.mark.parametrize("ui_mode", ["gui", "hidden"])
def test_confirmation_save_failure_never_opens_disk_copy(monkeypatch, tmp_path, ui_mode):
    state = {"working_geometry": "working.scdoc", "ui_mode": ui_mode, "labeling": {}}
    (tmp_path / "run-metadata.json").write_text(
        json.dumps(
            {"checkpoint": "unused", "run_id": "run", "run_format_version": RUN_FORMAT_VERSION}
        )
    )
    monkeypatch.setattr(
        api,
        "build_graph",
        lambda path: SimpleNamespace(get_state=lambda config: SimpleNamespace(values=state)),
    )
    events = []

    def save(*args, **kwargs):
        events.append("save")
        raise RuntimeError("lost editing session")

    def reader(**kwargs):
        events.append("read")
        raise RuntimeError("test reader reached")

    monkeypatch.setattr(api.SpaceClaimBuildAdapter, "save_current_document", save)
    monkeypatch.setattr(api, "SpaceClaimRunner", reader)
    with pytest.raises(
        RuntimeError, match="lost editing session" if ui_mode == "gui" else "reader reached"
    ):
        api.inspect_confirmation(tmp_path, save_current=True)
    assert events == (["save"] if ui_mode == "gui" else ["read"])
