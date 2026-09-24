"""Length conversions shared by task construction and Fluent execution."""

from __future__ import annotations

METRES_PER_UNIT = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048}


def convert_length(value: float, source_unit: str, target_unit: str) -> float:
    return float(value) * METRES_PER_UNIT[source_unit] / METRES_PER_UNIT[target_unit]


def control_in_metres(control: dict | None) -> float | None:
    if control is None:
        return None
    return convert_length(control["value"], control["unit"], "m")
