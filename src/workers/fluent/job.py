"""Internal Fluent job assembled from Prompt and confirmed SpaceClaim groups."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MeshJob:
    job_name: str
    geometry_path: Path
    length_unit: str | None
    boundaries: dict[str, list[str]]
    global_size: float | None
    local_refinements: tuple[dict[str, Any], ...]
    boundary_layers: dict[str, Any]
    volume_fill: str
    quality: dict[str, float]
    raw: dict[str, Any]
    control_unit: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MeshJob":
        geometry = Path(data["geometry_path"]).expanduser().resolve()
        if not geometry.is_file() or geometry.suffix.lower() != ".scdoc":
            raise ValueError("The confirmed SpaceClaim geometry is missing")
        unit = data.get("length_unit")
        if unit is not None and unit not in {"m", "cm", "mm", "in", "ft"}:
            raise ValueError("Unsupported geometry length unit")
        boundaries = {
            role: list(data.get("boundaries", {}).get(role, []))
            for role in ("inlet", "outlet", "wall", "symmetry")
        }
        if not boundaries["inlet"] or not boundaries["outlet"]:
            raise ValueError("Confirmed boundaries require at least one inlet and outlet")
        global_size = data.get("global_size")
        refinements = []
        for item in data.get("local_refinements", []):
            if not item.get("zone"):
                raise ValueError("Each local refinement needs a confirmed zone")
            refinements.append({"zone": str(item["zone"]), "size": float(item["size"])})
        layers = dict(data.get("boundary_layers") or {})
        layers.setdefault("zones", [])
        layers.setdefault("layers", None)
        layers.setdefault("growth_rate", None)
        layers.setdefault("first_layer_height", None)
        return cls(
            job_name=str(data.get("job_name") or "cfd-agent-run"),
            geometry_path=geometry,
            length_unit=unit,
            boundaries=boundaries,
            global_size=None if global_size is None else float(global_size),
            local_refinements=tuple(refinements),
            boundary_layers=layers,
            volume_fill="poly-hexcore",
            quality=dict(
                data.get("quality")
                or {
                    "min_orthogonal_quality": 0.1,
                    "max_skewness": 0.95,
                }
            ),
            raw=data,
            control_unit=data.get("control_unit") or unit,
        )
