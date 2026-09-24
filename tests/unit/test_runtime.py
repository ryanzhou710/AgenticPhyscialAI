"""Run options, output replacement and owned process communication."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import api, cli
from src.adapters.fluent import FluentClient, FluentWorkerError
from src.config import RuntimeConfig
from src.services.artifacts import create_run_directory


def test_close_does_not_target_reused_or_unowned_pid(monkeypatch):
    from src.adapters import windows_process

    monkeypatch.setattr(windows_process, "process_creation_time", lambda pid: 999)
    result = windows_process.request_window_close({"process_id": 1, "process_creation_time": 111})
    assert result["requested"] is False


def client_with_program(tmp_path, monkeypatch, program, timeout=2):
    original = subprocess.Popen

    def local_worker(command, **kwargs):
        return original([sys.executable, "-u", "-c", program], **kwargs)

    monkeypatch.setattr("src.adapters.fluent.subprocess.Popen", local_worker)
    return FluentClient(tmp_path, RuntimeConfig(fluent_operation_timeout_s=timeout))


def test_ipc_drains_stderr_before_response(tmp_path, monkeypatch):
    client = client_with_program(
        tmp_path,
        monkeypatch,
        """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    sys.stderr.write('x' * 100000 + '\\n'); sys.stderr.flush()
    print(json.dumps({'id': request['id'], 'ok': True, 'result': {'done': True}}), flush=True)
    if request['operation'] == 'close': break
""",
    )
    try:
        assert client.call("observe")["done"]
    finally:
        client.close()
    for reader in client._readers:
        reader.join(timeout=2)
    assert client.stderr_path.stat().st_size >= 100000


def test_ipc_timeout_quarantines_session_no_second_request(tmp_path, monkeypatch):
    client = client_with_program(
        tmp_path, monkeypatch, "import sys,time; sys.stdin.readline(); time.sleep(60)", timeout=0.1
    )
    # Isolate the test subprocess cleanup from subprocess.run's Popen lookup.
    monkeypatch.setattr(
        client,
        "_abort",
        lambda: (
            setattr(client, "_broken", True),
            client.process.kill(),
            client.process.wait(timeout=3),
        ),
    )
    with pytest.raises(FluentWorkerError, match="timed out") as error:
        client.call("execute_step")
    assert error.value.evidence["session_lost"]
    with pytest.raises(FluentWorkerError, match="will not be restarted"):
        client.call("observe")
    assert client._counter == 1


def test_malformed_protocol_is_terminal(tmp_path, monkeypatch):
    client = client_with_program(
        tmp_path, monkeypatch, "import sys; sys.stdin.readline(); print('invalid',flush=True)"
    )
    monkeypatch.setattr(
        client,
        "_abort",
        lambda: (
            setattr(client, "_broken", True),
            client.process.kill() if client.process.poll() is None else None,
            client.process.wait(timeout=3),
        ),
    )
    with pytest.raises(FluentWorkerError, match="protocol failed"):
        client.call("observe")
    assert client._broken


def test_overwrite_starts_fresh_and_preserves_unmanaged_files(tmp_path, monkeypatch):
    geometry = tmp_path / "input.scdoc"
    geometry.write_bytes(b"offline fixture")
    prompt_file = tmp_path / "input-prompt.txt"
    prompt_file.write_text("生成网格", encoding="utf-8")
    output = tmp_path / "output"
    calls = []

    class Graph:
        def invoke(self, initial, config):
            calls.append(initial)
            return {"run_id": initial["run_id"], "status": "created"}

    def build(checkpoint):
        assert not checkpoint.exists()
        checkpoint.write_bytes(b"new checkpoint")
        return Graph()

    monkeypatch.setattr(api, "build_graph", build)
    settings = RuntimeConfig(runtime_root=str(tmp_path / "runtime"), model="chosen-model")
    first = api.run_pipeline(
        geometry=geometry, prompt_path=prompt_file, output_dir=output, runtime_config=settings
    )
    assert calls[0]["prompt"] == "生成网格"
    (output / "artifacts" / "old.mesh").write_text("old")
    (output / "notes.txt").write_text("keep me")
    (output / "pause.json").write_text("old pause")
    (output / "result.json.tmp").write_text("old temp result")
    with pytest.raises(FileExistsError):
        api.run_pipeline(
            geometry=geometry, prompt_path=prompt_file, output_dir=output, runtime_config=settings
        )
    assert (output / "artifacts" / "old.mesh").exists()
    prompt_file.write_text("new mesh", encoding="utf-8")
    second = api.run_pipeline(
        geometry=geometry,
        prompt_path=prompt_file,
        output_dir=output,
        overwrite=True,
        runtime_config=settings,
    )
    assert second["run_id"] != first["run_id"]
    assert calls[1]["runtime_dir"] != calls[0]["runtime_dir"]
    assert calls[1]["status"] == "created" and "repair_history" not in calls[1]
    assert calls[1]["runtime_config"]["model"] == "chosen-model"
    assert not (output / "artifacts" / "old.mesh").exists()
    assert not (output / "pause.json").exists()
    assert not (output / "result.json.tmp").exists()
    assert (output / "notes.txt").read_text() == "keep me"
    assert (output / "prompt.txt").read_text().strip() == "new mesh"
    metadata = json.loads((output / "run-metadata.json").read_text())
    assert metadata["run_id"] == second["run_id"] and metadata["overwrite"]
    assert geometry.read_bytes() == b"offline fixture"
    assert prompt_file.read_text(encoding="utf-8") == "new mesh"


@pytest.mark.parametrize("protected_name", ["artifacts/input.scdoc", "prompt.txt", "state/source"])
def test_overwrite_checks_all_conflicts_before_removing_files(tmp_path, protected_name):
    output = tmp_path / "output"
    (output / "artifacts").mkdir(parents=True)
    marker = output / "artifacts" / "keep-until-validated.txt"
    marker.write_text("unchanged")
    protected = output / protected_name
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("protected input")
    with pytest.raises(ValueError, match="conflicts"):
        create_run_directory(
            output,
            RuntimeConfig(runtime_root=str(tmp_path / "runtime")),
            overwrite=True,
            protected_paths=(protected,),
        )
    assert marker.read_text() == "unchanged"
    assert protected.read_text() == "protected input"


def test_cli_passes_model_and_overwrite_to_api(monkeypatch, tmp_path):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(cli, "run_pipeline", run)
    assert (
        cli.main(
            [
                "run",
                "--geometry",
                "example.scdoc",
                "--prompt-file",
                "prompt.txt",
                "--model",
                "chosen-model",
                "--auth-mode",
                "api_key",
                "--output",
                str(tmp_path),
                "--overwrite",
            ]
        )
        == 0
    )
    assert calls[0]["runtime_config"].model == "chosen-model"
    assert calls[0]["runtime_config"].auth_mode == "api_key"
    assert calls[0]["overwrite"] is True
    assert calls[0]["prompt_path"] == Path("prompt.txt")
    assert "prompt" not in calls[0]


@pytest.mark.parametrize(
    "prompt_args",
    [[], ["--prompt", "mesh"], ["--prompt-file", "prompt.txt", "--prompt", "mesh"]],
)
def test_cli_requires_prompt_file_and_rejects_inline_prompt(prompt_args):
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["run", "--geometry", "example.scdoc", *prompt_args])
    assert error.value.code == 2


@pytest.mark.parametrize("answer,expected", [("accept", None), ("5", 5), ("cancel", "cancel")])
def test_cli_locked_parameter_intervention_resumes_with_explicit_value(monkeypatch, answer, expected):
    pause = {
        "kind": "locked_parameter",
        "message": "Locked control needs approval",
        "failed_step": "boundary_layers",
        "required_action": "approve or cancel",
        "evidence": {"proposed_parameters": {"value": 3}},
    }
    calls = []

    def resume(**kwargs):
        calls.append(kwargs)
        return {"status": "cancelled" if kwargs["action"] == "cancel" else "success"}

    answers = iter([answer])
    monkeypatch.setattr("builtins.input", lambda message: next(answers))
    monkeypatch.setattr(cli, "resume_pipeline", resume)
    result = cli._interactive_resume(
        {"status": "paused", "run_dir": "run", "interrupt": pause}, False
    )
    assert len(calls) == 1
    assert result["status"] == ("cancelled" if expected == "cancel" else "success")
    assert calls[0].get("parameter_value") == (expected if isinstance(expected, int) else None)


def test_legacy_checkpoint_is_explicitly_rejected(tmp_path):
    (tmp_path / "run-metadata.json").write_text(
        json.dumps({"checkpoint": "unused", "run_id": "run"})
    )
    with pytest.raises(ValueError, match="incompatible CFD Agent version"):
        api.resume_pipeline(run_dir=tmp_path, action="approve")
