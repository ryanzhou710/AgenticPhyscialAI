"""Command-line interface for CFD agent."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from src.adapters.fluent import has_live_client
from src.api import (
    close_run_sessions,
    fail_confirmation,
    inspect_confirmation,
    resume_pipeline,
    run_pipeline,
)
from src.config import PRODUCTION_MODEL, RuntimeConfig
from src.services.cli_output import brief, show_outcome


def _respond_to_intervention(outcome: dict) -> dict:
    pause = outcome["interrupt"]
    print("\nAction required:", pause["message"])
    print("Failed step:", pause.get("failed_step", "unknown"))
    print("Required action:", pause.get("required_action", "See run records."))
    print("Evidence:", brief(pause.get("evidence", {})))
    kind = pause["kind"]
    if kind == "clarification":
        clarification = input("Provide clarification, or type 'cancel': ").strip()
        if clarification.lower() == "cancel":
            return resume_pipeline(run_dir=outcome["run_dir"], action="cancel")
        return resume_pipeline(
            run_dir=outcome["run_dir"], action="clarify", clarification=clarification
        )
    if kind == "boundary_mapping":
        replacement = input("Replacement Fluent boundary label(s), comma-separated, or type 'cancel': ").strip()
        if replacement.lower() == "cancel":
            return resume_pipeline(run_dir=outcome["run_dir"], action="cancel")
        replacements = [item.strip() for item in replacement.split(",") if item.strip()]
        if not replacements:
            print("Enter one or more boundary labels, or cancel.")
            return _respond_to_intervention(outcome)
        return resume_pipeline(
            run_dir=outcome["run_dir"],
            action="approve",
            boundary_replacement=replacements[0],
            boundary_replacements=replacements,
        )
    if kind == "parameter_change":
        evidence = pause.get("evidence", {})
        print("Parameter:", evidence.get("repair_action", "unknown"))
        print("Target:", evidence.get("target", "unknown"))
        print("Originally requested:", evidence.get("requested_value"), evidence.get("requested_unit", ""))
        if evidence.get("original_expression"):
            print("Original expression:", evidence["original_expression"])
        current = evidence.get("current_value")
        print("Current value:", current if current is not None else "unknown", evidence.get("unit", ""))
        print("Proposed value:", evidence.get("proposed_value"), evidence.get("unit", ""))
        print("Reason:", evidence.get("diagnosis", "See run records."))
        choice = input("Type 'accept', a replacement number, or 'cancel': ").strip()
        if choice.lower() == "cancel":
            return resume_pipeline(run_dir=outcome["run_dir"], action="cancel")
        if choice.lower() == "accept":
            return resume_pipeline(run_dir=outcome["run_dir"], action="approve")
        try:
            value = float(choice)
        except ValueError:
            print("Enter a numeric value, 'accept', or 'cancel'.")
            return _respond_to_intervention(outcome)
        return resume_pipeline(run_dir=outcome["run_dir"], action="approve", parameter_value=value)
    raise RuntimeError("Unsupported intervention kind: " + str(kind))


def _roles_for_confirmation(run_dir: str | Path) -> dict[str, str]:
    print("[Running] Save current CAD if modified, then reread groups", flush=True)
    inspection = inspect_confirmation(run_dir, save_current=True)
    saved = bool(inspection["save_receipt"]["saved"])
    print(
        "[Done] "
        + ("Unsaved CAD changes saved." if saved else "CAD already saved; no save needed.")
    )
    print("[Done] Saved boundary groups matched to existing roles.")
    return inspection["roles"]


def _interactive_resume(outcome: dict, keep_open: bool) -> dict:
    while outcome["status"] == "paused":
        pause = outcome["interrupt"]
        if pause.get("kind") != "cad_confirmation":
            outcome = _respond_to_intervention(outcome)
            continue
        print("\nSpaceClaim processing is complete.")
        print("Working CAD:", pause["working_geometry"])
        print("Unsaved changes will be saved automatically before continuing.")
        while True:
            choice = input("Continue to Fluent? [yes/no] ").strip().lower()
            if choice in {"yes", "no"}:
                break
            print("Please enter yes or no.")
        if choice == "no":
            outcome = resume_pipeline(run_dir=outcome["run_dir"], action="cancel")
        else:
            try:
                roles = _roles_for_confirmation(outcome["run_dir"])
            except Exception as error:
                outcome = fail_confirmation(outcome["run_dir"], error)
                print("[Failed] CAD handoff. This run has stopped; no further meshing will run.")
                break
            outcome = resume_pipeline(
                run_dir=outcome["run_dir"], action="approve", boundary_roles=roles
            )
    show_outcome(outcome)
    if keep_open and outcome.get("fluent_session_open"):
        print("Fluent is being kept open. Close its window when you are done.")
        while has_live_client(outcome["run_id"]):
            time.sleep(1)
        close_run_sessions(outcome["run_dir"])
    return outcome


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start a Prompt + SCDOC workflow", allow_abbrev=False)
    run.add_argument("--geometry", type=Path, required=True)
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument("--output", type=Path)
    run.add_argument(
        "--overwrite", action="store_true", help="Replace previous run outputs and start over"
    )
    run.add_argument(
        "--model", default=PRODUCTION_MODEL, help="Model name for the configured provider"
    )
    run.add_argument(
        "--auth-mode",
        choices=["codex_oauth", "api_key"],
        default="codex_oauth",
        help="Model authentication: Codex OAuth or OpenAI API key",
    )
    run.add_argument("--ui-mode", choices=["gui", "hidden"], default="hidden")
    run.add_argument("--keep-open", action="store_true")
    run.add_argument("--max-repair-rounds", type=int, default=10)
    run.add_argument("--ansys-root", help="Ansys 2024 R1 root (otherwise AWP_ROOT241)")
    run.add_argument(
        "--runtime-root", help="Local software staging root (otherwise system temporary directory)"
    )
    run.add_argument("--processors", type=int, default=2)
    run.add_argument("--selection-max-candidates-per-round", type=int, default=12)
    run.add_argument("--selection-max-detail-rounds", type=int, default=3)
    run.add_argument(
        "--fluent-timeout", type=float, default=1800, help="Seconds per worker operation"
    )
    run.add_argument("--spaceclaim-timeout", type=float, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        outcome = run_pipeline(
            geometry=args.geometry,
            prompt_path=args.prompt_file,
            output_dir=args.output,
            overwrite=args.overwrite,
            ui_mode=args.ui_mode,
            keep_open=args.keep_open,
            max_repair_rounds=args.max_repair_rounds,
            runtime_config=RuntimeConfig(
                model=args.model,
                auth_mode=args.auth_mode,
                ansys_root=args.ansys_root,
                runtime_root=args.runtime_root,
                processor_count=args.processors,
                selection_max_candidates_per_round=args.selection_max_candidates_per_round,
                selection_max_detail_rounds=args.selection_max_detail_rounds,
                fluent_operation_timeout_s=args.fluent_timeout,
                spaceclaim_timeout_s=args.spaceclaim_timeout,
            ),
        )
        outcome = _interactive_resume(outcome, args.keep_open)
        return 0 if outcome["status"] in {"success", "paused", "cancelled"} else 1
    except Exception as error:
        print(f"[Failed] {type(error).__name__}: {brief(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
