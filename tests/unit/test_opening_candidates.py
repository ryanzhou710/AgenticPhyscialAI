"""Opening topology candidates exposed to the CAD grounding model."""

import pytest

from src.services.geometry_models import GeometryCatalog
from src.services.grounding import _opening_candidate_context


def test_opening_context_exposes_planar_faces_and_closed_arbitrary_loops():
    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        faces=[
            {"id": "F_RECT", "kind": "face", "surface_type": "Plane"},
            {"id": "F_WALL", "kind": "face", "surface_type": "Cylinder"},
        ],
        loops=[
            {
                "id": "L_RECT",
                "kind": "loop",
                "face_id": "F_RECT",
                "closed": True,
                "is_outer": True,
                "edge_ids": ["E1", "E2", "E3", "E4"],
            },
            {
                "id": "L_OPEN",
                "kind": "loop",
                "face_id": "F_RECT",
                "closed": False,
            },
        ],
    )

    context = _opening_candidate_context(catalog)

    assert context["planar_faces"] == ["F_RECT"]
    assert context["closed_loops"] == ["L_RECT"]
    assert "arbitrary opening" in context["selection_guidance"]


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
