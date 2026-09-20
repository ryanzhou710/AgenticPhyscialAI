# CFD Agent

CFD Agent generates Fluent volume meshes from a SpaceClaim CAD file and a natural-language
prompt. It supports a single connected internal fluid domain with human confirmation before
meshing. If the input is already one closed solid fluid body, it reuses that body directly;
otherwise it uses the SpaceClaim fluid-volume extraction path before meshing. Flow solving is
not included.

The workflow uses LangGraph for orchestration, a language model for interpretation and
failure diagnosis, SpaceClaim for fluid-domain extraction, and PyFluent for meshing.
Meshing uses Fluent Watertight Geometry and poly-hexcore.

## Requirements

- Windows; Python 3.12 is recommended (package metadata allows 3.11–3.13).
- Ansys 2024 R1 (v241), including SpaceClaim, Fluent, and a valid Ansys license.
- An existing `.scdoc` file and a nonempty UTF-8 prompt file.
- Codex CLI installed if using Codex OAuth auto-login.
- Either existing Codex OAuth credentials or an OpenAI API key, plus access to a compatible
  image-capable model.

In OAuth mode, the usual credential location is `%USERPROFILE%\.codex\auth.json`. When the CFD
Agent needs the model and cannot find a readable cache, it automatically runs `codex login` and
opens the browser authorization flow. Complete the authorization in the browser; the workflow
continues after Codex saves the credentials. To use device-code authentication instead, set
`$env:FOAMAGENT_CODEX_DEVICE_AUTH = "1"` before starting the run. If Codex stores credentials
in the OS keyring rather than `auth.json`, configure its credential store to `file`, or set
`CODEX_HOME` / `FOAMAGENT_CODEX_AUTH_PATH` to a readable file-based cache.

OAuth caches contain access tokens and must never be committed to GitHub or copied into chat
messages. API Key mode reads `OPENAI_API_KEY` from the environment and never stores the key in
the run metadata or audit files.
The default model is `gpt-5.6-luna`. `--model` selects another compatible model available
through the selected authentication mode; it does not itself change the authentication mode.

## Quick start

### 1. Install

Open PowerShell in the project root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:AWP_ROOT241 = "C:\Program Files\ANSYS Inc\v241"
```

Replace the Ansys path if necessary, or use `--ansys-root` when starting a run.
Python dependencies are installed automatically; Ansys and model credentials are separate.

### 2. Prepare a prompt

Save a UTF-8 file such as `C:\CFD-inputs\prompt.txt`. Replace the placeholders for your CAD:

```text
Use [reference view] as the directional reference.
Select [opening locations or features] and [the seed face on the inner fluid wall].
Assign [opening name] as an inlet and [opening name] as an outlet.
Treat the remaining fluid boundary as a wall.
Optional: [global size, local refinement, boundary-layer settings, and length units].
```

Specify a reference view when using directions such as left or right.

### 3. Run

Choose one of the following authentication modes. OAuth is the default, so the first example
works without adding an authentication argument.

#### Option A: Codex OAuth (default)

If a readable OAuth cache is not available, the agent runs `codex login` and opens the browser
authorization flow. Complete the authorization once, then the workflow continues.

```powershell
.\.venv\Scripts\cfd-agent.exe run `
  --geometry "cfd inputs\no-panel-b.scdoc" `
  --prompt-file "cfd inputs\prompt.txt" `
  --ui-mode gui `
  --keep-open
```

The same mode can be selected explicitly with `--auth-mode codex_oauth`.

#### Option B: OpenAI API Key

Set the API key in the current PowerShell session, then select `api_key`. The key is not passed as
a command-line argument and is not stored in the run metadata or audit files.

```powershell
$env:OPENAI_API_KEY = "sk-..."
.\.venv\Scripts\cfd-agent.exe run `
  --auth-mode api_key `
  --geometry "cfd inputs\no-panel-b.scdoc" `
  --prompt-file "cfd inputs\prompt.txt" `
  --ui-mode gui `
  --keep-open
```

Do not put the key directly in source code, commit it to Git, or paste it into chat. API Key mode
uses the standard OpenAI Responses API endpoint. The selected model must be available to the
OpenAI Platform project associated with the key. API Key requests use OpenAI Platform billing,
separate from ChatGPT subscription credits.

In PowerShell, each continuation backtick must be the last character on its line. The `>>` prompt
is PowerShell's continuation prompt and should not be copied into the command.

Use `run --help` to see all options. The default UI mode is `hidden`, which still requires
terminal confirmation. `--keep-open` requires `--ui-mode gui`.

## Confirmation and resume

At the CAD pause, inspect or edit the working copy shown in the terminal.

- `yes`: in GUI mode, save unsaved changes in the original editing session, then reread
  the saved groups and continue if the handoff succeeds. Hidden mode uses the saved file.
- `no`: cancel without requesting a save; the SpaceClaim editing window stays open.

Keep existing group names when using the CLI: new or renamed groups with unknown roles
stop the handoff. For these groups, Python callers must first save the CAD, then supply
their roles through `cfd_agent.resume_pipeline` using `boundary_roles` and `action="approve"`.

Failures can trigger limited repair attempts. Numeric repairs to parameters identified
by the model as user-specified require confirmation: enter `accept`, a replacement value
in the displayed unit, or `cancel`. Layer counts require integers.
Complete parameter confirmation in the original process; it needs the live Fluent session.

With `--keep-open`, press Enter at the final terminal prompt to close the retained Fluent
session. Close SpaceClaim separately. Cancellation closes the run's Fluent session.

If the terminal was closed at a CAD confirmation pause, keep the original SpaceClaim
editing session open and resume using the same code version:

Replace `YOUR-RUN-ID` with the name of the relevant folder under `runs`, then run:

```powershell
.\.venv\Scripts\cfd-agent.exe resume --run-dir ".\runs\YOUR-RUN-ID"
```

This command resumes pending CAD confirmation, not parameter confirmation or failed runs.

## Results and limitations

Outputs are created under `runs/<run-id>/` in the current working directory, or the
directory selected with `--output`.

| File | Contents |
|---|---|
| `artifacts/confirmed.scdoc` | Saved CAD copied at the confirmation handoff |
| `artifacts/mesh.msh.h5` | Mesh copied after successful validation |
| `result.json` | Outcome and available details; fields vary for success, failure, and cancellation |

Mesh checks cover quality metrics, negative volumes, execution-label presence, and file
save/readback. They do not establish physical validity or independently verify that
repaired inlet/outlet assignments preserve the confirmed roles. Review the final mesh.

For failures, inspect terminal messages and available run logs. Early startup errors may
occur before `result.json` exists.

Prompts, geometry attributes, and images are sent to the model service. Keep credentials,
private CAD, and generated run data out of public commits.
