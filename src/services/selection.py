"""LLM planning over a neutral SpaceClaim geometry catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from src.adapters.llm import GroundingLLMClient
from src.config import RuntimeConfig
from src.services.contracts import (
    CadSelectionPlan,
    CadSelectionReview,
    CadSelectionScreening,
    CandidateDetailRequest,
    MeshRequirements,
)
from src.services.errors import PipelineError
from src.services.geometry_catalog import GeometryCatalog


def _model_images(catalog: GeometryCatalog) -> list[Path]:
    references: dict[str, Path] = {}
    for row in catalog.images:
        path = Path(str(row.get("path", "")))
        if not path.is_file():
            continue
        view = row.get("view")
        if view in {"Front", "Top", "Right", "Isometric"} and not row.get("candidate_id"):
            references[str(view)] = path
    return [
        references[name] for name in ("Front", "Top", "Right", "Isometric") if name in references
    ]


def _native_open_edges(catalog: GeometryCatalog) -> list[dict[str, Any]]:
    native = getattr(catalog, "native_catalog", {})
    public = native.get("public", {}) if isinstance(native, dict) else {}
    return [
        row
        for row in public.get("edges", [])
        if len(row.get("face_ids", [])) == 1
        and row.get("closed") is True
    ]


def _opening_candidate_context(catalog: GeometryCatalog) -> dict[str, Any]:
    """Describe the topology representations accepted by volume extraction."""
    return {
        # Keep every catalog face visible to the model.  Planarity limits the
        # current extraction operation, but it must not erase geometry from the
        # semantic decision or accidentally hide a seed candidate.
        "face_candidates": [row.id for row in catalog.faces],
        "planar_faces": [
            row.id for row in catalog.faces
            if row.surface_type == "Plane"
        ],
        "closed_loops": [
            row.id for row in catalog.loops
            if row.closed is True
        ],
        "seed_face_candidates": [row.id for row in catalog.faces],
        "single_face_edges": _native_open_edges(catalog),
        "selection_guidance": (
            "Prefer a planar face or a closed loop for an arbitrary opening. "
            "A face may use its one inner loop (a cutout) or its one outer loop "
            "(a flush end). Use an edge only when it is itself a closed boundary."
        ),
    }


_DEFAULT_DETAIL_VIEWS = {
    "opening": ("Selected",),
    "seed": ("OwnerContext", "SelectedProxy"),
}


def _candidate_details(requests: Iterable[CandidateDetailRequest], limit: int = 12) -> list[dict[str, Any]]:
    """Apply stable default views and combine requests for the same candidate."""

    combined: dict[str, dict[str, Any]] = {}
    for request in requests:
        item = combined.setdefault(
            request.candidate_id,
            {
                "candidate_id": request.candidate_id,
                "purpose": request.purpose,
                "detail_views": [],
                "reason": request.reason,
            },
        )
        if item["purpose"] != request.purpose:
            raise PipelineError(
                "CAD_CANDIDATE_PURPOSE_CONFLICT",
                "The same candidate was requested as both an opening and an inner-wall seed.",
                stage="understand_prompt",
                objects=[{"candidate_id": request.candidate_id}],
                suggested_action="Choose separate opening contours and an inner-wall seed face.",
            )
        for view in request.views or _DEFAULT_DETAIL_VIEWS[request.purpose]:
            if view not in item["detail_views"]:
                item["detail_views"].append(view)
    if len(combined) > limit:
        raise PipelineError(
            "CAD_DETAIL_REQUEST_LIMIT",
            f"This detail request contains {len(combined)} candidates, exceeding the {limit}-candidate limit.",
            stage="understand_prompt",
            suggested_action="Narrow the candidate set before requesting more detail images.",
        )
    return list(combined.values())


def _validate_detail_requests(catalog: GeometryCatalog, requests: list[dict[str, Any]]) -> None:
    objects = catalog.by_id()
    for request in requests:
        candidate_id = request["candidate_id"]
        candidate = objects.get(candidate_id)
        if candidate is None:
            raise PipelineError(
                "CAD_CANDIDATE_UNKNOWN",
                "The model requested a candidate absent from the current geometry catalog.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Reload the CAD and select an object present in the catalog.",
            )
        if request["purpose"] == "seed" and candidate.kind != "face":
            raise PipelineError(
                "CAD_SEED_NOT_FACE",
                "The inner-wall seed must be a face object.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a face on the internal flow-path wall as the seed.",
            )
        if request["purpose"] == "opening" and candidate.kind not in {"face", "loop", "edge"}:
            raise PipelineError(
                "CAD_OPENING_UNSUPPORTED_OBJECT",
                "An opening candidate must be a face, a closed loop, or a closed edge.",
                stage="understand_prompt",
                objects=[{"candidate_id": candidate_id}],
                suggested_action="Choose a face, loop, or closed edge that represents the opening boundary.",
            )


def _validate_final_selection(catalog: GeometryCatalog, answer: CadSelectionPlan) -> CadSelectionPlan:
    if answer.status != "selected" or answer.fluid_domain_action == "ambiguous":
        return answer
    universe = catalog.by_id()
    requested = [item.candidate_id for item in answer.openings]
    requested.append(str(answer.seed_inner_wall_id))
    unknown = [candidate_id for candidate_id in requested if candidate_id not in universe]
    if unknown:
        raise PipelineError(
            "CAD_CANDIDATE_UNKNOWN",
            "The model returned a final candidate absent from the current geometry catalog.",
            stage="understand_prompt",
            objects=[{"candidate_id": value} for value in unknown],
            suggested_action="Reload the CAD and confirm the candidate objects again.",
        )
    if universe[str(answer.seed_inner_wall_id)].kind != "face":
        raise PipelineError(
            "CAD_SEED_NOT_FACE",
            "The model selected a fluid-domain seed that is not a face.",
            stage="understand_prompt",
            objects=[{"candidate_id": str(answer.seed_inner_wall_id)}],
            suggested_action="Choose a face on the internal flow-path wall as the seed.",
        )
    if str(answer.seed_inner_wall_id) in {opening.candidate_id for opening in answer.openings}:
        raise PipelineError(
            "CAD_CANDIDATE_PURPOSE_CONFLICT",
            "The same object cannot be both an opening and the inner-wall seed.",
            stage="understand_prompt",
            objects=[{"candidate_id": str(answer.seed_inner_wall_id)}],
            suggested_action="Choose a separate inner-wall face as the extraction seed.",
        )
    unsupported = [
        opening.candidate_id
        for opening in answer.openings
        if universe[opening.candidate_id].kind not in {"face", "loop", "edge"}
    ]
    if unsupported:
        raise PipelineError(
            "CAD_OPENING_UNSUPPORTED_OBJECT",
            "Selected opening is not a face, loop, or edge.",
            stage="understand_prompt",
            objects=[{"candidate_id": candidate_id} for candidate_id in unsupported],
            suggested_action="Choose an opening face, closed loop, or closed edge from the catalog.",
        )
    return answer


def _clarification_plan(
    *,
    status: str,
    reference_view: str,
    explanation: str,
    missing_information: list[str],
    fluid_domain_action: str = "ambiguous",
    fluid_domain_evidence: str = "",
) -> CadSelectionPlan:
    return CadSelectionPlan(
        status=status,
        reference_view=reference_view,
        explanation=explanation,
        missing_information=missing_information,
        fluid_domain_action=fluid_domain_action,
        fluid_domain_evidence=fluid_domain_evidence,
    )


def plan_cad_selection(
    *,
    catalog: GeometryCatalog,
    user_prompt: str,
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
    detail_renderer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> CadSelectionPlan:
    settings = config or RuntimeConfig()
    client = GroundingLLMClient.from_runtime_config(
        config=settings,
        audit_dir=Path(audit_dir) / "selection",
    )
    images = _model_images(catalog)
    if images:
        probe = client.probe_vision()
        if probe.status.value != "verified":
            raise RuntimeError(settings.model + " image input is not available: " + probe.reason)
    context = catalog.public_dict()
    context["opening_candidates"] = _opening_candidate_context(catalog)
    prompt = (
        "USER REQUEST:\n"
        + user_prompt
        + "\n\nCANDIDATE CATALOG (metres, global SpaceClaim XYZ):\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n\nIMAGE ORDER: Front, Top, Right, Isometric. These are global reference views only."
    )
    # Preserve the original single-pass public helper for API callers that do
    # not provide a SpaceClaim detail renderer.
    if detail_renderer is None:
        answer = client.invoke(
            system_prompt=(
                "You translate a user's CAD intent into executable SpaceClaim object references.\n"
                "\n"
                "Use only the supplied neutral candidate catalog and images. Candidate IDs are temporary\n"
                "references for this run. Match descriptions using geometry type, size, position in the\n"
                "declared reference view, ownership, adjacency and visible shape. Never use file names,\n"
                "named groups or prior case knowledge as answers.\n"
                "\n"
                "Also return fluid_domain_action. Use \"reuse\" only when the user explicitly states that the\n"
                "supplied CAD is already the fluid domain. Use \"extract\" when the user does not make that\n"
                "declaration or asks for volume extraction. Use \"ambiguous\" when the request conflicts or\n"
                "cannot be resolved. fluid_domain_evidence must briefly quote or faithfully identify the user\n"
                "statement that supports this action. The host validates topology; never infer reuse from a\n"
                "closed solid alone.\n"
                "\n"
                "For an internal fluid-volume extraction, return every opening boundary requested by the\n"
                "user and one face on the enclosing inner wall as the seed. An opening should normally be\n"
                "represented by a planar face or a closed loop. A planar face may be a face with one inner\n"
                "loop (a cutout through a wall) or a flush end face whose one outer loop is the opening.\n"
                "Loops may contain any number of connected edges: lines, arcs, splines, polygons, or mixed\n"
                "curves. A single edge is valid only when that edge is itself closed. Do not require a\n"
                "circle, radius, or diameter. Preserve the user's boundary roles. Generate concise, stable\n"
                "boundary names only when the user did not provide names.\n"
                "\n"
                "When fluid_domain_action is \"reuse\", select the actual inlet/outlet faces of the already-fluid\n"
                "body (or a loop that maps to one face), rather than an edge. Otherwise, select the boundary\n"
                "contour to be capped for fluid-volume extraction. The host validates that a requested reuse\n"
                "has one closed positive-volume solid body.\n"
                "Do not invent a role. If the request cannot be mapped uniquely, return ambiguous or\n"
                "not_found instead of guessing.\n"
            ),
            user_prompt=prompt,
            images=images,
            response_model=CadSelectionPlan,
        )
        return _validate_final_selection(catalog, answer)

    screening = client.invoke(
        system_prompt=(
            "Read the user's CAD request, the neutral geometry catalog and the four global reference views.\n"
            "\n"
            "Do not make a final geometry selection in this pass. Return `needs_details` and request only\n"
            "the candidate opening contours and inner-wall seed faces whose detailed images are necessary.\n"
            "Each candidate must be a real catalog ID. Use purpose `opening` for face, loop or closed edge\n"
            "opening candidates; use purpose `seed` only for a face that may be the inner-wall seed.\n"
            "\n"
            "Choose the smallest useful set. The application will render only the configured number of distinct objects in\n"
            f"one round ({settings.selection_max_candidates_per_round} objects maximum). An opening normally receives Selected evidence. A seed normally receives OwnerContext\n"
            "and SelectedProxy evidence. Request another supported detail view only when it resolves a concrete\n"
            "ambiguity. Never use filenames, existing group names or prior-case knowledge.\n"
            "\n"
            "If the request cannot be narrowed from the supplied evidence, return ambiguous or not_found and\n"
            "state the missing information. Set fluid_domain_action to reuse only when the user explicitly says\n"
            "that the supplied CAD already is the fluid domain.\n"
        ),
        user_prompt=prompt,
        images=images,
        response_model=CadSelectionScreening,
    )
    if screening.status != "needs_details":
        return _clarification_plan(
            status=screening.status,
            reference_view=screening.reference_view,
            explanation=screening.explanation,
            missing_information=screening.missing_information,
            fluid_domain_action=screening.fluid_domain_action,
            fluid_domain_evidence=screening.fluid_domain_evidence,
        )

    cached: dict[tuple[str, str], dict[str, Any]] = {}
    history = [{"screening": screening.model_dump(mode="json")}]
    requests = screening.candidates
    for round_index in range(1, settings.selection_max_detail_rounds + 1):
        details = _candidate_details(requests, settings.selection_max_candidates_per_round)
        _validate_detail_requests(catalog, details)
        missing = [
            {
                **item,
                "detail_views": [
                    view
                    for view in item["detail_views"]
                    if (item["candidate_id"], view) not in cached
                ],
            }
            for item in details
            if any((item["candidate_id"], view) not in cached for view in item["detail_views"])
        ]
        if not missing:
            return _clarification_plan(
                status="ambiguous",
                reference_view=screening.reference_view,
                explanation="The model requested only candidate detail evidence that was already supplied.",
                missing_information=[
                    "Clarify the opening or seed-face feature that distinguishes the remaining candidates."
                ],
                fluid_domain_action=screening.fluid_domain_action,
                fluid_domain_evidence=screening.fluid_domain_evidence,
            )
        rendered = detail_renderer(missing)
        for row in rendered:
            candidate_id = row.get("candidate_id")
            view = row.get("view")
            path = Path(str(row.get("path", "")))
            if candidate_id and view and path.is_file():
                cached[(str(candidate_id), str(view))] = dict(row)
        supplied = [
            cached[(item["candidate_id"], view)]
            for item in details
            for view in item["detail_views"]
            if (item["candidate_id"], view) in cached
        ]
        absent = [
            {"candidate_id": item["candidate_id"], "view": view}
            for item in details
            for view in item["detail_views"]
            if (item["candidate_id"], view) not in cached
        ]
        if absent:
            raise PipelineError(
                "CAD_DETAIL_RENDER_FAILED",
                "SpaceClaim did not produce every requested candidate detail image.",
                stage="understand_prompt",
                substep="candidate-detail rendering",
                objects=absent,
                suggested_action="Check whether the CAD objects remain valid and inspect the SpaceClaim rendering record.",
                evidence={"missing_details": absent},
            )
        detail_prompt = (
            "USER REQUEST:\n"
            + user_prompt
            + "\n\nCANDIDATE CATALOG (metres, global SpaceClaim XYZ):\n"
            + json.dumps(context, ensure_ascii=False)
            + "\n\nPREVIOUS DECISIONS:\n"
            + json.dumps(history, ensure_ascii=False)
            + "\n\nAVAILABLE EVIDENCE:\n"
            + json.dumps([{"candidate_id": key[0], "view": key[1]} for key in cached])
            + f"\nMAXIMUM OBJECTS PER ROUND: {settings.selection_max_candidates_per_round}"
            + "\n\nCURRENT REQUESTS:\n"
            + json.dumps(details, ensure_ascii=False)
            + "\n\nDETAIL ROUND: "
            + str(round_index) + " of " + str(settings.selection_max_detail_rounds)
            + "\nIMAGE ORDER: Front, Top, Right, Isometric, then these candidate details:\n"
            + json.dumps(
                [
                    {"candidate_id": row["candidate_id"], "view": row["view"], "purpose": row.get("purpose")}
                    for row in supplied
                ],
                ensure_ascii=False,
            )
        )
        review = client.invoke(
            system_prompt=(
                "Use the global reference views, the neutral geometry catalog and only the requested candidate\n"
                "detail images to make the final CAD selection.\n"
                "\n"
                "Return selected only when every requested inlet/outlet/symmetry opening and one inner-wall seed\n"
                "face are identified by real catalog IDs. Preserve user-specified roles and names. Openings may be\n"
                "planar faces, closed loops, or single closed edges. The seed must be a face. Do not infer a fluid\n"
                "domain reuse merely because a body is closed: reuse requires the user's explicit statement.\n"
                "\n"
                "If more candidate evidence is necessary, return needs_details and request only new object/view\n"
                "pairs. Do not request evidence already supplied. If the evidence remains insufficient, return\n"
                "ambiguous or not_found and explain exactly what the user must clarify. Never guess an opening or\n"
                "seed face.\n"
            ),
            user_prompt=detail_prompt,
            images=[*images, *(Path(str(row["path"])) for row in supplied)],
            response_model=CadSelectionReview,
        )
        if review.status == "selected":
            return _validate_final_selection(catalog, review.selection)
        if review.status in {"ambiguous", "not_found"}:
            return _clarification_plan(
                status=review.status,
                reference_view=screening.reference_view,
                explanation=review.explanation,
                missing_information=review.missing_information,
                fluid_domain_action=screening.fluid_domain_action,
                fluid_domain_evidence=screening.fluid_domain_evidence,
            )
        history.append({"round": round_index, "review": review.model_dump(mode="json")})
        requests = review.detail_requests
    return _clarification_plan(
        status="ambiguous",
        reference_view=screening.reference_view,
        explanation=f"{settings.selection_max_detail_rounds} candidate-detail rounds did not uniquely identify the opening or inner-wall seed.",
        missing_information=[
            "Clarify the opening location, shape, or inner-wall feature for these unresolved objects: "
            + ", ".join(
                request.candidate_id + " (" + request.purpose + ")" for request in requests
            )
        ],
        fluid_domain_action=screening.fluid_domain_action,
        fluid_domain_evidence=screening.fluid_domain_evidence,
    )


def extract_mesh_requirements(
    *,
    catalog: GeometryCatalog,
    user_prompt: str,
    selection_plan: CadSelectionPlan,
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
) -> MeshRequirements:
    settings = config or RuntimeConfig()
    client = GroundingLLMClient.from_runtime_config(
        config=settings,
        audit_dir=Path(audit_dir) / "requirements",
    )
    body_boxes = [item.bbox.model_dump() for item in catalog.bodies if item.bbox is not None]
    prompt = (
        "USER REQUEST:\n"
        + user_prompt
        + "\n\nGEOMETRY SCALE (metres):\n"
        + json.dumps(
            {
                "body_bounding_boxes": body_boxes,
                "confirmed_selection_boundary_names": [
                    item.name for item in selection_plan.openings
                ],
            },
            ensure_ascii=False,
        )
    )
    return client.invoke(
        system_prompt=(
            "Extract Fluent Meshing requirements from the user's request.\n"
            "\n"
            "Record numeric values only when the user states them. Leave omitted controls as null so\n"
            "Fluent can use its native defaults. If a qualitative requirement necessarily needs a\n"
            "number, you may propose one using the supplied geometry scale; mark it as inferred and\n"
            "explain the basis. For local sizing, map the target to one of the supplied selection\n"
            "boundary names or leave boundary_name null when the request is not uniquely mappable.\n"
            "Mark explicitly supplied numeric controls as source=user, and proposed values as\n"
            "source=inferred. Changing a user-specified control requires human approval.\n"
            "For boundary-layer count and growth rate, populate the matching *_source field whenever\n"
            "you populate the number; otherwise leave both null.\n"
            "Use layers=0 only when the user explicitly disables boundary layers. Otherwise leave\n"
            "unspecified values null, including the layer count. For a requested subset of walls,\n"
            "populate boundary_names with the supplied names, or retain the target description if it\n"
            "cannot be bound. Never replace a specific target by \"all walls\". Boundary-layer growth\n"
            "rate affects boundary layers only. Accept any unambiguous length unit in user text, convert\n"
            "each numeric length to metres (unit=m), preserve original_expression and explain the conversion in basis.\n"
            "The first release supports internal flow, Watertight Geometry and poly-hexcore only. Do\n"
            "not infer boundary roles here: those belong to CAD object grounding. Report unsupported requests\n"
            "in unsupported_requirements instead of silently substituting defaults. Report missing or\n"
            "ambiguous units in missing_information and leave the affected control null; never guess units.\n"
            "\n"
            "length_unit is an explicit Fluent import-unit request, not the unit used by the\n"
            "geometry catalog. If the user does not request an import unit, return null. Do not\n"
            "copy the catalog's metres into this field. Keep notes limited to meshing requirements;\n"
            "do not repeat CAD selection/extraction instructions or boundary-role assignments.\n"
        ),
        user_prompt=prompt,
        response_model=MeshRequirements,
    )
