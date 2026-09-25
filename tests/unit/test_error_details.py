import json

from src.services import terminal
from src.services.errors import PipelineError
from src.services.execution import _failed, _succeeded


def test_failed_stage_persists_actionable_error_detail(tmp_path):
    result = _failed(
        {"run_dir": str(tmp_path)},
        "extract_volume",
        PipelineError(
            "CAD_OPENING_AMBIGUOUS",
            "internal message",
            stage="extract_volume",
            substep="opening check",
            objects=[{"name": "outlet_out", "role": "outlet", "candidate_id": "F0012"}],
            evidence={"candidate_loop_ids": ["L0007", "L0008"]},
        ),
    )

    artifact = tmp_path / "artifacts" / "extract_volume-error.json"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert result["error_detail"]["code"] == "CAD_OPENING_AMBIGUOUS"
    assert result["error_detail"]["evidence_path"] == str(artifact)
    assert saved["error_detail"]["objects"][0]["candidate_id"] == "F0012"
    assert saved["error_evidence"]["candidate_loop_ids"] == ["L0007", "L0008"]


def test_terminal_summary_includes_stage_object_reason_action_and_path(capsys):
    detail = {
        "code": "CAD_OPENING_AMBIGUOUS",
        "stage": "extract_volume",
        "substep": "opening check",
        "objects": [{"name": "outlet_out", "role": "outlet", "candidate_id": "F0012"}],
        "reason": "two contours",
        "suggested_action": "inspect L0007 and L0008",
        "evidence_path": "artifacts/extraction-error.json",
        "raw_error": "SpaceClaimError: native detail",
    }

    terminal.show_error_detail(detail, phase=terminal._STAGE_FAILED)
    output = capsys.readouterr().err
    assert terminal.LABELS["extract_volume"] in output
    assert "CAD_OPENING_AMBIGUOUS" in output
    assert "outlet_out / F0012" in output
    assert "two contours" in output
    assert "L0007" in output
    assert "artifacts/extraction-error.json" in output
    assert "SpaceClaimError: native detail" not in output


def test_reviewer_stop_keeps_original_failure_visible(capsys):
    original = {
        "code": "CAD_OPENING_AMBIGUOUS",
        "stage": "extract_volume",
        "reason": "two contours",
        "suggested_action": "inspect loops",
    }
    terminal.progress_node(
        "review_failure",
        lambda state: {
            "repair_decision": {"action": "stop", "diagnosis": "reviewer unavailable"},
            "repair_stop_reason": "reviewer request failed",
        },
    )({"error_detail": original})

    output = capsys.readouterr().err
    assert "CAD_OPENING_AMBIGUOUS" in output
    assert "two contours" in output
    assert "reviewer unavailable" in output


def test_completed_rerun_clears_only_the_current_failure_details(tmp_path):
    update = _succeeded(
        {
            "run_dir": str(tmp_path),
            "failed_step": "surface_mesh",
            "error": "native failure",
            "error_detail": {"code": "FLUENT_OPERATION_FAILED"},
            "error_evidence": {"native": "rejected"},
            "repair_stop_reason": "repair_application_exception",
        },
        "surface_mesh",
        {"fluent_steps": {"surface_mesh": {"ok": True}}},
    )

    assert update["failed_step"] == ""
    assert update["error"] == ""
    assert update["error_detail"] == {}
    assert update["error_evidence"] == {}
    assert update["repair_stop_reason"] == ""
