"""Repair registry, user approval policy and visual diagnostic evidence."""

import json
from types import SimpleNamespace
from typing import get_args

import pytest

from src.services import reviewer
from src.services.contracts import (
    REPAIR_ACTIONS,
    RepairDecision,
    repair_tool_catalog,
)
from src.services.reviewer import _user_parameter_change
from src.workers.fluent.repair import RepairState


def test_repair_registry_covers_contract_and_runtime_handlers():
    declared = set(get_args(RepairDecision.model_fields["action"].annotation))
    assert set(REPAIR_ACTIONS) == declared == set(repair_tool_catalog())
    for spec in REPAIR_ACTIONS.values():
        if spec.route in {"retry", "fluent"}:
            assert spec.worker_handler
            assert callable(getattr(RepairState, spec.worker_handler))
        else:
            assert spec.worker_handler is None
        if spec.approval == "user_parameter":
            assert spec.user_parameter is not None


@pytest.mark.parametrize("source", ["user", "inferred", None])
@pytest.mark.parametrize("action", [
    "set_global_size", "set_local_size", "set_first_layer_height",
    "set_layer_count", "set_growth_rate",
])
def test_all_numeric_controls_use_provenance(action, source):
    control = {"source": source, "value": 2, "unit": "mm"}
    requirements = {
        "global_size": control,
        "local_refinements": [{"boundary_name": "feed", "size": control}],
        "boundary_layers": {
            "first_layer_height": control,
            "layers": 2, "layers_source": source,
            "growth_rate": 2, "growth_rate_source": source,
        },
    }
    decision = RepairDecision(
        action=action, target_step="volume_mesh", diagnosis="Native rejection",
        evidence="Offline test", parameters={"value": 3, **({"zone": "feed"} if action == "set_local_size" else {})},
    )
    result = _user_parameter_change({"mesh_requirements": requirements}, decision)
    assert (result is not None) == (source == "user")


def test_new_local_control_does_not_inherit_an_unrelated_user_request():
    decision = RepairDecision(
        action="set_local_size", target_step="local_sizing", diagnosis="Native rejection",
        evidence="Offline test", parameters={"zone": "other", "value": 3},
    )
    state = {"mesh_requirements": {"local_refinements": [
        {"boundary_name": "feed", "size": {"source": "user", "value": 2, "unit": "mm"}},
    ]}}
    assert _user_parameter_change(state, decision) is None


@pytest.mark.parametrize("response,expected", [
    ({"action": "approve"}, 3),
    ({"action": "approve", "parameter_value": 5}, 5),
    ({"action": "cancel"}, None),
])
def test_parameter_approval_keeps_session_and_applies_only_this_decision(monkeypatch, response, expected):
    from src.nodes import confirmation

    decision = {"action": "set_layer_count", "parameters": {"value": 3}}
    state = {"human_request": {"kind": "parameter_change"}, "repair_decision": decision}
    monkeypatch.setattr(confirmation, "interrupt", lambda request: response)
    result = confirmation.human_intervention(state)
    if expected is None:
        assert result.goto == "cancelled"
        assert not result.update.get("repair_approved")
    else:
        assert result.goto == "apply_repair"
        assert result.update["repair_approved"] is True
        assert result.update["repair_decision"]["parameters"]["value"] == expected
    assert decision["parameters"]["value"] == 3


@pytest.mark.parametrize("source", ["user", "inferred", None])
def test_disabling_layers_always_requires_approval(source, monkeypatch, tmp_path):
    decision = RepairDecision(action="set_layer_count", target_step="volume_mesh",
                              diagnosis="Prism failure", evidence="native", parameters={"value": 0})
    state = {"run_id": "run", "runtime_dir": str(tmp_path), "run_dir": str(tmp_path),
             "failed_step": "volume_mesh", "repair_history": [{}],
             "mesh_requirements": {"boundary_layers": {"layers": 5, "layers_source": source}},
             "repair_decision": decision.model_dump()}
    monkeypatch.setattr(reviewer, "_copy_runtime_evidence", lambda *args: {})
    calls = []
    client = SimpleNamespace(call=lambda *args: calls.append(args) or {"resume": "boundary_layers"})
    monkeypatch.setattr(reviewer, "get_client", lambda *args: client)
    result = reviewer.execute_repair(state)
    assert result.goto == "human_intervention"
    assert not calls
    approved = reviewer.execute_repair({**state, "repair_approved": True})
    assert approved.goto == "boundary_layers"
    assert calls[0][1]["manual_approved"] is True


def test_explicit_zero_layers_does_not_require_repeated_approval():
    decision = RepairDecision(action="set_layer_count", target_step="volume_mesh",
                              diagnosis="Retry", evidence="native", parameters={"value": 0})
    state = {"mesh_requirements": {"boundary_layers": {"layers": 0, "layers_source": "user"}}}
    assert reviewer._user_parameter_change(state, decision) is None


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
