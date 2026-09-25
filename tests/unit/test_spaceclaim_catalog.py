from pathlib import Path

from src.adapters.spaceclaim import SpaceClaimRunner


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
    from src.services.geometry_models import GeometryCatalog

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
