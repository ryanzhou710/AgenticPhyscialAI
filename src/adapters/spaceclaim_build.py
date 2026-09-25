"""Host adapter for state-changing SpaceClaim V241 operations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from src.config import RuntimeConfig
from src.services.errors import PipelineError
from src.services.openings import resolve_extraction_selection

from .spaceclaim_query import SpaceClaimError, SpaceClaimRunner
from .windows_process import process_creation_time


class SpaceClaimBuildAdapter:
    def __init__(
        self,
        *,
        runtime_dir: str | Path,
        ui_mode: str,
        timeout_s: float | None = None,
        config: RuntimeConfig | None = None,
    ):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.ui_mode = ui_mode
        self.config = config or RuntimeConfig()
        self.timeout_s = timeout_s if timeout_s is not None else self.config.spaceclaim_timeout_s
        workers = Path(__file__).resolve().parents[1] / "workers" / "spaceclaim"
        self.script = self.runtime_dir / "build.py"
        shutil.copy2(workers / "build.py", self.script)
        self.common_script = self.runtime_dir / "common.py"
        shutil.copy2(workers / "common.py", self.common_script)
        self.save_script = self.runtime_dir / "save.py"
        shutil.copy2(workers / "save.py", self.save_script)

    def _execute(
        self, operation: str, payload: dict[str, Any], *, keep_open: bool = False
    ) -> dict[str, Any]:
        call_id = f"{operation}-{uuid.uuid4().hex[:10]}"
        response = self.runtime_dir / f"{call_id}-response.json"
        request = self.runtime_dir / f"{call_id}-request.json"
        record = {
            "operation": operation,
            "ui_mode": self.ui_mode,
            "folder": str(self.runtime_dir),
            "response": str(response),
            "keep_open": keep_open,
            **payload,
        }
        request.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding="ascii")
        environment = dict(
            os.environ,
            CFD_AGENT_SC_BUILD_REQUEST=str(request),
            CFD_AGENT_SC_COMMON=str(self.common_script),
            CFD_AGENT_SC_SAVE=str(self.save_script),
        )
        command = [
            str(SpaceClaimRunner.executable(self.config)),
            "/UseLicenseMode=true",
            "/RunScript=" + str(self.script),
            "/ScriptAPI=241",
            "/ExitAfterScript=" + ("False" if keep_open else "True"),
            "/Splash=False",
            "/Welcome=False",
            "/Headless=" + ("False" if self.ui_mode == "gui" else "True"),
        ]
        startup = subprocess.STARTUPINFO()
        if self.ui_mode == "hidden":
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        log = (self.runtime_dir / f"{call_id}.log").open("wb")
        process = subprocess.Popen(
            command,
            cwd=self.runtime_dir,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            startupinfo=startup,
        )
        deadline = time.monotonic() + self.timeout_s
        creation_time = process_creation_time(process.pid)
        while not response.is_file():
            if process.poll() is not None:
                log.close()
                raise SpaceClaimError(f"SpaceClaim ended before {operation} produced a response")
            if time.monotonic() > deadline:
                process.kill()
                log.close()
                raise TimeoutError(f"SpaceClaim {operation} timed out")
            time.sleep(0.25)
        result = json.loads(response.read_text(encoding="utf-8-sig"))
        if not keep_open:
            process.wait(timeout=60)
        log.close()
        result["process_id"] = process.pid
        result["process_creation_time"] = creation_time
        result["kept_open"] = bool(keep_open)
        result["record_path"] = str(response)
        if not result.get("ok"):
            raise SpaceClaimError(
                result.get("error", f"SpaceClaim {operation} failed"),
                detail=result.get("error_detail"),
                evidence={"native_result": result, "record_path": str(response)},
            )
        return result

    @staticmethod
    def save_current_document(
        session: dict[str, Any], geometry: str | Path, *, timeout_s: float
    ) -> dict[str, Any]:
        """Ask the existing editing session to save; never launch a replacement session."""
        bridge = session.get("save_bridge")
        if not bridge:
            raise SpaceClaimError("This editing session has no current-document save connection")
        pid = session.get("process_id")
        started = session.get("process_creation_time")
        if not pid or started is None or process_creation_time(pid) != started:
            raise SpaceClaimError("The SpaceClaim editing session is no longer available")
        request_path = Path(bridge["request_path"])
        call_id = uuid.uuid4().hex
        response = request_path.parent / f"save-current-document-{call_id}-response.json"
        request = {
            "id": call_id,
            "operation": "save_if_modified",
            "document_path": str(Path(geometry).resolve()),
            "response": str(response),
        }
        temporary = request_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(request, ensure_ascii=True), encoding="ascii")
        temporary.replace(request_path)
        deadline = time.monotonic() + timeout_s
        while not response.is_file():
            if process_creation_time(pid) != started:
                raise SpaceClaimError("The SpaceClaim editing session closed before saving")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "SpaceClaim current-document save timed out; Fluent was not started"
                )
            time.sleep(0.25)
        result = json.loads(response.read_text(encoding="utf-8-sig"))
        if result.get("id") != call_id or not result.get("ok"):
            raise SpaceClaimError(result.get("error", "SpaceClaim did not acknowledge the save"))
        return {**result, "record_path": str(response)}

    def extract_volume(
        self,
        *,
        source: str | Path,
        output: str | Path,
        catalog: dict[str, Any],
        selection_plan: dict[str, Any],
    ) -> dict[str, Any]:
        selection = resolve_extraction_selection(catalog, selection_plan)
        strategy = selection["extraction_strategy"]
        print(
            "[Running] Fluid-domain extraction using explicitly selected " + strategy + " objects.",
            flush=True,
        )
        try:
            result = self._execute(
                "extract_volume",
                {
                    "input": str(Path(source).resolve()),
                    "output": str(Path(output).resolve()),
                    "catalog": catalog,
                    "terminal_records": selection["terminal_records"],
                    "seed_face_id": selection["seed_face_id"],
                    "extraction_strategy": strategy,
                },
            )
            result["extraction_attempts"] = [{"strategy": strategy, "status": "success"}]
            return result
        except (TimeoutError, FileNotFoundError) as error:
            raise PipelineError(
                "CAD_EXTRACTION_RUNTIME_FAILED",
                "The SpaceClaim extraction timed out or its runtime environment is unavailable.",
                stage="extract_volume",
                substep="volume extraction",
                suggested_action="Check the license, SpaceClaim installation, and extraction timeout.",
                evidence={"attempts": [{"strategy": strategy, "status": "failed", "error": str(error)}]},
            ) from error
        except SpaceClaimError as error:
            raise PipelineError(
                "CAD_VOLUME_EXTRACT_FAILED",
                "SpaceClaim could not create a positive-volume body from the explicitly selected objects.",
                stage="extract_volume",
                substep="volume extraction",
                suggested_action="Inspect the selected objects and the native extraction record.",
                evidence={
                    "attempts": [{"strategy": strategy, "status": "failed", "error": str(error), "detail": error.detail}],
                    **error.evidence,
                },
            ) from error

    def isolate_body(
        self,
        *,
        source: str | Path,
        output: str | Path,
        catalog: dict[str, Any],
        target_body_id: str,
    ) -> dict[str, Any]:
        return self._execute(
            "isolate_body",
            {
                "input": str(Path(source).resolve()),
                "output": str(Path(output).resolve()),
                "catalog": catalog,
                "target_body_id": target_body_id,
            },
        )

    def label_faces(
        self,
        *,
        source: str | Path,
        output: str | Path,
        catalog: dict[str, Any],
        target_body_id: str,
        groups: list[dict[str, Any]],
        keep_editor_open: bool,
    ) -> dict[str, Any]:
        return self._execute(
            "label_faces",
            {
                "input": str(Path(source).resolve()),
                "output": str(Path(output).resolve()),
                "catalog": catalog,
                "target_body_id": target_body_id,
                "groups": groups,
            },
            keep_open=keep_editor_open,
        )
