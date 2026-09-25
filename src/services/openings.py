"""Resolve model-selected terminal faces to SpaceClaim cap-edge loops."""

from __future__ import annotations

from typing import Any

from src.services.errors import PipelineError


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


def _terminal_error(
    code: str,
    reason: str,
    *,
    candidate_id: str,
    evidence: dict[str, Any] | None = None,
) -> PipelineError:
    return PipelineError(
        code,
        reason,
        stage="extract_volume",
        substep="opening validation",
        objects=[{"candidate_id": candidate_id}],
        suggested_action="Check the opening contour and inner-wall seed, and specify the exact contour if needed.",
        evidence=evidence,
    )


def resolve_terminal_record(catalog: dict[str, Any], port: dict[str, Any]) -> dict[str, Any]:
    """Resolve one selected opening once, before handing it to SpaceClaim build code."""

    public = catalog.get("public", catalog)
    candidate_id = str(port.get("candidate_id", ""))
    faces = {str(row.get("id")): row for row in public.get("faces", [])}
    edges = {str(row.get("id")): row for row in public.get("edges", [])}
    loops = {str(row.get("id")): row for row in public.get("loops", [])}

    def support_face(face_id: str | None) -> dict[str, Any]:
        face = faces.get(str(face_id))
        if face is None:
            raise _terminal_error(
                "CAD_OPENING_SUPPORT_FACE_MISSING",
                "The opening contour has no usable support face.",
                candidate_id=candidate_id,
            )
        if face.get("surface_type") != "Plane":
            raise _terminal_error(
                "CAD_OPENING_SUPPORT_NOT_PLANAR",
                "The opening support face is not planar and cannot be safely capped by this workflow.",
                candidate_id=candidate_id,
            )
        return face

    def edge_list(loop: dict[str, Any] | None, values: list[str]) -> list[str]:
        if loop is not None and loop.get("closed") is not True:
            raise _terminal_error(
                "CAD_OPENING_NOT_CLOSED",
                "The selected opening contour is not closed.",
                candidate_id=candidate_id,
            )
        if not values:
            raise _terminal_error(
                "CAD_OPENING_EMPTY",
                "The selected opening contour contains no edges.",
                candidate_id=candidate_id,
            )
        missing = [edge_id for edge_id in values if edge_id not in edges]
        if missing:
            raise _terminal_error(
                "CAD_OPENING_EDGE_MISSING",
                "The opening contour references an edge absent from the current CAD.",
                candidate_id=candidate_id,
                evidence={"missing_edge_ids": missing},
            )
        return list(values)

    source_kind = candidate_id[:1]
    contour_loop: dict[str, Any] | None = None
    is_outer = False
    face_cap_supported = False
    if source_kind == "F":
        face = support_face(candidate_id)
        face_loops = [row for row in loops.values() if row.get("face_id") == candidate_id]
        inner = [row for row in face_loops if not row.get("is_outer")]
        outer = [row for row in face_loops if row.get("is_outer")]
        if len(inner) > 1:
            raise _terminal_error(
                "CAD_OPENING_AMBIGUOUS",
                "This face has multiple inner loops, so the opening to cap cannot be identified uniquely.",
                candidate_id=candidate_id,
                evidence={"candidate_loop_ids": [row.get("id") for row in inner]},
            )
        if len(inner) == 1:
            contour_loop = inner[0]
        elif len(outer) == 1:
            contour_loop = outer[0]
        else:
            raise _terminal_error(
                "CAD_OPENING_AMBIGUOUS",
                "This face does not have one unique closed opening contour.",
                candidate_id=candidate_id,
                evidence={"candidate_loop_ids": [row.get("id") for row in face_loops]},
            )
        is_outer = bool(contour_loop.get("is_outer"))
        face_cap_supported = is_outer and len(face_loops) == 1
        support = face
        edge_ids = edge_list(contour_loop, list(contour_loop.get("edge_ids") or []))
    elif source_kind == "L":
        contour_loop = loops.get(candidate_id)
        if contour_loop is None:
            raise _terminal_error(
                "CAD_OPENING_UNKNOWN",
                "The selected opening loop is absent from the current CAD.",
                candidate_id=candidate_id,
            )
        support = support_face(contour_loop.get("face_id"))
        edge_ids = edge_list(contour_loop, list(contour_loop.get("edge_ids") or []))
        is_outer = bool(contour_loop.get("is_outer"))
        face_loops = [row for row in loops.values() if row.get("face_id") == support.get("id")]
        face_cap_supported = is_outer and len(face_loops) == 1
    elif source_kind == "E":
        edge = edges.get(candidate_id)
        if edge is None:
            raise _terminal_error(
                "CAD_OPENING_UNKNOWN",
                "The selected opening edge is absent from the current CAD.",
                candidate_id=candidate_id,
            )
        closed = edge.get("closed") is True or edge.get("curve_type") == "Circle"
        face_ids = list(edge.get("face_ids") or [])
        if not closed or len(face_ids) != 1:
            raise _terminal_error(
                "CAD_OPENING_NOT_CLOSED",
                "The opening edge must be a closed edge attached to exactly one face.",
                candidate_id=candidate_id,
            )
        support = support_face(face_ids[0])
        edge_ids = [candidate_id]
    else:
        raise _terminal_error(
            "CAD_OPENING_UNSUPPORTED_OBJECT",
            "An opening must be represented by a face, a closed loop, or a closed edge.",
            candidate_id=candidate_id,
        )
    return {
        "name": str(port["name"]),
        "role": str(port["role"]),
        "source_candidate_id": candidate_id,
        "support_face_id": str(support["id"]),
        "contour_loop_id": None if contour_loop is None else str(contour_loop["id"]),
        "edge_ids": edge_ids,
        "is_outer": is_outer,
        "face_cap_supported": face_cap_supported,
    }


def resolve_terminal_records(catalog: dict[str, Any], openings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return non-overlapping, executable opening records for one extraction."""

    records = [resolve_terminal_record(catalog, port) for port in openings]
    seen_edges: dict[str, str] = {}
    for record in records:
        for edge_id in record["edge_ids"]:
            previous = seen_edges.get(edge_id)
            if previous is not None:
                raise PipelineError(
                    "CAD_OPENING_OVERLAP",
                    "Two opening selections reference the same edge and cannot be extracted independently.",
                    stage="extract_volume",
                    substep="opening validation",
                    objects=[
                        {"name": previous, "candidate_id": edge_id},
                        {"name": record["name"], "candidate_id": edge_id},
                    ],
                    suggested_action="Choose non-overlapping opening contours for each inlet and outlet.",
                )
            seen_edges[edge_id] = record["name"]
    return records


def resolve_extraction_selection(catalog: dict[str, Any], selection_plan: dict[str, Any]) -> dict[str, Any]:
    """Validate opening records and the seed before starting a mutable CAD operation."""

    records = resolve_terminal_records(catalog, list(selection_plan.get("openings") or []))
    seed_id = str(selection_plan.get("seed_inner_wall_id") or "")
    faces = {str(row.get("id")): row for row in catalog.get("public", catalog).get("faces", [])}
    if seed_id not in faces:
        raise PipelineError(
            "CAD_SEED_INVALID",
            "The fluid-domain seed face is absent or is not a face object.",
            stage="extract_volume",
            substep="seed-face validation",
            objects=[{"candidate_id": seed_id}],
            suggested_action="Choose a face on the internal flow-path wall as the seed.",
        )
    if not records:
        raise PipelineError(
            "CAD_OPENING_EMPTY",
            "No usable opening is available for fluid-domain extraction.",
            stage="extract_volume",
            substep="opening validation",
            suggested_action="Specify at least one inlet opening and one outlet opening in the request.",
        )
    return {
        "terminal_records": records,
        "seed_face_id": seed_id,
        "face_strategy_available": all(row["face_cap_supported"] for row in records),
    }
