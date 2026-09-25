"""Read confirmed SpaceClaim named groups and build Fluent boundary input."""

from __future__ import annotations

import copy
from typing import Any

from src.services.errors import PipelineError
from src.services.geometry_catalog import GeometryCatalog
from src.services.units import control_in_metres

ALLOWED_ROLES = {"inlet", "outlet", "wall", "symmetry"}


def named_groups(catalog: GeometryCatalog) -> dict[str, list[str]]:
    native = getattr(catalog, "native_catalog", None)
    if not isinstance(native, dict):
        raise ValueError("Confirmed catalog has no native group data")
    raw_groups = native.get("internal", {}).get("raw_groups", [])
    result: dict[str, list[str]] = {}
    for row in raw_groups:
        name = str(row.get("raw_name", "")).strip()
        members = [str(item) for item in row.get("member_ids", [])]
        if not name:
            continue
        if name in result:
            raise ValueError("Duplicate SpaceClaim group name: " + name)
        result[name] = members
    return result


def confirm_roles(
    *, catalog: GeometryCatalog, proposed: dict[str, str], previous: dict[str, str]
) -> dict[str, str]:
    groups = named_groups(catalog)
    extra = sorted(set(proposed) - set(groups))
    roles = {name: proposed.get(name, previous.get(name, "")) for name in groups}
    missing = sorted(name for name, role in roles.items() if not role)
    invalid = sorted(name for name, role in roles.items() if role and role not in ALLOWED_ROLES)
    if missing:
        raise ValueError("Boundary roles are missing for confirmed groups: " + ", ".join(missing))
    if extra:
        raise ValueError("Boundary roles refer to absent confirmed groups: " + ", ".join(extra))
    if invalid:
        raise ValueError("Unsupported boundary roles for: " + ", ".join(invalid))
    if not any(role == "inlet" for role in roles.values()):
        raise ValueError("Confirmed groups contain no inlet")
    if not any(role == "outlet" for role in roles.values()):
        raise ValueError("Confirmed groups contain no outlet")
    return roles


def validate_confirmed_cad(
    *, catalog: GeometryCatalog, roles: dict[str, str]
) -> dict[str, Any]:
    """Validate the actual saved CAD before Fluent receives it.

    SpaceClaim groups are editable during human confirmation.  Verify the
    topological and grouping invariants again from a fresh catalog rather than
    trusting the groups produced before the handoff pause.
    """

    bodies = list(catalog.bodies)
    positive = [
        body
        for body in bodies
        if body.solid_or_sheet == "solid" and (body.volume_m3 or 0.0) > 0.0
    ]
    if len(bodies) != 1 or len(positive) != 1:
        raise PipelineError(
            "CAD_CONFIRMED_SOLID_INVALID",
            "The confirmed CAD must contain exactly one positive-volume solid.",
            stage="reload_confirmed_cad",
            substep="solid validation",
            objects=[{"candidate_id": body.id} for body in bodies],
            suggested_action="Remove extra bodies or repair zero-volume bodies, then save the CAD again.",
            evidence={
                "body_count": len(bodies),
                "positive_solid_ids": [body.id for body in positive],
            },
        )
    body = positive[0]
    groups = named_groups(catalog)
    if set(groups) != set(roles):
        raise PipelineError(
            "CAD_CONFIRMED_GROUP_ROLE_MISMATCH",
            "The confirmed boundary groups do not match the confirmed role names.",
            stage="reload_confirmed_cad",
            substep="boundary-group validation",
            suggested_action="Assign roles again for every current boundary group.",
            evidence={"group_names": sorted(groups), "role_names": sorted(roles)},
        )
    face_ids = {face.id for face in catalog.faces if face.body_id == body.id}
    assigned: dict[str, str] = {}
    for name, members in groups.items():
        if not members:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_EMPTY",
                "A confirmed boundary group is empty.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[{"name": name, "role": roles.get(name, "")}],
                suggested_action="Add fluid-body faces to that group, or remove the empty group and confirm roles again.",
            )
        non_faces = [member for member in members if member not in face_ids]
        if non_faces:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_MEMBER_INVALID",
                "A confirmed boundary group contains an object outside the fluid body.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[{"name": name, "candidate_id": member} for member in non_faces],
                suggested_action="Boundary groups may contain only faces from the fluid body.",
            )
        overlap = [member for member in members if member in assigned]
        if overlap:
            raise PipelineError(
                "CAD_CONFIRMED_GROUP_OVERLAP",
                "Confirmed boundary groups contain overlapping faces.",
                stage="reload_confirmed_cad",
                substep="boundary-group validation",
                objects=[
                    {"name": assigned[member], "candidate_id": member}
                    for member in overlap
                ] + [{"name": name, "candidate_id": member} for member in overlap],
                suggested_action="Assign each fluid face to exactly one boundary group.",
            )
        assigned.update({member: name for member in members})
    missing_faces = sorted(face_ids - set(assigned))
    if missing_faces:
        raise PipelineError(
            "CAD_CONFIRMED_GROUP_COVERAGE_INCOMPLETE",
            "Confirmed boundary groups do not cover every fluid face.",
            stage="reload_confirmed_cad",
            substep="boundary-group validation",
            objects=[{"candidate_id": face_id} for face_id in missing_faces],
            suggested_action="Assign every ungrouped fluid face to an inlet, outlet, wall, or symmetry group.",
        )
    if not any(role == "inlet" for role in roles.values()) or not any(
        role == "outlet" for role in roles.values()
    ):
        raise PipelineError(
            "CAD_CONFIRMED_TERMINAL_ROLE_MISSING",
            "Confirmed boundary groups must include both inlet and outlet roles.",
            stage="reload_confirmed_cad",
            substep="boundary-role validation",
            suggested_action="Set one group to inlet and another group to outlet.",
        )
    return {
        "positive_volume": True,
        "nonempty_groups": True,
        "nonoverlapping_groups": True,
        "all_faces_grouped": True,
        "roles_complete": True,
        "body_id": body.id,
        "face_count": len(face_ids),
        "group_count": len(groups),
    }


def build_fluent_job(
    *, geometry: str, roles: dict[str, str], requirements: dict[str, Any]
) -> dict[str, Any]:
    boundaries = {role: [] for role in ALLOWED_ROLES}
    for name, role in roles.items():
        boundaries[role].append(name)
    global_control = requirements.get("global_size")
    layers = requirements.get("boundary_layers") or {}
    layer_zones = resolve_layer_zones(layers, roles)
    return {
        "job_name": "cfd-agent-run",
        "geometry_path": geometry,
        "length_unit": requirements.get("length_unit"),
        "control_unit": "m",
        "boundaries": boundaries,
        "global_size": control_in_metres(global_control),
        "local_refinements": [
            {
                "target": item["target"],
                "size": control_in_metres(item["size"]),
                "zone": item.get("boundary_name"),
            }
            for item in requirements.get("local_refinements", [])
        ],
        "boundary_layers": {
            "zones": layer_zones,
            "scope_specified": bool(layers) and layers.get("layers") != 0,
            "layers": layers.get("layers"),
            "growth_rate": layers.get("growth_rate"),
            "first_layer_height": control_in_metres(layers.get("first_layer_height")),
        },
        "parameter_sources": copy.deepcopy(requirements),
        "volume_fill": "poly-hexcore",
        "quality": {
            "min_orthogonal_quality": 0.1,
            "max_skewness": 0.95,
        },
    }


def resolve_layer_zones(layers: dict, roles: dict[str, str]) -> list[str]:
    if not layers or layers.get("layers") == 0:
        return []
    names = layers.get("boundary_names")
    if names is None:
        target = layers.get("target", "all walls")
        names = (
            [name for name, role in roles.items() if role == "wall"]
            if target == "all walls"
            else [target]
        )
    return list(dict.fromkeys(names))


def rebind_mesh_targets(
    *,
    requirements: dict,
    previous_groups: list[dict],
    confirmed_catalog: GeometryCatalog,
    roles: dict[str, str],
) -> dict:
    """Preserve identity across group renames, never infer a different face set."""
    result = copy.deepcopy(requirements)
    groups = named_groups(confirmed_catalog)
    objects = confirmed_catalog.by_id()
    current = {
        name: {objects[item].moniker for item in members if item in objects}
        for name, members in groups.items()
    }
    previous = {row["name"]: set(row["member_monikers"]) for row in previous_groups}

    def resolve(name: str | None, target: str, *, required: bool = True) -> str:
        requested = name or target
        if requested in roles:
            return requested
        members = previous.get(requested)
        matches = [
            name
            for name, values in current.items()
            if members and None not in members and values == members
        ]
        if len(matches) != 1:
            if not required:
                return requested
            raise ValueError(
                "Cannot uniquely bind meshing target after CAD confirmation: " + requested
            )
        return matches[0]

    for item in result.get("local_refinements", []):
        item["boundary_name"] = resolve(item.get("boundary_name"), item["target"])
    layers = result.get("boundary_layers")
    if layers and layers.get("layers") != 0:
        names = layers.get("boundary_names")
        if names is not None:
            layers["boundary_names"] = [resolve(name, name, required=False) for name in names]
        elif layers.get("target", "all walls") != "all walls":
            layers["boundary_names"] = [resolve(None, layers["target"], required=False)]
        resolve_layer_zones(layers, roles)
    return result
