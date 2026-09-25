import pytest

from src.services.errors import PipelineError
from src.services.terminals import resolve_extraction_selection


def catalog(*, loops):
    return {
        "public": {
            "faces": [
                {"id": "F1", "surface_type": "Plane"},
                {"id": "F2", "surface_type": "Cylinder"},
            ],
            "edges": [
                {"id": "E1", "curve_type": "Line", "face_ids": ["F1", "F3"]},
                {"id": "E2", "curve_type": "Line", "face_ids": ["F1", "F3"]},
                {"id": "E3", "curve_type": "Line", "face_ids": ["F1", "F3"]},
                {"id": "E4", "curve_type": "Line", "face_ids": ["F1", "F3"]},
                {"id": "E5", "curve_type": "Circle", "closed": True, "face_ids": ["F1"]},
                {"id": "E6", "curve_type": "Circle", "closed": True, "face_ids": ["F1"]},
            ],
            "loops": loops,
        }
    }


def plan(candidate_id="F1"):
    return {
        "seed_inner_wall_id": "F2",
        "openings": [{"candidate_id": candidate_id, "name": "inlet", "role": "inlet"}],
    }


def test_rectangular_end_face_uses_outer_loop_and_face_strategy():
    selection = resolve_extraction_selection(
        catalog(loops=[
            {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
             "edge_ids": ["E1", "E2", "E3", "E4"]}
        ]),
        plan(),
    )

    assert selection["terminal_records"] == [
        {
            "name": "inlet",
            "role": "inlet",
            "source_candidate_id": "F1",
            "support_face_id": "F1",
            "contour_loop_id": "L1",
            "edge_ids": ["E1", "E2", "E3", "E4"],
            "is_outer": True,
            "face_cap_supported": True,
        }
    ]
    assert selection["face_strategy_available"] is True


def test_annular_face_resolves_its_inner_contour_once_and_uses_edges():
    selection = resolve_extraction_selection(
        catalog(loops=[
            {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
             "edge_ids": ["E1", "E2", "E3", "E4"]},
            {"id": "L2", "face_id": "F1", "is_outer": False, "closed": True,
             "edge_ids": ["E5"]},
        ]),
        plan(),
    )

    terminal = selection["terminal_records"][0]
    assert terminal["contour_loop_id"] == "L2"
    assert terminal["edge_ids"] == ["E5"]
    assert terminal["face_cap_supported"] is False
    assert selection["face_strategy_available"] is False


def test_multi_inner_loop_face_reports_concrete_loop_candidates():
    with pytest.raises(PipelineError) as error:
        resolve_extraction_selection(
            catalog(loops=[
                {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
                 "edge_ids": ["E1", "E2", "E3", "E4"]},
                {"id": "L2", "face_id": "F1", "is_outer": False, "closed": True,
                 "edge_ids": ["E5"]},
                {"id": "L3", "face_id": "F1", "is_outer": False, "closed": True,
                 "edge_ids": ["E6"]},
            ]),
            plan(),
        )
    assert error.value.detail["code"] == "CAD_OPENING_AMBIGUOUS"
    assert error.value.evidence["candidate_loop_ids"] == ["L2", "L3"]


def test_overlapping_openings_are_rejected_before_spaceclaim_starts():
    data = catalog(loops=[
        {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
         "edge_ids": ["E1", "E2", "E3", "E4"]}
    ])
    duplicate = {
        "seed_inner_wall_id": "F2",
        "openings": [
            {"candidate_id": "L1", "name": "inlet", "role": "inlet"},
            {"candidate_id": "F1", "name": "outlet", "role": "outlet"},
        ],
    }

    with pytest.raises(PipelineError) as error:
        resolve_extraction_selection(data, duplicate)
    assert error.value.detail["code"] == "CAD_OPENING_OVERLAP"


def test_invalid_seed_is_rejected_before_spaceclaim_starts():
    bad = plan()
    bad["seed_inner_wall_id"] = "E5"
    with pytest.raises(PipelineError) as error:
        resolve_extraction_selection(
            catalog(loops=[
                {"id": "L1", "face_id": "F1", "is_outer": True, "closed": True,
                 "edge_ids": ["E1", "E2", "E3", "E4"]}
            ]),
            bad,
        )
    assert error.value.detail["code"] == "CAD_SEED_INVALID"
