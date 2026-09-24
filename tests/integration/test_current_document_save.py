"""Opt-in native save verification. No LLM, meshing or original-CAD modifications."""

import os
from pathlib import Path

import pytest

from src.adapters.spaceclaim import SpaceClaimRunner
from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.adapters.windows_process import request_window_close
from src.services.artifacts import write_json
from src.services.boundaries import named_groups


@pytest.mark.skipif(
    not os.environ.get("CFD_AGENT_REAL_SAVE_CAD"), reason="requires an explicit local CAD and Ansys"
)
@pytest.mark.parametrize("make_edit", [False, True], ids=["saved", "unsaved"])
def test_real_current_document_save(tmp_path, make_edit):
    output_root = Path(os.environ["CFD_AGENT_REAL_SAVE_OUTPUT"]) / (
        "unsaved" if make_edit else "saved"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    adapter = SpaceClaimBuildAdapter(runtime_dir=tmp_path, ui_mode="gui", timeout_s=120)
    adapter.script = Path(__file__).with_name("native_save_fixture_v241.py")
    working = tmp_path / "working.scdoc"
    group_name = "save_check_unsaved_group"
    session = adapter._execute(
        "save_fixture",
        {
            "input": os.environ["CFD_AGENT_REAL_SAVE_CAD"],
            "output": str(working),
            "make_edit": make_edit,
            "group_name": group_name,
        },
        keep_open=True,
    )
    write_json(output_root / "session.json", session)
    try:
        assert session["saved_initially_modified"] is False
        assert session["modified_after_edit"] is make_edit
        before = working.stat().st_mtime_ns
        reader = SpaceClaimRunner(output_dir=output_root / "before", ui_mode="hidden")
        try:
            old_catalog, _ = reader.catalog(working, render_candidates=False)
        finally:
            reader.close()
        assert group_name not in named_groups(old_catalog)

        receipt = adapter.save_current_document(session, working, timeout_s=30)
        write_json(output_root / "save-receipt.json", receipt)
        assert receipt["modified_before"] is make_edit
        assert receipt["saved"] is make_edit
        assert receipt["modified_after"] is False
        after = working.stat().st_mtime_ns
        if not make_edit:
            assert before == after

        reader = SpaceClaimRunner(output_dir=output_root / "after", ui_mode="hidden")
        try:
            new_catalog, _ = reader.catalog(working, render_candidates=False)
        finally:
            reader.close()
        assert (group_name in named_groups(new_catalog)) is make_edit
        repeat = adapter.save_current_document(session, working, timeout_s=30)
        write_json(output_root / "already-saved-receipt.json", repeat)
        assert repeat["saved"] is False
        assert working.stat().st_mtime_ns == after
        write_json(
            output_root / "result.json",
            {
                "status": "passed",
                "automated_edit_not_human_acceptance": True,
                "working_geometry": str(working),
                "save": receipt,
                "before_groups": list(named_groups(old_catalog)),
                "after_groups": list(named_groups(new_catalog)),
                "repeated_save": repeat,
            },
        )
    finally:
        write_json(output_root / "close-request.json", request_window_close(session))
