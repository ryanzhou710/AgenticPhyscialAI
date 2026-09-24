"""Serializable, explicit host/runtime configuration (not simulation intent)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PRODUCTION_MODEL = "gpt-5.6-luna"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ansys_root: str | None = None
    runtime_root: str | None = None
    fluent_version: Literal["24.1.0"] = "24.1.0"
    model: str = Field(default=PRODUCTION_MODEL, min_length=1, pattern=r"\S")
    auth_mode: Literal["codex_oauth", "api_key"] = "codex_oauth"
    processor_count: int = Field(default=2, ge=1)
    spaceclaim_timeout_s: float = Field(default=900, gt=0)
    fluent_start_timeout_s: float = Field(default=180, gt=0)
    fluent_operation_timeout_s: float = Field(default=1800, gt=0)
    model_timeout_s: float = Field(default=300, gt=0)

    def spaceclaim_executable(self) -> Path:
        root = self.ansys_root or os.environ.get("AWP_ROOT241")
        if not root:
            raise FileNotFoundError(
                "Set AWP_ROOT241 or --ansys-root to the Ansys 2024 R1 installation"
            )
        executable = Path(root) / "scdm" / "SpaceClaim.exe"
        if not executable.is_file():
            raise FileNotFoundError(f"SpaceClaim executable not found: {executable}")
        return executable.resolve()

    def staging_root(self) -> Path:
        configured = self.runtime_root or os.environ.get("CFD_AGENT_RUNTIME_ROOT")
        return (
            Path(configured or (Path(tempfile.gettempdir()) / "cfd-agent")).expanduser().resolve()
        )


def config_from_state(state: dict) -> RuntimeConfig:
    return RuntimeConfig.model_validate(state.get("runtime_config", {}))
