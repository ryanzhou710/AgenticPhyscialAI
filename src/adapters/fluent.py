"""Bounded JSONL transport to one process-owned PyFluent session."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from src.config import RuntimeConfig


class FluentWorkerError(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


class FluentClient:
    def __init__(self, runtime_dir: str | Path, config: RuntimeConfig | None = None):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.config = config or RuntimeConfig()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "src.workers.fluent_worker", str(self.runtime_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._counter = 0
        self._lock = threading.Lock()
        self._broken = False
        self.stderr_path = self.runtime_dir / "fluent-worker-stderr.log"
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._drain_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read_stdout(self) -> None:
        try:
            for line in self.process.stdout:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def _drain_stderr(self) -> None:
        with self.stderr_path.open("w", encoding="utf-8") as stream:
            for line in self.process.stderr:
                stream.write(line)
                stream.flush()

    def _abort(self) -> None:
        self._broken = True
        if self.process.poll() is None:
            # The live subprocess handle belongs to this client; never kill by image name.
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=10)

    def _protocol_failure(self, message: str, operation: str) -> FluentWorkerError:
        try:
            self._abort()
        except Exception as error:
            message += f"; owned worker cleanup: {error}"
        return FluentWorkerError(
            message,
            {
                "session_lost": True,
                "operation": operation,
                "stderr_log": str(self.stderr_path),
                "worker_pid": self.process.pid,
            },
        )

    def call(self, operation: str, data: dict[str, Any] | None = None) -> Any:
        with self._lock:
            if self._broken or self.process.poll() is not None:
                raise FluentWorkerError(
                    "Fluent worker is unavailable; it will not be restarted",
                    {
                        "session_lost": True,
                        "stderr_log": str(self.stderr_path),
                    },
                )
            self._counter += 1
            message = {"id": self._counter, "operation": operation, "data": data or {}}
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
                self.process.stdin.flush()
                timeout = (
                    self.config.fluent_start_timeout_s + 60
                    if operation == "launch"
                    else 45
                    if operation == "close"
                    else self.config.fluent_operation_timeout_s
                )
                line = self._responses.get(timeout=timeout)
                if line is None:
                    raise EOFError("Worker exited without a response")
                response = json.loads(line)
                if not isinstance(response, dict) or response.get("id") != self._counter:
                    raise ValueError("Worker response ID mismatch")
            except queue.Empty:
                raise self._protocol_failure(
                    f"Fluent operation timed out after {timeout}s", operation
                )
            except (OSError, EOFError, ValueError) as error:
                raise self._protocol_failure(
                    f"Fluent protocol failed: {error}", operation
                ) from error
            if not response.get("ok"):
                raise FluentWorkerError(
                    response.get("error", "Fluent operation failed"), response.get("evidence")
                )
            return response.get("result")

    def close(self) -> None:
        if self._broken or self.process.poll() is not None:
            return
        try:
            self.call("close")
            self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self._abort()


_CLIENTS: dict[str, FluentClient] = {}


def get_client(
    run_id: str, runtime_dir: str | Path, config: RuntimeConfig | None = None
) -> FluentClient:
    # Keep unhealthy clients associated with their run: no implicit session restart.
    if run_id not in _CLIENTS:
        _CLIENTS[run_id] = FluentClient(runtime_dir, config)
    return _CLIENTS[run_id]


def close_client(run_id: str) -> None:
    client = _CLIENTS.pop(run_id, None)
    if client is not None:
        client.close()


def has_live_client(run_id: str) -> bool:
    client = _CLIENTS.get(run_id)
    return bool(client and not client._broken and client.process.poll() is None)
