"""SpaceClaim catalog identity, model-visible fields and candidate rendering."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import Field

from src.adapters.spaceclaim_query import SpaceClaimRunner
from src.services.geometry_catalog import CatalogObject, GeometryCatalog


def test_visible_declared_fields_exclude_internal_and_extras():
    class ExtendedObject(CatalogObject):
        curvature: float = 2.0
        internal_note: str = Field(default="private", json_schema_extra={"model_visible": False})

    data = ExtendedObject(id="F1", kind="face", moniker="native", arbitrary="extra").model_facing_dict()
    assert data["curvature"] == 2.0
    assert not {"internal_note", "moniker", "arbitrary"} & data.keys()
    assert "schema_version" not in GeometryCatalog(catalog_id="c", geometry_id="g").model_dump()


def native_functions(*names):
    source = Path("src/workers/spaceclaim/common.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"json": json, "number": float, "moniker_of": lambda obj: obj.moniker}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "native-test", "exec"), namespace)
    return namespace


def test_signature_keeps_full_precision_and_checks_native_identity():
    ns = native_functions("geometry_signature", "validate_catalog_identity", "expand_candidate_ids")
    def catalog(area, moniker="native"):
        return {"public": {"faces": [{"id": "F1", "area": area}]},
                "internal": {"refs": {"F1": {"moniker": moniker}}}}
    a = catalog(0.012345678901234)
    b = catalog(0.012345678901235)
    with pytest.raises(ValueError, match="Geometry or topology changed"):
        ns["validate_catalog_identity"](a, b, ["F1"])
    with pytest.raises(ValueError, match="identity changed"):
        ns["validate_catalog_identity"](a, catalog(0.012345678901234, "other"), ["F1"])
    ns["validate_catalog_identity"](a, a, ["F1"])


def test_native_numeric_sort_keeps_precision_and_moniker_breaks_ties():
    ns = native_functions("scalar_key", "assign_ids")
    key = ns["scalar_key"]
    assert key(0.012345678901234) != key(0.012345678901235)
    assert key(2) < key(10)
    objects = [SimpleNamespace(moniker="b"), SimpleNamespace(moniker="a")]
    by_id, _ = ns["assign_ids"]("F", objects, lambda obj: key(2))
    assert [obj.moniker for obj in by_id.values()] == ["a", "b"]


def test_catalog_preserves_candidate_render_failures_without_failing(tmp_path, monkeypatch):
    images = []
    for view, role in (
        ("Front", "direction_reference"),
        ("Top", "auxiliary"),
        ("Right", "auxiliary"),
        ("Isometric", "auxiliary"),
    ):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"evidence")
        images.append(
            {
                "view": view,
                "role": role,
                "coordinate_frame": "spaceclaim_global",
                "path": str(path),
            }
        )

    response = {
        "ok": True,
        "images": images,
        "catalog": {
            "public": {
                "coordinate_unit": "m",
                "bodies": [],
                "faces": [],
                "edges": [],
                "loops": [],
            },
            "internal": {"refs": {}},
        },
        "candidate_render_results": [
            {
                "candidate_id": "E0003",
                "candidate_kind": "edge",
                "status": "failed",
                "error": "Traceback: native camera failure",
                "images": [],
            }
        ],
        "candidate_render_summary": {"rendered": 2, "failed": 1, "skipped": 4},
    }
    runner = SpaceClaimRunner(output_dir=tmp_path / "output")
    monkeypatch.setattr(runner, "_invoke", lambda *_args, **_kwargs: response)

    catalog, catalog_path = runner.catalog(Path("fixture.scdoc"), render_candidates=True)

    assert catalog_path.is_file()
    assert catalog.candidate_render_summary == {"rendered": 2, "failed": 1, "skipped": 4}
    assert catalog.candidate_render_results[0]["candidate_id"] == "E0003"
    assert "native camera failure" in catalog.candidate_render_results[0]["error"]


def test_detail_renderer_passes_only_requested_views_and_labels_them(tmp_path, monkeypatch):
    from src.services.geometry_catalog import GeometryCatalog

    runner = SpaceClaimRunner(output_dir=tmp_path / "output")
    image = tmp_path / "detail.png"
    from PIL import Image

    Image.new("RGB", (20, 20), "white").save(image)
    requests = [
        {
            "candidate_id": "F1",
            "purpose": "seed",
            "detail_views": ["OwnerContext", "SelectedProxy"],
            "reason": "inner wall",
        }
    ]
    captured = []
    labels = []

    def batch_select(path, catalog, selections):
        captured.extend(selections)
        return [
            {
                "images": [
                    {"view": "OwnerContext", "path": str(image)},
                    {"view": "SelectedProxy", "path": str(image)},
                    {"view": "Selected", "path": str(image)},
                ]
            }
        ]

    monkeypatch.setattr(runner, "batch_select", batch_select)
    monkeypatch.setattr(
        runner,
        "_annotate_candidate",
        lambda path, candidate_id, view=None: labels.append((candidate_id, view)),
    )
    result = runner.render_candidate_details(
        Path("fixture.scdoc"),
        GeometryCatalog(catalog_id="c", geometry_id="g"),
        requests,
    )

    assert captured == [
        {
            "task_id": "detail-F1",
            "candidate_ids": ["F1"],
            "views": [],
            "detail_views": ["OwnerContext", "SelectedProxy"],
        }
    ]
    assert [row["view"] for row in result] == ["OwnerContext", "SelectedProxy"]
    assert labels == [("F1", "OwnerContext"), ("F1", "SelectedProxy")]
