"""User parameter approval policies across supported numeric repair actions."""

from typing import get_args

import pytest

from src.services.contracts import REPAIR_ACTIONS, RepairDecision, repair_tool_catalog
from src.services.reviewer import _user_parameter_change
from src.workers.repair_protocol import RepairState


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
