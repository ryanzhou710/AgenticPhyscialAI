# CFD Agent

CFD Agent converts one SpaceClaim CAD model and one UTF-8 natural-language request into a Fluent Watertight Geometry poly-hexcore volume mesh. It supports one connected internal fluid domain and arbitrary planar opening contours. It does not solve a flow field or create multiple fluid domains.

The installed command remains `cfd-agent`; the Python package is now `src`:

```python
from src import resume_pipeline, run_pipeline
```

The module entry point is `python -m src`.

## Workflow

```text
CAD + request
→ LLM selects geometry and explains fluid-domain intent
→ SpaceClaim extracts or reuses the fluid domain and labels groups
→ fixed CAD review/edit/confirmation
→ Fluent creates and validates the volume mesh
→ mesh, logs, effective controls, and evidence are archived
```

The LLM uses the current geometry catalog, supplied images, native-software observations, and repair history. Production code does not select behavior from filenames, fixed object IDs, predefined boundary names, or example-specific geometry rules. Failed examples belong in tests, not in a production whitelist.

## Requirements and installation

- Windows and Python 3.11–3.13; Python 3.12 is recommended.
- Ansys 2024 R1 (`v241`) with SpaceClaim, Fluent, and a valid license for a real mesh run.
- An existing `.scdoc` CAD file and a nonempty UTF-8 request file.
- Codex OAuth credentials or `OPENAI_API_KEY`, with access to a compatible image-capable model.

Use a project-local environment so imports cannot resolve to an older checkout:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:AWP_ROOT241 = "C:\Program Files\ANSYS Inc\v241"
```

The editable install creates `.\.venv\Scripts\cfd-agent.exe`. Build a wheel for a separate clean environment with:

```powershell
.\.venv\Scripts\python.exe -m pip wheel . --wheel-dir dist
```

## Inputs and run

Create an input directory you control, for example `C:\CFD-inputs\`. Put the CAD at `C:\CFD-inputs\duct.scdoc` and write `C:\CFD-inputs\prompt.txt` in UTF-8:

```text
Use Front as the directional reference. Extract the internal fluid volume.
The left circular opening is inlet_in; the right rectangular opening is outlet_out.
Use the long inner duct face as the extraction seed. Treat remaining faces as walls.
Use a 4 mm global size and three boundary layers on the wall.
```

Run with Codex OAuth:

```powershell
.\.venv\Scripts\cfd-agent.exe run `
  --geometry "C:\CFD-inputs\duct.scdoc" `
  --prompt-file "C:\CFD-inputs\prompt.txt" `
  --ui-mode gui `
  --keep-open
```

For an API key, set it in the current shell and add `--auth-mode api_key`:

```powershell
$env:OPENAI_API_KEY = "sk-..."
.\.venv\Scripts\cfd-agent.exe run `
  --auth-mode api_key `
  --geometry "C:\CFD-inputs\duct.scdoc" `
  --prompt-file "C:\CFD-inputs\prompt.txt"
```

Use `--ansys-root` instead of `AWP_ROOT241` when necessary. The default mode is `hidden`; GUI mode is required by `--keep-open`. With `--keep-open`, Fluent remains open after a successful run and the CLI waits until its window closes. Without it, success, cancellation, exceptions, and CAD-revision pauses close the run-owned Fluent session.

## Fluid-domain choice and fixed CAD handoff

The selection response includes `fluid_domain_action` and evidence from the request. If the request does not clearly say that the CAD is already a fluid domain, the agent extracts one. Reuse still requires exactly one positive-volume closed solid without free edges. Ambiguous intent pauses for a clarification.

Every normal run pauses after SpaceClaim creates the working copy and boundary groups:

1. The CLI shows the working CAD path and proposed boundary roles.
2. Review or edit that working copy in SpaceClaim.
3. Enter `yes` to save, reread its actual geometry and groups, and continue; enter `no` to cancel.
4. Fluent receives a new task built from the saved CAD and confirmed groups.

If group names or roles changed, save the CAD and supply roles through the Python API:

```python
from src import resume_pipeline

outcome = resume_pipeline(
    run_dir=r"C:\path\to\runs\20260924-120000-ab12cd34",
    action="approve",
    boundary_roles={"inlet_in": "inlet", "outlet_out": "outlet", "wall": "wall"},
)
```

## Automated repairs and human intervention

Each input version has at most 10 automatic repair rounds by default (`--max-repair-rounds`), and the full run has a hard cumulative limit of 100. Two consecutive failures with the same error, action, and parameters are no progress. Editing CAD or providing a clarification starts a new per-input budget; merely continuing does not.

Ordinary numeric corrections, including stated sizes and layer controls, run automatically when Fluent evidence supports the change. A value pauses only when the request explicitly marks that exact control as unchangeable. Existing quality criteria are never relaxed.

A pause includes the failed step, native evidence, attempted repairs, why automatic work stopped, and a concrete next action:

| Trigger | Required action |
|---|---|
| Opening, seed face, or fluid-domain intent is not unique | Clarify the request or choose the intended object. |
| Confirmed geometry or boundary purpose must change | Edit the working CAD and repeat CAD confirmation. |
| A label cannot be proved to identify the confirmed physical surface, or roles conflict | Supply the exact Fluent label mapping. |
| A locked numeric control needs a change | Approve the proposed value, provide a replacement, or cancel. |
| Repair budget exhausted or no progress, with a concrete CAD remedy | Revise the CAD/request and confirm again. |

Session loss, unsupported operations, and code defects fail with their evidence; the CLI does not ask to ignore them. Mapping changes are checked against Fluent's actual boundary names and types. Name similarity alone never proves a physical-surface correspondence.

In a noninteractive terminal, the workflow writes `pause.json` and returns `paused`. Resume a pending run in the same code version:

```powershell
.\.venv\Scripts\cfd-agent.exe resume --run-dir "C:\path\to\runs\20260924-120000-ab12cd34"
```

`resume_pipeline` also accepts `clarification`, `parameter_value`, `boundary_replacement`, and `boundary_replacements` for structured interventions. Older run formats are rejected explicitly; use their original version or start a new run.

## Results and validation

Each run directory contains:

| Path | Contents |
|---|---|
| `prompt.txt` | Original request. |
| `artifacts/confirmed.scdoc` | CAD saved at the handoff. |
| `artifacts/mesh.msh.h5` | Mesh after Fluent write/readback validation. |
| `artifacts/success-runtime/` or `failure-runtime/` | Worker logs, transcripts, validation logs, images, controls, and runtime evidence. |
| `result.json` | Requested and final controls, confirmed mapping, actual Fluent boundary types, quality checks, repair history, and outcome. |
| `state/` and `latest-state.json` | Durable step-by-step state. |

Final validation reads Fluent's actual boundary names and types, checks them against the final mapping, checks mesh quality and negative-volume evidence, and performs mesh readback. Native SpaceClaim and Fluent integration tests require an available installation, license, credentials, and suitable CAD input; unit-test doubles do not constitute a real software run.

## Development checks

```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```
