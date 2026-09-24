"""Structured LLM and pipeline contracts; none contain case-specific answers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OpeningSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    role: Literal["inlet", "outlet", "symmetry"]
    name: str
    description: str
    reason: str


class CadSelectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "ambiguous", "not_found"]
    reference_view: Literal["Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"]
    openings: list[OpeningSelection] = Field(default_factory=list)
    seed_inner_wall_id: str | None = None
    explanation: str
    missing_information: list[str] = Field(default_factory=list)
    fluid_domain_action: Literal["extract", "reuse", "ambiguous"] = "extract"
    fluid_domain_evidence: str = ""

    @model_validator(mode="after")
    def selected_has_objects(self):
        if self.status == "selected" and (not self.openings or not self.seed_inner_wall_id):
            raise ValueError("selected requires openings and seed_inner_wall_id")
        ids = [item.candidate_id for item in self.openings]
        if len(ids) != len(set(ids)):
            raise ValueError("opening candidate IDs must be unique")
        names = [item.name for item in self.openings]
        if len(names) != len(set(names)):
            raise ValueError("boundary names must be unique")
        return self


class NumericControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    unit: Literal["m", "cm", "mm", "in", "ft"]
    source: Literal["user", "inferred"]
    basis: str
    locked: bool = False


class LocalRefinement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    boundary_name: str | None = None
    size: NumericControl


class BoundaryLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = "all walls"
    boundary_names: list[str] | None = None
    layers: int | None = None
    layers_source: Literal["user", "inferred"] | None = None
    layers_locked: bool = False
    growth_rate: float | None = None
    growth_rate_source: Literal["user", "inferred"] | None = None
    growth_rate_locked: bool = False
    first_layer_height: NumericControl | None = None

    @model_validator(mode="after")
    def numeric_sources_are_recorded(self):
        if self.layers == 0 and self.layers_source != "user":
            raise ValueError("Disabling boundary layers requires an explicit user request")
        if (self.layers is None) != (self.layers_source is None):
            raise ValueError("layers and layers_source must be supplied together")
        if (self.growth_rate is None) != (self.growth_rate_source is None):
            raise ValueError("growth_rate and growth_rate_source must be supplied together")
        if self.layers_locked and self.layers is None:
            raise ValueError("layers_locked requires an explicit layer count")
        if self.growth_rate_locked and self.growth_rate is None:
            raise ValueError("growth_rate_locked requires an explicit growth rate")
        return self


class MeshRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_type: Literal["internal"] = "internal"
    length_unit: Literal["m", "cm", "mm", "in", "ft"] | None = None
    global_size: NumericControl | None = None
    local_refinements: list[LocalRefinement] = Field(default_factory=list)
    boundary_layers: BoundaryLayerRequest | None = None
    volume_method: Literal["poly-hexcore"] = "poly-hexcore"
    notes: list[str] = Field(default_factory=list)


class EmptyRepairParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObjectReferenceParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(description="seed_inner_wall_id or opening:<existing opening name>")
    candidate_id: str


class ValueParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float


class LocalSizeParameters(ValueParameters):
    zone: str


class LayerCountParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class LayerTargetsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zones: list[str]


class ZoneReferenceParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal[
        "boundaries.inlet",
        "boundaries.outlet",
        "boundaries.wall",
        "boundaries.symmetry",
        "boundary_layers",
        "local_refinements",
    ]
    old: str
    new: str


REPAIR_PARAMETERS = {
    "retry_step": EmptyRepairParameters,
    "replace_object_reference": ObjectReferenceParameters,
    "set_global_size": ValueParameters,
    "set_local_size": LocalSizeParameters,
    "set_growth_rate": ValueParameters,
    "set_layer_count": LayerCountParameters,
    "set_first_layer_height": ValueParameters,
    "set_layer_targets": LayerTargetsParameters,
    "enable_quality_improvement": EmptyRepairParameters,
    "replace_zone_reference": ZoneReferenceParameters,
    "return_to_human": EmptyRepairParameters,
    "stop": EmptyRepairParameters,
}


def repair_tool_catalog() -> dict[str, Any]:
    return {name: parameters.model_json_schema() for name, parameters in REPAIR_PARAMETERS.items()}


class RepairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis: str
    evidence: str
    action: Literal[
        "retry_step",
        "replace_object_reference",
        "set_global_size",
        "set_local_size",
        "set_growth_rate",
        "set_layer_count",
        "set_first_layer_height",
        "set_layer_targets",
        "enable_quality_improvement",
        "replace_zone_reference",
        "return_to_human",
        "stop",
    ]
    target_step: Literal[
        "prepare",
        "query_geometry",
        "understand_prompt",
        "verify_selection",
        "extract_volume",
        "label_faces",
        "validate_cad",
        "human_confirmation",
        "reload_confirmed_cad",
        "launch_fluent",
        "import_geometry",
        "local_sizing",
        "surface_mesh",
        "describe_geometry",
        "update_boundaries",
        "update_regions",
        "boundary_layers",
        "volume_mesh",
        "final_validation",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def parameters_match_tool(self):
        REPAIR_PARAMETERS[self.action].model_validate(self.parameters)
        return self


class ConfirmationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "cancel"]
    boundary_roles: dict[str, Literal["inlet", "outlet", "wall", "symmetry"]] = Field(
        default_factory=dict
    )

    @field_validator("boundary_roles")
    @classmethod
    def nonempty_names(cls, value):
        if any(not name.strip() for name in value):
            raise ValueError("boundary group names must be non-empty")
        return value


class HumanInterventionPayload(BaseModel):
    """A response to a structured request that cannot safely be automated."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "clarify", "cancel"]
    clarification: str | None = None
    parameter_value: int | float | None = None
    boundary_replacement: str | None = None
    boundary_replacements: list[str] | None = None

    @model_validator(mode="after")
    def clarification_is_supplied_when_requested(self):
        if self.action == "clarify" and not (self.clarification or "").strip():
            raise ValueError("clarify requires non-empty clarification")
        if self.boundary_replacements is not None and any(
            not item.strip() for item in self.boundary_replacements
        ):
            raise ValueError("boundary_replacements cannot contain empty labels")
        return self
