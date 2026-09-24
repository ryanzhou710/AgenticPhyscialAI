"""Resolve model-selected terminal faces to SpaceClaim cap-edge loops."""

from __future__ import annotations

from typing import Any


def resolve_terminal_boundary(catalog: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    """Return the catalog edge IDs that bound one supported opening selection.

    A planar end face contributes its outer loop.  An annular wall face contributes
    its single circular inner loop.  A directly selected edge remains supported for
    the original single-circular-open-edge representation.
    """

    public = catalog.get("public", catalog)
    edges = {row["id"]: row for row in public.get("edges", [])}

    if candidate_id.startswith("E"):
        edge = edges.get(candidate_id)
        if edge is None:
            raise ValueError(f"Unknown opening edge: {candidate_id}")
        if len(edge.get("face_ids", [])) != 1:
            raise ValueError("Selected opening edge is not a single-face boundary")
        if edge.get("curve_type") != "Circle" and edge.get("closed") is not True:
            raise ValueError("Selected opening edge is not a closed boundary")
        kind = "circular_edge" if edge.get("curve_type") == "Circle" else "closed_edge"
        return {"kind": kind, "edge_ids": [candidate_id]}

    if candidate_id.startswith("L"):
        loops = {row["id"]: row for row in public.get("loops", [])}
        boundary = loops.get(candidate_id)
        if boundary is None:
            raise ValueError(f"Unknown opening loop: {candidate_id}")
        if boundary.get("closed") is not True:
            raise ValueError("Selected opening loop is not closed")
        edge_ids = boundary.get("edge_ids", [])
        if not edge_ids:
            raise ValueError("Selected opening loop has no edges")
        missing = [edge_id for edge_id in edge_ids if edge_id not in edges]
        if missing:
            raise ValueError(f"Opening loop contains unknown edges: {missing}")
        return {"kind": "loop", "edge_ids": list(edge_ids)}

    faces = {row["id"]: row for row in public.get("faces", [])}
    face = faces.get(candidate_id)
    if face is None:
        raise ValueError(f"Unknown opening face: {candidate_id}")
    if face.get("surface_type") != "Plane":
        raise ValueError("Selected opening face is not planar")

    loops = [
        row for row in public.get("loops", []) if row.get("face_id") == candidate_id
    ]
    inner_loops = [row for row in loops if not row.get("is_outer")]
    if inner_loops:
        if len(inner_loops) != 1:
            raise ValueError("Selected opening face does not contain one inner loop")
        boundary = inner_loops[0]
        edge_ids = boundary.get("edge_ids", [])
        if not edge_ids:
            raise ValueError("Selected opening inner loop has no edges")
        kind = "circular_inner_loop" if (
            len(edge_ids) == 1 and edges.get(edge_ids[0], {}).get("curve_type") == "Circle"
        ) else "inner_loop"
    else:
        outer_loops = [row for row in loops if row.get("is_outer")]
        if len(outer_loops) != 1 or not outer_loops[0].get("edge_ids"):
            raise ValueError("Selected terminal face does not contain one closed outer loop")
        boundary = outer_loops[0]
        edge_ids = boundary["edge_ids"]
        kind = "terminal_face"

    missing = [edge_id for edge_id in edge_ids if edge_id not in edges]
    if missing:
        raise ValueError(f"Opening loop contains unknown edges: {missing}")
    return {"kind": kind, "edge_ids": list(edge_ids)}
