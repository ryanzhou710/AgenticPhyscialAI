"""Small terminal summaries; complete evidence remains in the run directory."""

from cfd_agent.state import PipelineState

LABELS = {
    "prepare": "Prepare working CAD",
    "query_geometry": "Query SpaceClaim geometry and reference views",
    "understand_prompt": "LLM object selection and mesh requirements",
    "verify_selection": "Verify native SpaceClaim selection",
    "extract_volume": "SpaceClaim: extract fluid volume",
    "label_faces": "SpaceClaim: group and label boundaries",
    "validate_cad": "Check CAD and boundary coverage",
    "reload_confirmed_cad": "Read confirmed CAD and boundary groups",
    "launch_fluent": "Start Fluent",
    "import_geometry": "Fluent 1/8: import geometry",
    "local_sizing": "Fluent 2/8: local sizing",
    "surface_mesh": "Fluent 3/8: surface mesh",
    "describe_geometry": "Fluent 4/8: describe geometry",
    "update_boundaries": "Fluent 5/8: update boundaries",
    "update_regions": "Fluent 6/8: update regions",
    "boundary_layers": "Fluent 7/8: boundary layers",
    "volume_mesh": "Fluent 8/8: volume mesh",
    "final_validation": "Check, save and reread mesh",
    "review_failure": "Failure diagnosis",
    "apply_repair": "Apply repair action",
}


def brief(value, limit: int = 350) -> str:
    lines = str(value or "").strip().splitlines()
    text = lines[-1].strip() if lines else ""
    return text if len(text) <= limit else text[:limit] + "... (see run records)"


def progress_node(stage, operation):
    """Observe node results without changing state, routing or repair decisions."""

    def execute(state: PipelineState):
        label = LABELS.get(stage)
        if label:
            print(f"[Running] {label}", flush=True)
        result = operation(state)
        update = result if isinstance(result, dict) else (result.update or {})
        if stage == "review_failure":
            decision = update.get("repair_decision", {})
            if decision.get("action") == "stop":
                print("[Failed] Diagnosis: " + brief(decision.get("diagnosis")), flush=True)
                source = update.get(
                    "repair_decision_source", state.get("repair_decision_source", "unknown")
                )
                reason = update.get(
                    "repair_stop_reason", state.get("repair_stop_reason", "")
                )
                print("  Decision source: " + brief(source), flush=True)
                if reason:
                    print("  Stop reason: " + brief(reason), flush=True)
                if decision.get("diagnosis") == "Reviewer failed":
                    print("  " + brief(decision.get("evidence")), flush=True)
            else:
                print("[Done] Diagnosis: " + brief(decision.get("diagnosis")), flush=True)
                print("  Proposed action: " + brief(decision.get("action")), flush=True)
        elif stage in {"apply_repair", "parameter_confirmation"}:
            target = getattr(result, "goto", None)
            decision = update.get("repair_decision", state.get("repair_decision", {}))
            if update.get("error") or target == "failed":
                print(
                    "[Failed] Repair action: " + brief(update.get("error") or state.get("error")),
                    flush=True,
                )
            elif target == "parameter_confirmation":
                print("  Parameter approval required; no change applied yet.", flush=True)
            elif target == "cancelled":
                print("Cancelled. No further operations will run.", flush=True)
            elif target == "human_confirmation":
                print("  Returned to CAD confirmation; no automatic geometry repair.", flush=True)
            else:
                print("[Done] Action: " + brief(decision.get("action")), flush=True)
                history = update.get("repair_history", [])
                application = history[-1].get("application", {}) if history else {}
                if application.get("description"):
                    print("  Changes: " + brief(application["description"]), flush=True)
                elif decision.get("parameters"):
                    print("  Changes: " + brief(decision["parameters"]), flush=True)
                reverted = application.get("reverted", {})
                if "state_after" in reverted:
                    print(
                        "  Task state after revert: " + brief(reverted["state_after"]), flush=True
                    )
                print(
                    f"  Next: {LABELS.get(target, target)}. Recovery is not yet verified.",
                    flush=True,
                )
        elif label:
            failed = bool(update.get("error"))
            rerun = (
                " (rerun)"
                if state.get("failed_step") == stage and state.get("repair_rounds")
                else ""
            )
            print(f"[{'Failed' if failed else 'Done'}] {label}{rerun}", flush=True)
            if failed:
                print("  " + brief(update["error"]), flush=True)
        return result

    return execute


def show_outcome(outcome: dict) -> None:
    result = outcome.get("result") or {}
    print("\nOverall status: " + str(outcome["status"]).capitalize(), flush=True)
    if result.get("failed_step"):
        print("Stopped at:", LABELS.get(result["failed_step"], result["failed_step"]))
    if result.get("error"):
        print("Error:", brief(result["error"]))
    if outcome["status"] == "cancelled":
        print("No further operations will run. Any open CAD editing window is left unchanged.")
    for key, label in (
        ("confirmed_geometry", "Confirmed CAD"),
        ("mesh", "Mesh"),
        ("mesh_image", "Mesh image"),
    ):
        if result.get(key):
            print(label + ":", result[key])
    print("Diagnosis rounds:", result.get("repair_rounds", outcome.get("repair_rounds", 0)))
    if outcome.get("run_dir"):
        print("Run records:", outcome["run_dir"], flush=True)
