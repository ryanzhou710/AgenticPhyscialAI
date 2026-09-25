"""Translate explicit model-selected extraction objects into native arguments."""

from __future__ import annotations

from typing import Any

from src.services.errors import PipelineError


def _failure(code: str, reason: str, candidate_ids: list[str]) -> PipelineError:
    return PipelineError(
        code,
        reason,
        stage="extract_volume",
        substep="selection validation",
        objects=[{"candidate_id": value} for value in candidate_ids],
        suggested_action="Choose real CAD objects that match the requested extraction method.",
    )


def resolve_extraction_selection(catalog: dict[str, Any], selection_plan: dict[str, Any]) -> dict[str, Any]:
    """Validate only native argument references, not geometric feasibility.

    SpaceClaim decides whether a selected face or edge contour can produce a
    volume. This layer only ensures that every requested object exists and has
    the object type required by the explicit method selected by the model.
    """

    public = catalog.get("public", catalog)
    objects = {
        str(row["id"]): {**row, "kind": kind}
        for kind, collection in (
            ("body", "bodies"),
            ("face", "faces"),
            ("edge", "edges"),
            ("loop", "loops"),
        )
        for row in public.get(collection, [])
    }
    strategy = selection_plan.get("extraction_strategy")
    if strategy not in {"faces", "edges"}:
        raise _failure(
            "CAD_EXTRACTION_STRATEGY_INVALID",
            "Extraction requires an explicit faces or edges method.",
            [],
        )
    seed_id = str(selection_plan.get("seed_inner_wall_id") or "")
    seed = objects.get(seed_id)
    if seed is None or seed["kind"] != "face":
        raise _failure("CAD_SEED_INVALID", "The extraction seed must be an existing face.", [seed_id])

    records: list[dict[str, Any]] = []
    for opening in selection_plan.get("openings") or []:
        object_ids = [str(value) for value in opening.get("object_ids") or []]
        if not object_ids:
            raise _failure(
                "CAD_OPENING_EMPTY",
                "Each extraction opening must explicitly name one or more objects.",
                [],
            )
        selected = [objects.get(value) for value in object_ids]
        missing = [value for value, row in zip(object_ids, selected) if row is None]
        if missing:
            raise _failure(
                "CAD_CANDIDATE_UNKNOWN",
                "An extraction object is absent from the current CAD catalog.",
                missing,
            )
        if strategy == "faces":
            if opening.get("selection_kind") != "face" or any(
                row["kind"] != "face" for row in selected
            ):
                raise _failure(
                    "CAD_EXTRACTION_OBJECT_TYPE",
                    "Face extraction accepts only explicitly selected faces.",
                    object_ids,
                )
            native_ids = object_ids
        else:
            native_ids: list[str] = []
            for row in selected:
                if row["kind"] == "edge" and opening.get("selection_kind") == "edges":
                    native_ids.append(str(row["id"]))
                elif row["kind"] == "loop" and opening.get("selection_kind") == "loop":
                    native_ids.extend(str(value) for value in row.get("edge_ids") or [])
                else:
                    raise _failure(
                        "CAD_EXTRACTION_OBJECT_TYPE",
                        "Edge extraction accepts explicitly selected loops or edges.",
                        object_ids,
                    )
            if not native_ids:
                raise _failure(
                    "CAD_OPENING_EMPTY",
                    "The selected edge method has no native edge objects.",
                    object_ids,
                )
        records.append(
            {
                "name": str(opening["name"]),
                "role": str(opening["role"]),
                "description": str(opening.get("description", "")),
                "reason": str(opening.get("reason", "")),
                "selection_kind": str(opening.get("selection_kind", "")),
                "object_ids": object_ids,
                "native_ids": native_ids,
            }
        )
    if not records:
        raise _failure(
            "CAD_OPENING_EMPTY",
            "Extraction requires at least one explicitly selected opening.",
            [],
        )
    return {
        "seed_face_id": seed_id,
        "extraction_strategy": strategy,
        "terminal_records": records,
    }
