"""Persistent JSONL worker owning one PyFluent meshing session."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from pathlib import Path

from src.config import RuntimeConfig

from .fluent_tasks import WatertightMeshingRunner
from .mesh_job import MeshJob
from .repair_protocol import RepairState


class FluentWorker:
    def __init__(self, runtime_dir: str | Path):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.session = None
        self.runner = None
        self.controls = None
        self.job = None
        self.ui_mode = "hidden"
        self.config = RuntimeConfig()

    def dispatch(self, operation: str, data: dict):
        if operation == "initialize":
            self.config = RuntimeConfig.model_validate(data.get("runtime_config", {}))
            if self.config.ansys_root:
                os.environ["AWP_ROOT241"] = self.config.ansys_root
            self.ui_mode = data["ui_mode"]
            self.job = MeshJob.from_dict(data["job"])
            return {"ready": True}
        if operation == "launch":
            import ansys.fluent.core as pyfluent

            if self.session is not None:
                self.close()
            self.session = pyfluent.launch_fluent(
                product_version=self.config.fluent_version,
                mode="meshing",
                dimension=3,
                ui_mode="gui" if self.ui_mode == "gui" else "hidden_gui",
                processor_count=self.config.processor_count,
                cwd=str(self.runtime_dir),
                start_timeout=self.config.fluent_start_timeout_s,
            )
            self.controls = RepairState(self.job)
            self.runner = WatertightMeshingRunner(
                self.session,
                self.job,
                self.controls,
                self.job.geometry_path,
                self.runtime_dir / "mesh.msh.h5",
                self.runtime_dir / "fluent.trn",
                lambda *values: print(*values, flush=True),
            )
            return {"version": str(self.session.get_fluent_version())}
        if operation == "execute_step":
            if self.runner is None:
                raise RuntimeError("Fluent has not been launched")
            result = self.runner.execute_step(data["step"])
            if data["step"] == "final_validation":
                result["final_execution"] = {
                    "controls": self.controls.snapshot(),
                    "actual_boundary_types": self.runner.actual_boundary_types(),
                }
            return result
        if operation == "observe":
            return {
                "workflow": self.runner.workflow_snapshot() if self.runner else [],
                "controls": self.controls.snapshot() if self.controls else {},
                "names": sorted(self.runner.available_names()) if self.runner else [],
                "healthy": bool(self.session and self.session.is_server_healthy()),
            }
        if operation == "repair":
            if self.runner is None or self.controls is None:
                raise RuntimeError("Fluent has not been launched")
            action = data["action"]
            target = data["target_step"]
            available = self.runner.available_names()
            earliest, description = self.controls.apply(
                action,
                data.get("parameters", {}),
                available,
                available_names_by_category=self.runner.available_names_by_category(),
                manual_approved=bool(data.get("manual_approved")),
            )
            self.runner.record_repair(action, data.get("parameters", {}))
            resume = target if action == "retry_step" else earliest
            reverted = self.runner.revert_from(resume)
            return {
                "resume": resume,
                "description": description,
                "reverted": reverted,
                "controls": self.controls.snapshot(),
            }
        if operation == "picture":
            if self.session is None:
                raise RuntimeError("Fluent has not been launched")
            self.session.tui.display.boundary_grid()
            self.session.tui.display.set.picture.driver.png()
            path = self.runtime_dir / "final-mesh.png"
            self.session.tui.display.save_picture(str(path))
            return {"path": str(path), "bytes": path.stat().st_size}
        if operation == "prism_controls":
            if self.session is None:
                raise RuntimeError("Fluent has not been launched")
            path = self.runtime_dir / "prism-controls.pzmcontrol"
            self.session.tui.mesh.scoped_prisms.write(str(path))
            return {"path": str(path), "text": path.read_text(encoding="utf-8")}
        if operation == "close":
            self.close()
            return {"closed": True}
        raise ValueError("Unknown Fluent worker operation: " + operation)

    def close(self):
        try:
            if self.runner is not None:
                self.runner.close_transcript()
            if self.session is not None:
                self.session.exit(timeout=30)
        finally:
            self.runner = None
            self.session = None


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m src.workers.fluent_worker RUNTIME_DIR")
    runtime = Path(sys.argv[1]).resolve()
    worker = FluentWorker(runtime)
    protocol = sys.stdout
    with (runtime / "fluent-worker.log").open("a", encoding="utf-8", buffering=1) as log:
        try:
            for line in sys.stdin:
                message = json.loads(line)
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    try:
                        result = worker.dispatch(message["operation"], message.get("data", {}))
                        response = {"id": message["id"], "ok": True, "result": result}
                    except Exception as error:
                        session_lost = False
                        try:
                            import grpc

                            cause = error
                            while cause is not None:
                                if (
                                    isinstance(cause, grpc.RpcError)
                                    and cause.code() == grpc.StatusCode.UNAVAILABLE
                                ):
                                    session_lost = True
                                    break
                                cause = cause.__cause__
                        except Exception:
                            pass
                        response = {
                            "id": message["id"],
                            "ok": False,
                            "error": str(error),
                            "evidence": {
                                **(getattr(error, "observation", {}) or {}),
                                "session_lost": session_lost,
                            },
                            "traceback": traceback.format_exc(),
                        }
                        print(response["traceback"], flush=True)
                print(
                    json.dumps(response, ensure_ascii=True, default=str), file=protocol, flush=True
                )
                if message["operation"] == "close":
                    break
        finally:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
