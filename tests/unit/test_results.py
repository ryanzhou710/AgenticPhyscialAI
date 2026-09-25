import json
from pathlib import Path

from src.nodes import results
from src.services.execution import _copy_runtime_evidence


def successful_state(tmp_path: Path) -> dict:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("mesh.msh.h5", "final-mesh.png", "fluent-worker.log"):
        (runtime / name).write_bytes(name.encode())
    return {
        "run_id": "result-test",
        "run_dir": str(tmp_path),
        "runtime_dir": str(runtime),
        "keep_open": False,
        "confirmed_geometry": str(tmp_path / "artifacts" / "confirmed.scdoc"),
        "boundary_roles": {"inlet": "inlet", "outlet": "outlet"},
        "mesh_requirements": {"global_size": {"value": 1}},
        "fluent_steps": {
            "final_validation": {"quality": {"minimum": 0.2}},
            "picture": {"path": str(runtime / "final-mesh.png")},
        },
        "catalog": {"faces": [{"id": "F1"}]},
    }


def test_completed_archives_primary_artifacts_once_and_writes_thin_stage_record(tmp_path):
    outcome = results.completed(successful_state(tmp_path))

    assert outcome["status"] == "success"
    result = outcome["result"]
    assert Path(result["mesh"]).is_file()
    assert Path(result["mesh_image"]).is_file()
    assert {"boundary_roles", "mesh_requirements"} <= result.keys()
    assert {key for key in result if key.endswith("boundary_roles")} == {"boundary_roles"}
    assert {key for key in result if key.endswith("mesh_requirements")} == {"mesh_requirements"}
    assert not (tmp_path / "artifacts" / "success-runtime" / "mesh.msh.h5").exists()
    assert not (tmp_path / "artifacts" / "success-runtime" / "final-mesh.png").exists()

    stage = json.loads((tmp_path / "state" / "completed.json").read_text(encoding="utf-8"))
    assert stage["current_step"] == "completed"
    assert "catalog" not in stage
    assert stage["update"]["result"]["mesh"] == result["mesh"]


def test_missing_required_mesh_ends_as_archive_failure_without_reviewer(tmp_path):
    state = successful_state(tmp_path)
    (Path(state["runtime_dir"]) / "mesh.msh.h5").unlink()

    outcome = results.completed(state)

    assert outcome["status"] == "failed"
    assert outcome["failed_step"] == "archive_mesh"
    assert outcome["repair_stop_reason"] == "archive_failure"
    assert outcome["result"]["archive_error"]["operation"] == "archive_mesh"
    assert "review_failure" not in outcome


def test_archive_failure_preserves_the_original_failure(tmp_path, monkeypatch):
    state = {
        "run_id": "failure-test",
        "run_dir": str(tmp_path),
        "runtime_dir": str(tmp_path),
        "keep_open": True,
        "failed_step": "surface_mesh",
        "error": "FluentWorkerError: native rejection",
        "error_detail": {"code": "FLUENT_OPERATION_FAILED"},
        "error_evidence": {"native": "rejected"},
    }
    original_write = results.write_json

    def fail_result_write(path, value):
        if Path(path).name == "result.json":
            raise OSError("disk unavailable")
        return original_write(path, value)

    monkeypatch.setattr(results, "write_json", fail_result_write)
    outcome = results.failed(state)

    assert outcome["status"] == "failed"
    assert outcome["result"]["error"] == state["error"]
    assert outcome["result"]["error_detail"] == state["error_detail"]
    assert outcome["result"]["archive_error"]["operation"] == "archive_results"


def test_preview_failure_after_mesh_validation_returns_success_with_warning(tmp_path, monkeypatch):
    from src.nodes import fluent

    state = successful_state(tmp_path)
    (Path(state["runtime_dir"]) / "final-mesh.png").unlink()
    calls = []

    class Client:
        def call(self, operation, data=None):
            calls.append(operation)
            if operation == "execute_step":
                return {"quality": {"minimum": 0.2}, "final_execution": {"valid": True}}
            raise RuntimeError("display driver rejected the preview")

    monkeypatch.setattr(fluent, "get_client", lambda *args: Client())
    update = fluent.validate_mesh(state)
    state.update(update)
    outcome = results.completed(state)

    assert calls == ["execute_step", "picture"]
    assert update["error"] == ""
    assert (tmp_path / "artifacts" / "mesh-preview-error.json").is_file()
    assert outcome["status"] == "success"
    assert outcome["result"]["mesh_image"] is None
    assert "Mesh preview could not be created" in outcome["result"]["warnings"][0]


def test_repeated_evidence_snapshots_do_not_overwrite_earlier_records(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "fluent-worker.log").write_text("first", encoding="utf-8")
    state = {"run_dir": str(tmp_path), "runtime_dir": str(runtime)}

    first = _copy_runtime_evidence(state, "intervention")
    (runtime / "fluent-worker.log").write_text("second", encoding="utf-8")
    second = _copy_runtime_evidence(state, "intervention")

    assert Path(first["fluent-worker.log"]).read_text(encoding="utf-8") == "first"
    assert Path(second["fluent-worker.log"]).read_text(encoding="utf-8") == "second"
    assert "intervention-2" in second["fluent-worker.log"]
