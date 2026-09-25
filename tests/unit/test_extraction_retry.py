import pytest

from src.adapters.spaceclaim import SpaceClaimError
from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.services.errors import PipelineError


def catalog():
    return {
        "public": {
            "faces": [
                {"id": "F1", "surface_type": "Plane"},
                {"id": "F2", "surface_type": "Cylinder"},
            ],
            "edges": [
                {"id": "E1", "curve_type": "Circle", "closed": True, "face_ids": ["F1"]}
            ],
            "loops": [
                {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
                 "edge_ids": ["E1"]}
            ],
        }
    }


def plan():
    return {
        "seed_inner_wall_id": "F2",
        "openings": [{"candidate_id": "F1", "name": "inlet", "role": "inlet"}],
    }


def test_face_failure_retries_once_with_same_contour_in_fresh_execution(monkeypatch, tmp_path):
    adapter = SpaceClaimBuildAdapter(runtime_dir=tmp_path, ui_mode="hidden")
    calls = []

    def execute(operation, payload, **kwargs):
        calls.append(payload)
        if payload["extraction_strategy"] == "faces":
            raise SpaceClaimError("VolumeExtract failed")
        return {"ok": True, "transfer": {}}

    monkeypatch.setattr(adapter, "_execute", execute)
    result = adapter.extract_volume(
        source=tmp_path / "original.scdoc",
        output=tmp_path / "extracted.scdoc",
        catalog=catalog(),
        selection_plan=plan(),
    )

    assert [call["extraction_strategy"] for call in calls] == ["faces", "edges"]
    assert calls[0]["input"] == calls[1]["input"]
    assert calls[0]["terminal_records"] == calls[1]["terminal_records"]
    assert "selection_plan" not in calls[0]
    assert result["extraction_attempts"] == [
        {"strategy": "faces", "status": "failed", "error": "VolumeExtract failed", "detail": {}},
        {"strategy": "edges", "status": "success"},
    ]


def test_identity_failure_does_not_switch_to_edge_strategy(monkeypatch, tmp_path):
    adapter = SpaceClaimBuildAdapter(runtime_dir=tmp_path, ui_mode="hidden")
    calls = []

    def execute(operation, payload, **kwargs):
        calls.append(payload)
        raise SpaceClaimError(
            "Object identity changed", detail={"code": "CAD_OBJECT_IDENTITY_CHANGED"}
        )

    monkeypatch.setattr(adapter, "_execute", execute)

    with pytest.raises(PipelineError) as error:
        adapter.extract_volume(
            source=tmp_path / "original.scdoc",
            output=tmp_path / "extracted.scdoc",
            catalog=catalog(),
            selection_plan=plan(),
        )
    assert error.value.detail["code"] == "CAD_VOLUME_EXTRACT_FAILED"
    assert [call["extraction_strategy"] for call in calls] == ["faces"]
