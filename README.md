# CFD Agent

CFD Agent turns a SpaceClaim CAD model and a natural-language request into a Fluent Watertight Geometry poly-hexcore volume mesh. LangGraph coordinates the workflow, an LLM interprets the request and selects geometry, SpaceClaim prepares the fluid domain, and PyFluent runs the meshing tasks.

The current scope is one connected internal fluid domain with planar opening contours. Flow solving and multiple fluid domains are not supported.

```text
CAD + request
→ understand the request and select geometry
→ extract or reuse the fluid domain in SpaceClaim and create boundary groups
→ review or edit the working CAD and confirm
→ generate and validate the volume mesh in Fluent
→ export the mesh and archive results and evidence
```

## Geometry selection and diagnostics

The initial SpaceClaim query exports the complete body, face, edge, and loop catalog plus four global reference views. It does **not** render every candidate object. The model first narrows the request to possible openings and an inner-wall seed. SpaceClaim then renders only those requested objects in one batch: openings default to `Selected`; seed faces default to `OwnerContext` and `SelectedProxy`.

At most 12 distinct objects are rendered per detail round and the selection dialogue has at most three detail rounds. Images are reused by object ID and view while the geometry catalog remains valid. If the evidence is still ambiguous, the run pauses for clarification instead of guessing an opening or seed face.

Before extraction, the application resolves every selected face, loop, or edge into one explicit opening record and checks closure, planarity, missing edges, overlapping contours, seed identity, and multi-inner-loop ambiguity. It tries face capping when every opening is exactly represented by one planar end face; otherwise it uses the equivalent edge contours. A failed face attempt may retry once with the same contours as edges in a fresh SpaceClaim process. It never changes the selected objects during that retry.

Each failed workflow stage writes `artifacts/<stage>-error.json`. The terminal prints an English summary with the error code, stage, involved object, reason, suggested next action, and artifact path. Native software feedback remains in the evidence record.

## Requirements

- Windows with Python 3.11–3.13; Python 3.12 is recommended.
- Ansys 2024 R1 (`v241`), including SpaceClaim, Fluent, and a valid license.
- An existing `.scdoc` file and a nonempty UTF-8 text file describing the requested mesh.
- Either readable Codex OAuth credentials or an `OPENAI_API_KEY`, with access to a compatible image-capable model.
- The Codex CLI installed and available on `PATH` if you want the agent to start OAuth login automatically.

## Installation

Run these commands from the project root. A project-local environment avoids importing an older installation from another checkout.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
$env:AWP_ROOT241 = "C:\Program Files\ANSYS Inc\v241"
```

Replace the Ansys path with your actual installation directory. You can also pass `--ansys-root` when running the command.

The installed command is `cfd-agent`. The Python package is `src`, and the equivalent module entry point is:

```powershell
.\.venv\Scripts\python.exe -m src run --help
```

## Prepare the inputs

Store your inputs in a directory you control, for example:

```text
C:\CFD-inputs\duct.scdoc
C:\CFD-inputs\prompt.txt
```

Write `prompt.txt` in UTF-8. Describe the intended openings, their boundary roles, and the seed face using features visible in your CAD. If you use directions such as left or right, specify the reference view.

Use this template as a starting point, replacing the bracketed text:

```text
Use [reference view] as the directional reference.
Extract the internal fluid volume.
Select [opening locations or features] and [the seed face on the inner fluid wall].
Assign [opening name] as an inlet and [opening name] as an outlet.
Treat the remaining fluid boundary as a wall.
Optional: [global size, local refinement, boundary-layer settings, and length units].
```

For example, **only if these features describe your model**:

```text
Use Front as the directional reference. Extract the internal fluid volume.
The left circular opening is inlet_in; the right rectangular opening is outlet_out.
Use the long inner duct face as the extraction seed. Treat remaining faces as walls.
Use a 4 mm global size and three boundary layers on the wall.
```

The example names and geometry are not required inputs or special cases in the pipeline. Selection uses the current geometry catalog, images, request, and software feedback.

By default, the agent extracts a fluid domain. If the CAD already represents the fluid volume, explicitly say so in the request. SpaceClaim performs the native reuse validation during that operation. Unclear or conflicting intent can require clarification.

## Run with Codex OAuth

OAuth is the default authentication mode:

```powershell
.\.venv\Scripts\cfd-agent.exe run `
  --geometry "C:\CFD-inputs\duct.scdoc" `
  --prompt-file "C:\CFD-inputs\prompt.txt" `
  --ui-mode gui
```

The adapter reads `%USERPROFILE%\.codex\auth.json` by default. If no readable credential cache is found, it starts `codex login`; complete the browser login so the run can continue. If login succeeds but credentials remain unreadable, check that the CLI writes an `auth.json` file rather than storing credentials only in a system keyring.

For a nondefault cache location, use `CODEX_HOME` for the directory containing `auth.json`, or `FOAMAGENT_CODEX_AUTH_PATH` for the exact file path. To request device-code login when automatic login is needed:

```powershell
$env:FOAMAGENT_CODEX_DEVICE_AUTH = "1"
```

These environment variable names are the ones currently read by this project's authentication adapter.

## Run with an API key

Set the key in the current PowerShell session and explicitly select API-key authentication:

```powershell
$env:OPENAI_API_KEY = "<your-api-key>"
.\.venv\Scripts\cfd-agent.exe run `
  --auth-mode api_key `
  --geometry "C:\CFD-inputs\duct.scdoc" `
  --prompt-file "C:\CFD-inputs\prompt.txt" `
  --ui-mode gui
```

This mode uses the OpenAI Responses API. Choose a compatible model accessible to your credentials. The project's configured default is `gpt-5.6-luna`; override it with `--model`. Changing the model does not change the authentication mode.

Do not put credentials in the request file or commit them to the repository. Geometry descriptions and images are sent to the configured model service; consider the confidentiality of the CAD before running.

## Runtime options

| Option | Purpose |
|---|---|
| `--model <name>` | Override the configured model. |
| `--ui-mode gui` | Show software windows for inspection and editing. The default is `hidden`. |
| `--keep-open` | Keep Fluent available after completion; requires GUI mode. |
| `--output <directory>` | Set the run directory instead of the default `runs/<run-id>`. |
| `--max-repair-rounds <number>` | Set the per-input repair budget; defaults to 10. |
| `--ansys-root <directory>` | Set the Ansys installation location. |
| `--runtime-root <directory>` | Set the local software staging directory. |

Use `run --help` for all options. In PowerShell, the continuation backtick must be the last character on its line. Do not copy the shell's `>>` continuation prompt into a command.

Hidden mode still requires CAD confirmation in the terminal. Without `--keep-open`, the normal completion path closes the Fluent session. With it, the CLI waits without asking you to press Enter. Currently this wait tracks the worker process, so closing the Fluent window may not be sufficient to release the CLI.

## Review and confirm

Every normal run pauses between SpaceClaim and Fluent:

1. Read the working CAD path printed in the terminal.
2. Review the fluid domain and boundary groups in that working copy. Edit it if necessary.
3. Enter `yes` to continue, or `no` to cancel.
4. Before meshing, the agent rereads the saved CAD and checks its groups against the confirmed roles.

The handoff also requires exactly one positive-volume solid, no free edges, nonempty nonoverlapping face groups, complete face coverage, and both inlet and outlet roles. A failed check stops before Fluent starts.

In GUI mode, `yes` saves the current working document before rereading it. In hidden mode, the agent uses the file already saved on disk, so save any external edits yourself. Cancelling does not approve or save pending CAD edits.

If you rename groups or change their roles, the CLI may be unable to match them to the previous roles and will stop the handoff. For explicit role changes, use the Python API at the pending confirmation, save the working CAD first, and provide the actual group names:

```python
from src import resume_pipeline, run_pipeline

outcome = run_pipeline(
    geometry=r"C:\CFD-inputs\duct.scdoc",
    prompt_path=r"C:\CFD-inputs\prompt.txt",
    ui_mode="gui",
)

# After a CAD-confirmation pause, review and save the working CAD.
# Replace these example names with its actual named groups.
outcome = resume_pipeline(
    run_dir=outcome["run_dir"],
    action="approve",
    boundary_roles={"inlet_in": "inlet", "outlet_out": "outlet", "wall": "wall"},
)

if outcome["status"] == "success":
    result = outcome["result"]
    print(result["boundary_roles"])
    print(result["mesh_requirements"])
    print(result["warnings"] if "warnings" in result else "No warnings")
```

Check `outcome["status"]` and the interrupt kind before resuming: a clarification or mapping request needs a different response from CAD confirmation.

Keep the CLI running while reviewing the CAD or answering other intervention requests. There is no command-line entry point for reopening a paused run after exiting the program; start a new run instead.

The Python API returns `status="paused"` and writes `pause.json` when human input is needed. Use `resume_pipeline()` to submit the user's answer during the active workflow. The CLI handles these calls automatically after each answer.

## Automatic repairs and additional human input

Numeric corrections to inferred or default values can run automatically following diagnosis of software feedback. Every proposed change to a user-specified mesh size or boundary-layer control requires approval; no separate lock flag is needed. The confirmation shows the target, original request, current value, proposed value, units and diagnosis. Accept the proposal, enter a replacement number in the displayed runtime unit, or cancel. Approval applies to this repair only; a later proposal requires confirmation again. Repairs do not authorize relaxing mesh-quality acceptance criteria.

Additional intervention can occur in these situations:

| Situation | What you provide |
|---|---|
| Unclear opening, seed face, or fluid-domain intent | A clarification identifying the intended geometry or operation. |
| Geometry or boundary purpose needs revision | An edited working CAD followed by confirmation. |
| A boundary reference or boundary-layer target must be replaced | The exact Fluent boundary label or labels. |
| A user-specified numeric control needs a change | Approval of the proposal, a replacement value, or cancellation. |
| Repairs exhaust the budget or repeat without progress | A CAD revision if offered by the workflow, or cancellation. |

Read the failed stage, evidence, repair history, and requested action before responding. A similar name or an identical role alone does not establish that two labels refer to the same physical surface. The current repair path asks for explicit input when replacing zone references or boundary-layer targets.

The default budget is 10 automatic repairs, with a cumulative limit of 100. Two consecutive repair-history entries with the same error, action, and parameters count as no progress. Clarification and CAD reconfirmation after a repair request reset the per-run repair budget while retaining cumulative history. **The current implementation does not verify that the CAD actually changed before that reset.**

`resume_pipeline` accepts `clarification`, `parameter_value`, `boundary_replacement`, and `boundary_replacements` for the corresponding intervention. Geometry reconfirmation clears downstream observations and rebuilds the Fluent task from the saved CAD. Locked-parameter and boundary-label approval pauses keep the existing Fluent session open. Approval applies the repair in that session and requests rollback of the affected workflow steps before rerunning them. CAD revision still closes the previous session and rebuilds from the newly confirmed geometry.

## Results and output files

By default, output is stored under `runs/<run-id>` relative to the current directory. Use `--output` to choose another run directory. Files are created as the workflow progresses; an early failure may occur before a mesh or `result.json` is available.

The mesh archive and `result.json` are required terminal outputs. If either cannot be written, the run ends with an archive failure and reports both the original failure, if any, and the archive target on stderr. Copying optional logs or producing a mesh preview records an English warning instead.

| Path inside the run directory | Contents |
|---|---|
| `prompt.txt` | Original request. |
| `run-metadata.json` | Runtime configuration and run identity. |
| `checkpoints.sqlite` | Workflow checkpoints used while the current run is paused for human input. |
| `pause.json` | A saved pause response; the checkpoint determines the current pending action. |
| `artifacts/confirmed.scdoc` | CAD saved at the confirmed handoff. |
| `artifacts/mesh.msh.h5` | Exported mesh when meshing and validation succeed. |
| `artifacts/success-runtime/` or `artifacts/failure-runtime/` | Archived worker logs, transcripts, controls, and runtime evidence. Successful-run evidence excludes files already archived directly under `artifacts/`. |
| `artifacts/<stage>-error.json` | Structured error code, object context, raw error evidence, and suggested action for a failed stage. |
| `artifacts/mesh-preview-error.json` | Optional preview-image failure after successful mesh validation. The mesh remains usable. |
| `result.json` | Outcome and, when available, `boundary_roles`, `mesh_requirements`, final controls, boundary mapping, boundary-type observations, quality checks, repair history, and English warnings. |
| `state/` and `latest-state.json` | Per-stage update records and the complete latest workflow state. |

Review the final execution information rather than assuming the original requested values were used unchanged. Validation checks include recorded Fluent boundary information, mesh quality, negative-volume evidence, and mesh write/readback. Keep CAD, credentials, and private run artifacts out of public commits.

## Development and packaging

Install the development dependencies and run the checks from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Build a wheel for installation in a separate environment:

```powershell
.\.venv\Scripts\python.exe -m pip wheel . --no-deps --wheel-dir dist
```

The source lives directly in `src/`, including `adapters/`, `nodes/`, `services/`, `workers/`, and `prompts/`. Tests live in `tests/`. The old `cfd_agent` import path has no compatibility layer.

Native SpaceClaim and Fluent integration tests require an available installation, license, and suitable CAD input. A complete LLM-driven run also needs model credentials. Passing unit tests with software doubles does not establish that a real Ansys run succeeds.
