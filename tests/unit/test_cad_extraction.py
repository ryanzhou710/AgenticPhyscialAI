"""CAD extraction strategy, explicit fluid-body reuse and native retries."""

import pytest

from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.adapters.spaceclaim_query import SpaceClaimError
from src.services.errors import PipelineError
from src.services.geometry_catalog import GeometryCatalog


def test_cad_node_uses_volume_extract_for_a_silent_closed_body(tmp_path, monkeypatch):
    from src.nodes import cad

    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        bodies=[
            {
                "id": "B1",
                "kind": "body",
                "solid_or_sheet": "solid",
                "volume_m3": 1.0,
                "edge_ids": ["E1"],
            }
        ],
        edges=[
            {"id": "E1", "kind": "edge", "body_id": "B1", "face_ids": ["F1", "F2"]},
        ],
        native_catalog={"public": {}, "internal": {}},
    )
    direct_flags = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def extract_volume(self, **kwargs):
            direct_flags.append(kwargs["existing_fluid_body"])
            return {"transfer": {"source_mode": "volume_extract"}}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    (tmp_path / "state").mkdir()
    result = cad.extract_volume(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "ui_mode": "hidden",
            "runtime_config": {},
            "working_geometry": str(tmp_path / "original.scdoc"),
            "catalog": catalog.model_dump(mode="json"),
            "selection_plan": {"openings": [], "fluid_domain_action": "extract"},
        }
    )

    assert not result["error"]
    assert result["extraction"]["transfer"]["source_mode"] == "volume_extract"
    assert direct_flags == [False]


def test_cad_node_reuses_only_an_explicitly_declared_fluid_body(tmp_path, monkeypatch):
    from src.nodes import cad

    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        bodies=[
            {
                "id": "B1",
                "kind": "body",
                "solid_or_sheet": "solid",
                "volume_m3": 1.0,
                "edge_ids": ["E1"],
            }
        ],
        edges=[
            {"id": "E1", "kind": "edge", "body_id": "B1", "face_ids": ["F1", "F2"]},
        ],
        native_catalog={"public": {}, "internal": {}},
    )
    direct_flags = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def extract_volume(self, **kwargs):
            direct_flags.append(kwargs["existing_fluid_body"])
            return {"transfer": {"source_mode": "existing_fluid_body"}}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    (tmp_path / "state").mkdir()
    result = cad.extract_volume(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "ui_mode": "hidden",
            "runtime_config": {},
            "working_geometry": str(tmp_path / "original.scdoc"),
            "catalog": catalog.model_dump(mode="json"),
            "selection_plan": {"openings": [], "fluid_domain_action": "reuse"},
        }
    )

    assert not result["error"]
    assert result["extraction"]["transfer"]["source_mode"] == "existing_fluid_body"
    assert direct_flags == [True]


@pytest.mark.parametrize("native_failure", [False, True])
def test_explicit_reuse_reaches_native_execution_without_catalog_topology_gate(
    tmp_path, monkeypatch, native_failure
):
    from src.nodes import cad

    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        bodies=[{"id": "B1", "kind": "body", "solid_or_sheet": "sheet", "volume_m3": 0.0}],
        native_catalog={"public": {}, "internal": {}},
    )

    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def extract_volume(self, **kwargs):
            calls.append(kwargs["existing_fluid_body"])
            if native_failure:
                raise RuntimeError("Native geometry operation failed")
            return {"transfer": {"source_mode": "existing_fluid_body"}}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    (tmp_path / "state").mkdir()
    result = cad.extract_volume(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "ui_mode": "hidden",
            "runtime_config": {},
            "working_geometry": str(tmp_path / "original.scdoc"),
            "catalog": catalog.model_dump(mode="json"),
            "selection_plan": {"openings": [], "fluid_domain_action": "reuse"},
        }
    )

    assert calls == [True]
    if native_failure:
        from src.nodes.results import has_error

        assert result["status"] == "repair_pending"
        assert result["failed_step"] == "extract_volume"
        assert "Native geometry operation failed" in result["error"]
        assert has_error(result) == "review_failure"
    else:
        assert not result["error"]
        assert result["extraction"]["transfer"]["source_mode"] == "existing_fluid_body"


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
