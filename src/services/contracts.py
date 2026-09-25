"""Structured LLM and pipeline contracts; none contain case-specific answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OpeningSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_kind: Literal["face", "loop", "edges"]
    object_ids: list[str] = Field(min_length=1)
    role: Literal["inlet", "outlet", "symmetry"]
    name: str
    description: str
    reason: str

    @model_validator(mode="after")
    def selection_is_explicit(self):
        if self.selection_kind in {"face", "loop"} and len(self.object_ids) != 1:
            raise ValueError("face and loop opening selections require exactly one object ID")
        return self


class CadSelectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "ambiguous", "not_found"]
    reference_view: Literal["Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"]
    openings: list[OpeningSelection] = Field(default_factory=list)
    seed_inner_wall_id: str | None = None
    fluid_body_id: str | None = None
    extraction_strategy: Literal["faces", "edges"] | None = None
    explanation: str
    missing_information: list[str] = Field(default_factory=list)
    fluid_domain_action: Literal["extract", "reuse", "ambiguous"] = "extract"
    fluid_domain_evidence: str = ""

    @model_validator(mode="after")
    def selected_has_objects(self):
        if self.status != "selected":
            return self
        if self.fluid_domain_action == "extract":
            if not self.openings or not self.seed_inner_wall_id or not self.extraction_strategy:
                raise ValueError("extract requires openings, seed_inner_wall_id, and extraction_strategy")
            if self.fluid_body_id is not None:
                raise ValueError("extract must not include fluid_body_id")
            expected_kinds = (
                {"face"} if self.extraction_strategy == "faces" else {"loop", "edges"}
            )
            if any(opening.selection_kind not in expected_kinds for opening in self.openings):
                raise ValueError("opening selection_kind must match extraction_strategy")
        elif self.fluid_domain_action == "reuse":
            if not self.fluid_body_id:
                raise ValueError("reuse requires fluid_body_id")
            if self.openings or self.seed_inner_wall_id or self.extraction_strategy:
                raise ValueError("reuse must select only fluid_body_id")
        else:
            raise ValueError("selected requires extract or reuse fluid_domain_action")
        names = [item.name for item in self.openings]
        if len(names) != len(set(names)):
            raise ValueError("boundary names must be unique")
        return self


DetailView = Literal["Selected", "OwnerContext", "SelectedProxy"]


class CandidateDetailRequest(BaseModel):
    """One candidate whose visual evidence is needed before final selection."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    purpose: Literal["opening", "seed", "body", "boundary"]
    views: list[DetailView] = Field(default_factory=list)
    reason: str

    @field_validator("views")
    @classmethod
    def views_are_unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("detail views must be unique")
        return value


class CadSelectionScreening(BaseModel):
    """First LLM pass: request visual evidence without making a final selection."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_details", "ambiguous", "not_found"]
    reference_view: Literal["Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"]
    candidates: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)
    fluid_domain_action: Literal["extract", "reuse", "ambiguous"] = "extract"
    fluid_domain_evidence: str = ""

    @model_validator(mode="after")
    def needs_details_has_candidates(self):
        if self.status == "needs_details" and not self.candidates:
            raise ValueError("needs_details requires at least one candidate")
        return self


class CadSelectionReview(BaseModel):
    """LLM decision after examining only the requested candidate detail images."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    selection: CadSelectionPlan | None = None
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def review_has_consistent_payload(self):
        if self.status == "selected":
            if self.selection is None or self.selection.status != "selected":
                raise ValueError("selected review requires a selected final plan")
        elif self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details review requires detail_requests")
        elif self.selection is not None:
            raise ValueError("only selected review may include selection")
        return self


CLARIFICATION_STEPS = frozenset({
    "understand_prompt", "select_fluid_body", "plan_boundary_groups",
})


def clarification_step(value: str) -> str:
    if value not in CLARIFICATION_STEPS:
        raise ValueError("Unsupported clarification resume step: " + str(value))
    return value


class BoundaryFaceGroup(BaseModel):
    """A real face set on the selected fluid body, ready for SpaceClaim grouping."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: Literal["inlet", "outlet", "wall", "symmetry"]
    face_ids: list[str]
    reason: str

class BoundaryGroupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "ambiguous", "not_found"]
    target_catalog_id: str
    groups: list[BoundaryFaceGroup] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

class BoundaryGroupReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    selection: BoundaryGroupPlan | None = None
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def review_has_consistent_payload(self):
        if self.status == "selected":
            if self.selection is None or self.selection.status != "selected":
                raise ValueError("selected review requires a selected boundary-group plan")
        elif self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details review requires detail_requests")
        elif self.selection is not None:
            raise ValueError("only selected review may include selection")
        return self


class FluidBodySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["selected", "needs_details", "ambiguous", "not_found"]
    body_id: str | None = None
    detail_requests: list[CandidateDetailRequest] = Field(default_factory=list)
    explanation: str
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def selected_has_body(self):
        if self.status == "selected" and not self.body_id:
            raise ValueError("selected requires body_id")
        if self.status == "needs_details" and not self.detail_requests:
            raise ValueError("needs_details requires detail_requests")
        if self.status != "selected" and self.body_id is not None:
            raise ValueError("only selected may include body_id")
        return self


class NumericControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(gt=0, allow_inf_nan=False)
    unit: Literal["m"]
    original_expression: str = ""
    source: Literal["user", "inferred"]
    basis: str


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
    growth_rate: float | None = None
    growth_rate_source: Literal["user", "inferred"] | None = None
    first_layer_height: NumericControl | None = None

    @model_validator(mode="after")
    def numeric_sources_are_recorded(self):
        if self.layers == 0 and self.layers_source != "user":
            raise ValueError("Disabling boundary layers requires an explicit user request")
        if (self.layers is None) != (self.layers_source is None):
            raise ValueError("layers and layers_source must be supplied together")
        if (self.growth_rate is None) != (self.growth_rate_source is None):
            raise ValueError("growth_rate and growth_rate_source must be supplied together")
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
    missing_information: list[str] = Field(default_factory=list)
    unsupported_requirements: list[str] = Field(default_factory=list)


class EmptyRepairParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


@dataclass(frozen=True)
class RepairActionSpec:
    parameters: type[BaseModel]
    route: Literal["retry", "cad", "fluent", "human", "stop"]
    resume_step: str | None = None
    approval: Literal["none", "user_parameter", "boundary_mapping"] = "none"
    user_parameter: Literal[
        "global_size", "local_size", "growth_rate", "layer_count", "first_layer_height"
    ] | None = None
    worker_handler: str | None = None
    resume_parameter: str | None = None
    resume_values: tuple[tuple[str, str], ...] = ()

    def resume_for(self, parameters: dict[str, Any]) -> str | None:
        if self.resume_step is not None:
            return self.resume_step
        if self.resume_parameter is None:
            return None
        value = str(parameters[self.resume_parameter])
        try:
            return dict(self.resume_values)[value]
        except KeyError as error:
            raise ValueError(
                f"Unsupported {self.resume_parameter} for repair routing: {value}"
            ) from error


REPAIR_ACTIONS: dict[str, RepairActionSpec] = {
    "retry_step": RepairActionSpec(
        EmptyRepairParameters, route="retry", worker_handler="_apply_retry"
    ),
    "reselect_cad": RepairActionSpec(EmptyRepairParameters, route="cad"),
    "set_global_size": RepairActionSpec(
        ValueParameters,
        route="fluent",
        resume_step="surface_mesh",
        approval="user_parameter",
        user_parameter="global_size",
        worker_handler="_apply_global_size",
    ),
    "set_local_size": RepairActionSpec(
        LocalSizeParameters,
        route="fluent",
        resume_step="local_sizing",
        approval="user_parameter",
        user_parameter="local_size",
        worker_handler="_apply_local_size",
    ),
    "set_growth_rate": RepairActionSpec(
        ValueParameters,
        route="fluent",
        resume_step="boundary_layers",
        approval="user_parameter",
        user_parameter="growth_rate",
        worker_handler="_apply_growth_rate",
    ),
    "set_layer_count": RepairActionSpec(
        LayerCountParameters,
        route="fluent",
        resume_step="boundary_layers",
        approval="user_parameter",
        user_parameter="layer_count",
        worker_handler="_apply_layer_count",
    ),
    "set_first_layer_height": RepairActionSpec(
        ValueParameters,
        route="fluent",
        resume_step="boundary_layers",
        approval="user_parameter",
        user_parameter="first_layer_height",
        worker_handler="_apply_first_layer_height",
    ),
    "set_layer_targets": RepairActionSpec(
        LayerTargetsParameters,
        route="fluent",
        resume_step="boundary_layers",
        approval="boundary_mapping",
        worker_handler="_apply_layer_targets",
    ),
    "enable_quality_improvement": RepairActionSpec(
        EmptyRepairParameters,
        route="fluent",
        resume_step="surface_mesh",
        worker_handler="_apply_quality_improvement",
    ),
    "replace_zone_reference": RepairActionSpec(
        ZoneReferenceParameters,
        route="fluent",
        approval="boundary_mapping",
        worker_handler="_apply_zone_reference",
        resume_parameter="category",
        resume_values=(
            ("boundaries.inlet", "update_boundaries"),
            ("boundaries.outlet", "update_boundaries"),
            ("boundaries.wall", "update_boundaries"),
            ("boundaries.symmetry", "update_boundaries"),
            ("boundary_layers", "boundary_layers"),
            ("local_refinements", "local_sizing"),
        ),
    ),
    "return_to_human": RepairActionSpec(EmptyRepairParameters, route="human"),
    "stop": RepairActionSpec(EmptyRepairParameters, route="stop"),
}


def repair_action_spec(action: str) -> RepairActionSpec:
    try:
        return REPAIR_ACTIONS[action]
    except KeyError as error:
        raise ValueError("Unsupported repair action: " + action) from error


def repair_tool_catalog() -> dict[str, Any]:
    return {
        name: spec.parameters.model_json_schema() for name, spec in REPAIR_ACTIONS.items()
    }


RepairAction = Literal[*tuple(REPAIR_ACTIONS)]


class RepairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis: str
    evidence: str
    action: RepairAction
    target_step: Literal[
        "prepare",
        "query_geometry",
        "understand_prompt",
        "verify_selection",
        "extract_volume",
        "select_fluid_body",
        "plan_boundary_groups",
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
        repair_action_spec(self.action).parameters.model_validate(self.parameters)
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
