"""Read confirmed SpaceClaim named groups and build Fluent boundary input."""

from __future__ import annotations

import copy
from typing import Any

from src.services.geometry_models import GeometryCatalog
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
    confirmed: GeometryCatalog,
    roles: dict[str, str],
) -> dict:
    """Preserve identity across group renames, never infer a different face set."""
    result = copy.deepcopy(requirements)
    groups = named_groups(confirmed)
    objects = confirmed.by_id()
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
