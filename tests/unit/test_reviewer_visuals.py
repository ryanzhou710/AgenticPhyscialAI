"""Reviewer captures images only where visual evidence can assist diagnosis."""

import json
from types import SimpleNamespace

import pytest

from src.services import reviewer
from src.services.contracts import RepairDecision


@pytest.mark.parametrize("stage,code,visual", [
    ("verify_selection", "", True),
    ("extract_volume", "", True),
    ("label_faces", "", True),
    ("surface_mesh", "", True),
    ("boundary_layers", "", True),
    ("volume_mesh", "", True),
    ("final_validation", "", True),
    ("launch_fluent", "", False),
    ("import_geometry", "", False),
    ("local_sizing", "", False),
    ("update_boundaries", "", False),
    ("query_geometry", "", False),
    ("validate_cad", "", False),
    ("surface_mesh", "RUNTIME_TIMEOUT", False),
    ("extract_volume", "CAD_EXTRACTION_RUNTIME_FAILED", False),
    ("verify_selection", "LLM_REQUEST_FAILED", False),
])
def test_visual_scope(tmp_path, monkeypatch, stage, code, visual):
    calls, requests = [], []
    picture = tmp_path / "evidence.png"
    picture.write_bytes(b"offline image")

    class Worker:
        def call(self, operation):
            calls.append(operation)
            return {"path": str(picture)} if operation == "picture" else {"controls": {}}

    class Model:
        def probe_vision(self):
            calls.append("probe")
            return SimpleNamespace(status=SimpleNamespace(value="verified"))

        def invoke(self, **kwargs):
            requests.append(kwargs)
            return RepairDecision(action="stop", target_step=stage, diagnosis="offline", evidence="offline")

    monkeypatch.setattr(reviewer, "get_client", lambda *args: Worker())
    monkeypatch.setattr(reviewer.GroundingLLMClient, "from_runtime_config", lambda **kwargs: Model())
    result = reviewer.diagnose_failure({
        "run_id": "test", "run_dir": str(tmp_path), "runtime_dir": str(tmp_path),
        "max_repair_rounds": 3, "failed_step": stage, "error": "native failure",
        "error_detail": {"code": code},
        "catalog": {"catalog_id": "c", "geometry_id": "g"},
        "native_selection": {"images": [{"path": str(picture)}]},
    })
    assert result["repair_decision_source"] == "llm"
    assert bool(requests[0]["images"]) is visual
    assert ("probe" in calls) is visual
    fluent = stage in (*reviewer.FLUENT_STEPS, "final_validation", "launch_fluent")
    assert ("picture" in calls) is (visual and fluent)
    assert ("observe" in calls) is fluent
    evidence = json.loads(requests[0]["user_prompt"])
    assert evidence["error"] == "native failure"
    if not fluent:
        assert "candidate_catalog" in evidence


@pytest.mark.parametrize("failure", ["picture", "probe", "unsupported"])
def test_optional_visual_failure_preserves_text_diagnosis(tmp_path, monkeypatch, failure):
    requests = []
    picture = tmp_path / "evidence.png"
    picture.write_bytes(b"offline image")

    class Worker:
        def call(self, operation):
            if operation == "observe":
                return {"controls": {"global_size": 2}}
            if failure == "picture":
                raise RuntimeError("Screenshot unavailable")
            return {"path": str(picture)}

    class Model:
        def probe_vision(self):
            if failure == "probe":
                raise RuntimeError("Vision probe unavailable")
            return SimpleNamespace(status=SimpleNamespace(value="unsupported"), reason="No vision")

        def invoke(self, **kwargs):
            requests.append(kwargs)
            return RepairDecision(action="stop", target_step="surface_mesh", diagnosis="offline", evidence="offline")

    monkeypatch.setattr(reviewer, "get_client", lambda *args: Worker())
    monkeypatch.setattr(reviewer.GroundingLLMClient, "from_runtime_config", lambda **kwargs: Model())
    result = reviewer.diagnose_failure({
        "run_id": "test", "run_dir": str(tmp_path), "runtime_dir": str(tmp_path),
        "max_repair_rounds": 3, "failed_step": "surface_mesh", "error": "native failure",
    })
    assert result["repair_decision_source"] == "llm"
    assert requests[0]["images"] == []
    evidence = json.loads(requests[0]["user_prompt"])
    assert evidence["error"] == "native failure"
    assert evidence["fluent_observation"]["controls"]["global_size"] == 2
