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
