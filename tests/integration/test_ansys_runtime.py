import os
from pathlib import Path

import pytest

from src.adapters.spaceclaim import SpaceClaimRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("CFD_AGENT_RUN_ANSYS_INTEGRATION") != "1",
    reason="requires licensed Ansys 2024 R1",
)


def test_spaceclaim_returns_real_native_catalog(tmp_path: Path):
    supplied = os.environ.get("CFD_AGENT_TEST_GEOMETRY")
    if not supplied:
        pytest.skip("Set CFD_AGENT_TEST_GEOMETRY to a privately supplied SCDOC")
    geometry = Path(supplied)
    runner = SpaceClaimRunner(output_dir=tmp_path, ui_mode="hidden", timeout_s=900)
    try:
        catalog, _ = runner.catalog(geometry, render_candidates=False)
    finally:
        runner.close()
    assert catalog.faces
    assert catalog.native_catalog["internal"]["refs"]


def test_candidate_catalog_is_resilient_and_line_edge_camera_is_non_degenerate(tmp_path: Path):
    supplied = os.environ.get("CFD_AGENT_TEST_GEOMETRY")
    if not supplied:
        pytest.skip("Set CFD_AGENT_TEST_GEOMETRY to a privately supplied SCDOC")
    geometry = Path(supplied)
    ui_mode = os.environ.get("CFD_AGENT_TEST_UI_MODE", "hidden")
    runner = SpaceClaimRunner(output_dir=tmp_path, ui_mode=ui_mode, timeout_s=900)
    try:
        catalog, _ = runner.catalog(
            geometry,
            render_candidates=True,
            candidate_collections=["faces", "edges"],
        )

        results = catalog.candidate_render_results
        assert len(results) == len(catalog.faces) + len(catalog.edges)
        by_id = {row["candidate_id"]: row for row in results}
        for edge in catalog.edges:
            expected_visual_candidate = (
                edge.curve_type == "Circle" and len(edge.face_ids) == 1
            )
            if expected_visual_candidate:
                assert by_id[edge.id]["status"] in {"rendered", "failed"}
            else:
                assert by_id[edge.id]["status"] == "skipped"
                assert by_id[edge.id]["reason"] == "not_a_supported_opening_edge"

        for failure in (
            row for row in results if row["status"] == "failed"
        ):
            assert failure["error"].startswith("Traceback")

        preferred_line_id = os.environ.get("CFD_AGENT_TEST_LINE_EDGE_ID")
        line_edge = next(
            edge
            for edge in catalog.edges
            if edge.curve_type == "Line"
            and (preferred_line_id is None or edge.id == preferred_line_id)
        )
        selection = runner.select(geometry, catalog, [line_edge.id], views=[])
        selected = [row for row in selection.images if row.get("view") == "Selected"]
        assert len(selected) == 1
        assert Path(selected[0]["path"]).is_file()
        assert selection.active_selection_verified
    finally:
        runner.close()
