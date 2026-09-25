"""Serializable geometry contracts shared by the model and SpaceClaim adapter."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BBox(BaseModel):
    min_m: list[float] = Field(min_length=3, max_length=3)
    max_m: list[float] = Field(min_length=3, max_length=3)
    center_m: list[float] = Field(min_length=3, max_length=3)


class CatalogObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["body", "face", "edge", "loop"]
    moniker: str | None = Field(default=None, json_schema_extra={"model_visible": False})
    body_id: str | None = None
    face_id: str | None = None
    surface_type: str | None = None
    curve_type: str | None = None
    solid_or_sheet: str | None = None
    volume_m3: float | None = None
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    radius_m: float | None = None
    bbox: BBox | None = None
    centroid_m: list[float] | None = None
    normal: list[float] | None = None
    axis: list[float] | None = None
    axis_origin_m: list[float] | None = None
    start_m: list[float] | None = None
    end_m: list[float] | None = None
    half_angle_rad: float | None = None
    face_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    loop_ids: list[str] = Field(default_factory=list)
    adjacent_face_ids: list[str] = Field(default_factory=list)
    is_outer: bool | None = None
    closed: bool | None = None

    def model_facing_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, definition in type(self).model_fields.items():
            if (definition.json_schema_extra or {}).get("model_visible") is False:
                continue
            value = getattr(self, name)
            if value is None or value == []:
                continue
            result[name] = value.model_dump() if isinstance(value, BaseModel) else value
        return result


class GeometryCatalog(BaseModel):
    model_config = ConfigDict(extra="allow")

    catalog_id: str
    geometry_id: str
    coordinate_unit: Literal["m"] = "m"
    bodies: list[CatalogObject] = Field(default_factory=list)
    faces: list[CatalogObject] = Field(default_factory=list)
    edges: list[CatalogObject] = Field(default_factory=list)
    loops: list[CatalogObject] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    candidate_render_results: list[dict[str, Any]] = Field(default_factory=list)
    candidate_render_summary: dict[str, int] = Field(default_factory=dict)

    def all_objects(self) -> list[CatalogObject]:
        return [*self.bodies, *self.faces, *self.edges, *self.loops]

    def by_id(self) -> dict[str, CatalogObject]:
        return {item.id: item for item in self.all_objects()}

    def public_dict(self) -> dict[str, Any]:
        return {
            "coordinate_unit": self.coordinate_unit,
            "bodies": [item.model_facing_dict() for item in self.bodies],
            "faces": [item.model_facing_dict() for item in self.faces],
            "edges": [item.model_facing_dict() for item in self.edges],
            "loops": [item.model_facing_dict() for item in self.loops],
        }


class SelectionExecution(BaseModel):
    ok: bool
    selected_ids: list[str] = Field(default_factory=list)
    selected_monikers: list[str] = Field(default_factory=list)
    active_selection_verified: bool = False
    images: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
