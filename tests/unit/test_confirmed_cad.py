import pytest

from src.services.boundaries import validate_confirmed_cad
from src.services.errors import PipelineError
from src.services.geometry_models import GeometryCatalog


def confirmed_catalog(groups):
    return GeometryCatalog(
        catalog_id="confirmed",
        geometry_id="fluid",
        bodies=[
            {"id": "B1", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 1.0,
             "face_ids": ["F1", "F2", "F3"]}
        ],
        faces=[
            {"id": "F1", "kind": "face", "body_id": "B1"},
            {"id": "F2", "kind": "face", "body_id": "B1"},
            {"id": "F3", "kind": "face", "body_id": "B1"},
        ],
        edges=[
            {"id": "E1", "kind": "edge", "body_id": "B1", "face_ids": ["F1", "F2"]},
            {"id": "E2", "kind": "edge", "body_id": "B1", "face_ids": ["F2", "F3"]},
        ],
        native_catalog={"internal": {"raw_groups": groups}},
    )


def roles():
    return {"in": "inlet", "out": "outlet", "wall": "wall"}


def valid_groups():
    return [
        {"raw_name": "in", "member_ids": ["F1"]},
        {"raw_name": "out", "member_ids": ["F2"]},
        {"raw_name": "wall", "member_ids": ["F3"]},
    ]


def test_confirmed_cad_requires_closed_full_nonoverlapping_groups():
    result = validate_confirmed_cad(catalog=confirmed_catalog(valid_groups()), roles=roles())
    assert result["all_faces_grouped"] is True
    assert result["roles_complete"] is True


@pytest.mark.parametrize(
    ("groups", "code"),
    [
        (
            [
                {"raw_name": "in", "member_ids": ["F1"]},
                {"raw_name": "out", "member_ids": ["F2"]},
                {"raw_name": "wall", "member_ids": []},
            ],
            "CAD_CONFIRMED_GROUP_EMPTY",
        ),
        (
            [
                {"raw_name": "in", "member_ids": ["F1"]},
                {"raw_name": "out", "member_ids": ["F2", "F3"]},
                {"raw_name": "wall", "member_ids": ["F3"]},
            ],
            "CAD_CONFIRMED_GROUP_OVERLAP",
        ),
    ],
)
def test_confirmed_cad_blocks_invalid_group_edits(groups, code):
    with pytest.raises(PipelineError) as error:
        validate_confirmed_cad(catalog=confirmed_catalog(groups), roles=roles())
    assert error.value.detail["code"] == code
