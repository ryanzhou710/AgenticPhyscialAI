import pytest

from src.services.terminals import resolve_terminal_boundary


def catalog(*, faces=None, edges=None, loops=None):
    return {
        "public": {
            "faces": faces or [],
            "edges": edges or [],
            "loops": loops or [],
        }
    }


def test_rectangular_terminal_face_uses_complete_outer_loop():
    data = catalog(
        faces=[{"id": "F1", "surface_type": "Plane"}],
        edges=[
            {"id": edge_id, "curve_type": "Line", "face_ids": ["F1", "F2"]}
            for edge_id in ("E1", "E2", "E3", "E4")
        ],
        loops=[
            {
                "id": "L1",
                "face_id": "F1",
                "is_outer": True,
                "edge_ids": ["E1", "E2", "E3", "E4"],
            }
        ],
    )

    assert resolve_terminal_boundary(data, "F1") == {
        "kind": "terminal_face",
        "edge_ids": ["E1", "E2", "E3", "E4"],
    }


def test_disc_terminal_face_uses_its_outer_circle():
    data = catalog(
        faces=[{"id": "F1", "surface_type": "Plane"}],
        edges=[{"id": "E1", "curve_type": "Circle", "face_ids": ["F1", "F2"]}],
        loops=[{"id": "L1", "face_id": "F1", "is_outer": True, "edge_ids": ["E1"]}],
    )

    assert resolve_terminal_boundary(data, "F1") == {
        "kind": "terminal_face",
        "edge_ids": ["E1"],
    }


def test_annular_face_keeps_using_its_circular_inner_loop():
    data = catalog(
        faces=[{"id": "F1", "surface_type": "Plane"}],
        edges=[
            {"id": "E1", "curve_type": "Circle", "face_ids": ["F1", "F2"]},
            {"id": "E2", "curve_type": "Circle", "face_ids": ["F1"]},
        ],
        loops=[
            {"id": "L1", "face_id": "F1", "is_outer": True, "edge_ids": ["E1"]},
            {"id": "L2", "face_id": "F1", "is_outer": False, "edge_ids": ["E2"]},
        ],
    )

    assert resolve_terminal_boundary(data, "F1") == {
        "kind": "circular_inner_loop",
        "edge_ids": ["E2"],
    }


def test_selected_loop_preserves_all_edges_for_a_non_circular_opening():
    data = catalog(
        edges=[
            {"id": edge_id, "curve_type": "Line", "face_ids": ["F1", "F2"]}
            for edge_id in ("E1", "E2", "E3", "E4")
        ],
        loops=[
            {
                "id": "L1",
                "face_id": "F1",
                "closed": True,
                "is_outer": True,
                "edge_ids": ["E1", "E2", "E3", "E4"],
            }
        ],
    )

    assert resolve_terminal_boundary(data, "L1") == {
        "kind": "loop",
        "edge_ids": ["E1", "E2", "E3", "E4"],
    }


def test_nonplanar_terminal_face_is_rejected():
    data = catalog(faces=[{"id": "F1", "surface_type": "Cylinder"}])

    with pytest.raises(ValueError, match="not planar"):
        resolve_terminal_boundary(data, "F1")
