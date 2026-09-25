"""Bounded, case-neutral controls available to the runtime LLM reviewer."""

from __future__ import annotations

from typing import Any

from src.services.contracts import repair_action_spec
from src.services.units import convert_length

from .job import MeshJob

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
        spec = repair_action_spec(action)
        if spec.worker_handler is None:
            raise ValueError("repair action cannot be applied by the Fluent worker: " + action)
        if spec.approval == "boundary_mapping" and not manual_approved:
            raise ValueError("boundary mapping changes require explicit user approval")
        validated = spec.parameters.model_validate(parameters).model_dump(mode="json")
        handler = getattr(self, spec.worker_handler, None)
        if handler is None:
            raise ValueError("registered Fluent repair handler is missing: " + spec.worker_handler)
        resume = spec.resume_for(validated)
        description = handler(
            validated,
            available_names=available_names,
            available_names_by_category=available_names_by_category or {},
        )
        return resume or "", description

    def _apply_retry(self, parameters: dict[str, Any], **_: Any) -> str:
        return "Retry without changing controls"

    def _apply_global_size(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        value = float(parameters["value"])
        self.global_size = value
        return f"Set global size to {value}"

    def _apply_local_size(
        self, parameters: dict[str, Any], *, available_names: set[str], **_: Any
    ) -> str:
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
        return f"Set local size on {zone} to {value}"

    def _apply_growth_rate(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        value = float(parameters["value"])
        self.boundary_layers["growth_rate"] = value
        return f"Set boundary-layer growth rate to {value}"

    def _apply_layer_count(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        value = int(parameters["value"])
        self.boundary_layers["layers"] = value
        return f"Set boundary-layer count to {value}"

    def _apply_first_layer_height(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        value = float(parameters["value"])
        self.boundary_layers["first_layer_height"] = value
        return f"Set first-layer height to {value}"

    def _apply_layer_targets(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        self.boundary_layers["zones"] = list(parameters["zones"])
        self.boundary_layers["scope_specified"] = True
        return "Updated requested boundary-layer labels"

    def _apply_quality_improvement(
        self, parameters: dict[str, Any], **_: Any
    ) -> str:
        self.quality_improvement = True
        return "Enabled Fluent surface quality improvement"

    def _apply_zone_reference(
        self,
        parameters: dict[str, Any],
        *,
        available_names: set[str],
        available_names_by_category: dict[str, set[str]],
        **_: Any,
    ) -> str:
        category = str(parameters["category"])
        old = str(parameters["old"])
        new = str(parameters["new"])
        if category != "boundary_layers" and new not in available_names:
            raise ValueError("replacement zone does not exist in Fluent")
        if category.startswith("boundaries."):
            role = category.split(".", 1)[1]
            names = self.boundaries[role]
            if old not in names:
                raise ValueError("old boundary reference is absent")
            expected = available_names_by_category.get(category)
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
            return f"Replaced {old} with {new} in {category}"
        if category == "boundary_layers":
            names = self.boundary_layers["zones"]
            if old not in names:
                raise ValueError("old boundary-layer reference is absent")
            self.boundary_layers["zones"] = [new if item == old else item for item in names]
            self.boundary_layers["scope_specified"] = True
            return f"Replaced boundary-layer zone {old} with {new}"
        if category == "local_refinements":
            item = next((row for row in self.local_refinements if row["zone"] == old), None)
            if item is None:
                raise ValueError("old local-size reference is absent")
            item["zone"] = new
            return f"Replaced local-size zone {old} with {new}"
        raise ValueError("unsupported zone-reference category")
