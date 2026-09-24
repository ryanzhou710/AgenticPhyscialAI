"""Bounded, case-neutral controls available to the runtime LLM reviewer."""

from __future__ import annotations

from typing import Any

from src.services.units import convert_length

from .mesh_job import MeshJob

STEP_ORDER = (
    "import_geometry",
    "local_sizing",
    "surface_mesh",
    "describe_geometry",
    "update_boundaries",
    "update_regions",
    "boundary_layers",
    "volume_mesh",
    "final_validation",
)


class RepairState:
    def __init__(self, job: MeshJob):
        self.boundaries = {key: list(value) for key, value in job.boundaries.items()}
        self.global_size = job.global_size
        self.local_refinements = [
            {**item, "source_boundary_name": item["zone"]} for item in job.local_refinements
        ]
        self.boundary_layers = dict(job.boundary_layers)
        self.quality_improvement = False
        self.unit = job.control_unit

    def resolve_units(self, unit: str) -> None:
        """Convert requested dimensions once the Fluent import unit is known."""
        if self.unit is not None and self.unit != unit:
            if self.global_size is not None:
                self.global_size = convert_length(self.global_size, self.unit, unit)
            for item in self.local_refinements:
                item["size"] = convert_length(item["size"], self.unit, unit)
            height = self.boundary_layers.get("first_layer_height")
            if height is not None:
                self.boundary_layers["first_layer_height"] = convert_length(height, self.unit, unit)
        self.unit = unit

    def snapshot(self) -> dict[str, Any]:
        return {
            "boundaries": {key: list(value) for key, value in self.boundaries.items()},
            "global_size": self.global_size,
            "local_refinements": [dict(item) for item in self.local_refinements],
            "boundary_layers": dict(self.boundary_layers),
            "quality_improvement": self.quality_improvement,
            "length_unit": self.unit,
        }

    def apply(
        self,
        action: str,
        parameters: dict[str, Any],
        available_names: set[str],
        *,
        available_names_by_category: dict[str, set[str]] | None = None,
        manual_approved: bool = False,
    ) -> tuple[str, str]:
        if action == "retry_step":
            return "", "Retry without changing controls"
        if action == "set_global_size":
            value = float(parameters["value"])
            self.global_size = value
            return "surface_mesh", f"Set global size to {value}"
        if action == "set_local_size":
            zone = str(parameters["zone"])
            value = float(parameters["value"])
            if zone not in available_names:
                raise ValueError("local-size zone is invalid")
            existing = next((item for item in self.local_refinements if item["zone"] == zone), None)
            if existing is None:
                self.local_refinements.append(
                    {"zone": zone, "size": value, "source_boundary_name": None}
                )
            else:
                existing["size"] = value
            return "local_sizing", f"Set local size on {zone} to {value}"
        if action == "set_growth_rate":
            value = float(parameters["value"])
            self.boundary_layers["growth_rate"] = value
            return "boundary_layers", f"Set boundary-layer growth rate to {value}"
        if action == "set_layer_count":
            value = int(parameters["value"])
            self.boundary_layers["layers"] = value
            return "boundary_layers", f"Set boundary-layer count to {value}"
        if action == "set_first_layer_height":
            value = float(parameters["value"])
            self.boundary_layers["first_layer_height"] = value
            return "boundary_layers", f"Set first-layer height to {value}"
        if action == "set_layer_targets":
            if not manual_approved:
                raise ValueError("boundary-layer label changes require explicit user approval")
            self.boundary_layers["zones"] = list(parameters["zones"])
            self.boundary_layers["scope_specified"] = True
            return "boundary_layers", "Updated requested boundary-layer labels"
        if action == "enable_quality_improvement":
            self.quality_improvement = True
            return "surface_mesh", "Enabled Fluent surface quality improvement"
        if action == "replace_zone_reference":
            category = str(parameters["category"])
            old = str(parameters["old"])
            new = str(parameters["new"])
            if category != "boundary_layers" and new not in available_names:
                raise ValueError("replacement zone does not exist in Fluent")
            if not manual_approved:
                raise ValueError("label replacements require explicit user approval")
            if category.startswith("boundaries."):
                role = category.split(".", 1)[1]
                names = self.boundaries[role]
                if old not in names:
                    raise ValueError("old boundary reference is absent")
                expected = (available_names_by_category or {}).get(category)
                if expected is not None and new not in expected:
                    raise ValueError("replacement label has an incompatible Fluent boundary type")
                other_roles = {
                    name
                    for current_role, current_names in self.boundaries.items()
                    if current_role != role
                    for name in current_names
                }
                if new in other_roles:
                    raise ValueError("replacement label conflicts with another confirmed boundary role")
                self.boundaries[role] = [new if item == old else item for item in names]
                return "update_boundaries", f"Replaced {old} with {new} in {category}"
            if category == "boundary_layers":
                names = self.boundary_layers["zones"]
                if old not in names:
                    raise ValueError("old boundary-layer reference is absent")
                self.boundary_layers["zones"] = [new if item == old else item for item in names]
                self.boundary_layers["scope_specified"] = True
                return "boundary_layers", f"Replaced boundary-layer zone {old} with {new}"
            if category == "local_refinements":
                item = next((row for row in self.local_refinements if row["zone"] == old), None)
                if item is None:
                    raise ValueError("old local-size reference is absent")
                item["zone"] = new
                return "local_sizing", f"Replaced local-size zone {old} with {new}"
            raise ValueError("unsupported zone-reference category")
        raise ValueError("unsupported Fluent repair action: " + action)
