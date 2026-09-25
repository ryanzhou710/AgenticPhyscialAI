"""Concise English terminal summaries backed by durable run artifacts."""

from __future__ import annotations

import sys
from typing import Any

from src.state import PipelineState

LABELS = {
    "prepare": "Prepare CAD working copy",
    "query_geometry": "Read SpaceClaim geometry and reference views",
    "understand_prompt": "LLM screening and extraction selection",
    "verify_selection": "Verify native SpaceClaim objects",
    "extract_volume": "Extract fluid domain",
    "select_fluid_body": "Select target fluid body",
    "plan_boundary_groups": "Plan extracted-fluid boundary groups",
    "label_faces": "Create fluid-domain boundary groups",
    "validate_cad": "Validate CAD",
    "reload_confirmed_cad": "Hand off confirmed CAD",
    "launch_fluent": "Launch Fluent",
    "import_geometry": "Fluent: import geometry",
    "local_sizing": "Fluent: local sizing",
    "surface_mesh": "Fluent: surface mesh",
    "describe_geometry": "Fluent: describe geometry",
    "update_boundaries": "Fluent: update boundaries",
    "update_regions": "Fluent: update regions",
    "boundary_layers": "Fluent: boundary layers",
    "volume_mesh": "Fluent: volume mesh",
    "final_validation": "Validate and archive mesh",
    "review_failure": "Diagnose failure",
    "apply_repair": "Apply repair",
}

_RUNNING = "Running"
_DONE = "Complete"
_RECOVERED = "Recovered"
_STAGE_FAILED = "Stage failed; diagnosing"
_FINAL_FAILED = "Final failure"


def brief(value: object, limit: int = 350) -> str:
    lines = str(value or "").strip().splitlines()
    text = lines[-1].strip() if lines else ""
    return text if len(text) <= limit else text[:limit] + "... (see run records)"


def _object_text(objects: object) -> str:
    if not isinstance(objects, list) or not objects:
        return ""
    rows = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        bits = [str(item[key]) for key in ("role", "name", "candidate_id") if item.get(key)]
        if bits:
            rows.append(" / ".join(bits))
    return "; ".join(rows)


def show_error_detail(detail: dict[str, Any], *, phase: str) -> None:
    """Emit actionable English diagnostics to stderr; raw evidence stays in run records."""

    stage = str(detail.get("stage") or "unknown")
    substep = detail.get("substep")
    label = LABELS.get(stage, stage)
    print(f"[{phase}] {label}" + (f" / {substep}" if substep else ""), file=sys.stderr)
    if detail.get("code"):
        print("Error code: " + str(detail["code"]), file=sys.stderr)
    objects = _object_text(detail.get("objects"))
    if objects:
        print("Object: " + objects, file=sys.stderr)
    print("Reason: " + str(detail.get("reason") or "Cause is not yet known."), file=sys.stderr)
    if detail.get("suggested_action"):
        print("Next step: " + str(detail["suggested_action"]), file=sys.stderr)
    if detail.get("evidence_path"):
        print("Detailed record: " + str(detail["evidence_path"]), file=sys.stderr)
    if detail.get("evidence_write_error"):
        print("Detailed-record write failed: " + str(detail["evidence_write_error"]), file=sys.stderr)


def progress_node(stage: str, operation):
    """Observe a graph node without changing its state or routing decision."""

    def execute(state: PipelineState):
        label = LABELS.get(stage)
        if label:
            print(f"[{_RUNNING}] {label}", flush=True)
        result = operation(state)
        update = result if isinstance(result, dict) else (result.update or {})
        if stage == "review_failure":
            decision = update.get("repair_decision", {})
            if decision.get("action") == "stop":
                original = update.get("error_detail") or state.get("error_detail")
                if original:
                    show_error_detail(original, phase=_FINAL_FAILED)
                print(
                    f"[{_FINAL_FAILED}] " + brief(decision.get("diagnosis")),
                    file=sys.stderr,
                    flush=True,
                )
                reason = update.get("repair_stop_reason", state.get("repair_stop_reason", ""))
                if reason:
                    print("Stop reason: " + brief(reason), file=sys.stderr)
            else:
                print(f"[{_DONE}] {LABELS[stage]}: " + brief(decision.get("diagnosis")), flush=True)
        elif stage == "apply_repair":
            target = getattr(result, "goto", None)
            if target == "failed":
                print(
                    f"[{_FINAL_FAILED}] " + brief(state.get("error")),
                    file=sys.stderr,
                    flush=True,
                )
                reason = update.get("repair_stop_reason")
                if reason:
                    print("Stop reason: " + brief(reason), file=sys.stderr, flush=True)
            elif update.get("error"):
                print(
                    f"[{_STAGE_FAILED}] " + brief(update["error"]),
                    file=sys.stderr,
                    flush=True,
                )
            elif target == "human_intervention":
                print("[Waiting for user input] A structured user decision is required to continue.", flush=True)
            elif target == "cancelled":
                print("[Cancelled] No further operations will run.", flush=True)
            else:
                print(f"[{_DONE}] {LABELS[stage]}", flush=True)
        elif label:
            failed = bool(update.get("error"))
            rerun = state.get("failed_step") == stage and bool(state.get("repair_rounds"))
            if failed:
                print(f"[{_STAGE_FAILED}] {label}", flush=True)
                detail = update.get("error_detail", state.get("error_detail", {}))
                if detail:
                    show_error_detail(detail, phase=_STAGE_FAILED)
                else:
                    print("Reason: " + brief(update["error"]), file=sys.stderr)
            else:
                status = _RECOVERED if rerun else _DONE
                print(f"[{status}] {label}", flush=True)
        return result

    return execute


def show_outcome(outcome: dict[str, Any]) -> None:
    result = outcome.get("result") or {}
    stream = sys.stderr if outcome.get("status") == "failed" else sys.stdout
    print("\nOverall status: " + str(outcome["status"]), file=stream, flush=True)
    if result.get("failed_step"):
        print("Stopped stage: " + LABELS.get(result["failed_step"], result["failed_step"]), file=sys.stderr)
    if result.get("error"):
        detail = result.get("error_detail")
        if detail:
            show_error_detail(detail, phase=_FINAL_FAILED)
        else:
            print("Error: " + brief(result["error"]), file=sys.stderr)
    for key, label in (("confirmed_geometry", "Confirmed CAD"), ("mesh", "Mesh"), ("mesh_image", "Mesh image")):
        if result.get(key):
            print(label + ":", result[key])
    print("Diagnosis rounds: " + str(result.get("repair_rounds", outcome.get("repair_rounds", 0))))
    if outcome.get("run_dir"):
        print("Run records: " + str(outcome["run_dir"]), flush=True)
