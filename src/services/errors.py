"""Serializable, English user-facing error details for workflow failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MESSAGES: dict[str, tuple[str, str]] = {
    "UNEXPECTED_ERROR": (
        "Cause is not yet known.",
        "Inspect the raw error and detailed record, then retry or provide that record.",
    ),
    "RUNTIME_TIMEOUT": (
        "The operation exceeded its allowed runtime.",
        "Check whether the software is still running and adjust the timeout for this model if needed.",
    ),
    "CAD_SOFTWARE_ERROR": (
        "The SpaceClaim operation failed.",
        "Inspect the SpaceClaim raw output, license status, and detailed record.",
    ),
    "LLM_REQUEST_FAILED": (
        "The model request failed.",
        "Check model authentication, network access, and the request record before retrying.",
    ),
    "FLUENT_OPERATION_FAILED": (
        "The Fluent operation failed.",
        "Inspect the Fluent worker log and detailed record.",
    ),
    "MESH_PREVIEW_FAILED": (
        "The mesh passed validation, but Fluent could not create its preview image.",
        "Inspect the preview-error record; the validated mesh artifact is still available.",
    ),
    "CAD_CANDIDATE_PURPOSE_CONFLICT": (
        "The same candidate was requested as both an opening and an inner-wall seed.",
        "Choose separate opening contours and an inner-wall seed face.",
    ),
    "CAD_CANDIDATE_UNKNOWN": (
        "The model requested an object that is absent from the current geometry catalog.",
        "Reload the CAD and select only objects present in the catalog.",
    ),
    "CAD_DETAIL_REQUEST_LIMIT": (
        "This detail request exceeds the limit of 12 distinct candidates.",
        "Narrow the candidate set before requesting more detail images.",
    ),
    "CAD_DETAIL_EVIDENCE_REPEATED": (
        "The model requested only candidate detail images that were already supplied.",
        "Clarify the opening, inner wall, or reference-view feature that distinguishes the candidates.",
    ),
    "CAD_DETAIL_RENDER_FAILED": (
        "SpaceClaim did not produce every requested candidate detail image.",
        "Check whether the CAD objects remain valid and inspect the SpaceClaim rendering record.",
    ),
    "CAD_OPENING_UNSUPPORTED_OBJECT": (
        "An opening must be represented by a face, a closed loop, or a closed edge.",
        "Choose a supported opening object from the current catalog.",
    ),
    "CAD_OPENING_SUPPORT_FACE_MISSING": (
        "The opening contour has no usable support face.",
        "Choose a contour attached to a valid planar support face.",
    ),
    "CAD_OPENING_SUPPORT_NOT_PLANAR": (
        "The opening support face is not planar and cannot be safely capped by this workflow.",
        "Choose a planar opening contour or revise the CAD before extraction.",
    ),
    "CAD_OPENING_NOT_CLOSED": (
        "The opening contour is not a closed boundary and cannot be safely capped.",
        "Choose a closed contour or repair the opening in SpaceClaim.",
    ),
    "CAD_OPENING_EMPTY": (
        "No usable opening contour is available, or the contour contains no edges.",
        "Specify at least one valid inlet or outlet opening.",
    ),
    "CAD_OPENING_EDGE_MISSING": (
        "The opening contour references an edge that is absent from the current CAD.",
        "Reload the CAD and choose a contour whose edges still exist.",
    ),
    "CAD_OPENING_AMBIGUOUS": (
        "The opening object has multiple possible contours, so the cap cannot be identified uniquely.",
        "Inspect the listed candidate loops and explicitly select one opening contour.",
    ),
    "CAD_OPENING_UNKNOWN": (
        "The selected opening object is absent from the current CAD.",
        "Reload the CAD and choose an existing opening object.",
    ),
    "CAD_OPENING_OVERLAP": (
        "Two opening selections reuse the same boundary edge set.",
        "Choose non-overlapping contours for each inlet and outlet.",
    ),
    "CAD_SEED_NOT_FACE": (
        "The inner-wall seed must be a face object.",
        "Choose a face on the internal flow-path wall as the extraction seed.",
    ),
    "CAD_SEED_INVALID": (
        "The fluid-domain seed face is absent or is not a face object.",
        "Choose an existing face on the internal flow-path wall as the seed.",
    ),
    "CAD_EXTRACTION_RUNTIME_FAILED": (
        "The SpaceClaim extraction timed out or its runtime environment is unavailable.",
        "Check the license, SpaceClaim installation, and extraction timeout.",
    ),
    "CAD_VOLUME_EXTRACT_FAILED": (
        "SpaceClaim could not create one positive-volume fluid domain from the confirmed openings and seed face.",
        "Check the opening caps, seed location, and extraction-attempt record.",
    ),
    "CAD_CONFIRMED_SOLID_INVALID": (
        "The confirmed CAD must contain exactly one positive-volume solid.",
        "Remove extra bodies or repair zero-volume bodies, then save the CAD again.",
    ),
    "CAD_CONFIRMED_OPEN_TOPOLOGY": (
        "The confirmed fluid body has free edges and is not closed.",
        "Close every opening in SpaceClaim and save the CAD again.",
    ),
    "CAD_CONFIRMED_GROUP_ROLE_MISMATCH": (
        "The confirmed boundary groups do not match the confirmed role names.",
        "Assign roles again for every current boundary group.",
    ),
    "CAD_CONFIRMED_GROUP_EMPTY": (
        "A confirmed boundary group is empty.",
        "Add fluid-body faces to that group, or remove the empty group and confirm roles again.",
    ),
    "CAD_CONFIRMED_GROUP_MEMBER_INVALID": (
        "A confirmed boundary group contains an object outside the fluid body.",
        "Boundary groups may contain only faces from the fluid body.",
    ),
    "CAD_CONFIRMED_GROUP_OVERLAP": (
        "Confirmed boundary groups contain overlapping faces.",
        "Assign each fluid face to exactly one boundary group.",
    ),
    "CAD_CONFIRMED_GROUP_COVERAGE_INCOMPLETE": (
        "Confirmed boundary groups do not cover every fluid face.",
        "Assign every ungrouped fluid face to an inlet, outlet, wall, or symmetry group.",
    ),
    "CAD_CONFIRMED_TERMINAL_ROLE_MISSING": (
        "Confirmed boundary groups must include both inlet and outlet roles.",
        "Set one group to inlet and another group to outlet.",
    ),
}


class PipelineError(RuntimeError):
    """An expected pipeline failure with stable diagnostic metadata."""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        stage: str | None = None,
        substep: str | None = None,
        objects: list[dict[str, str]] | None = None,
        suggested_action: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        default_reason, default_action = _MESSAGES.get(code, (reason, "See the detailed record and retry."))
        self.detail = {
            "code": code,
            "stage": stage,
            "substep": substep,
            "reason": default_reason,
            "objects": objects or [],
            "suggested_action": default_action if code in _MESSAGES else (suggested_action or default_action),
        }
        self.evidence = evidence or {}


def _default_code(stage: str, error: BaseException) -> str:
    name = type(error).__name__
    if isinstance(error, TimeoutError):
        return "RUNTIME_TIMEOUT"
    if name == "SpaceClaimError":
        return "CAD_SOFTWARE_ERROR"
    if name in {"ProviderRequestError", "GroundingLLMError", "StructuredOutputError"}:
        return "LLM_REQUEST_FAILED"
    if name == "FluentWorkerError":
        return "FLUENT_OPERATION_FAILED"
    if isinstance(error, ValueError):
        return "INPUT_OR_SELECTION_INVALID"
    return "UNEXPECTED_ERROR"


def make_error_detail(
    stage: str,
    error: BaseException,
    *,
    evidence: dict[str, Any] | None = None,
    evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    """Convert any exception to a stable detail object without hiding raw text."""

    supplied = getattr(error, "detail", None)
    detail = dict(supplied) if isinstance(supplied, dict) else {}
    detail.setdefault("code", _default_code(stage, error))
    detail["stage"] = detail.get("stage") or stage
    detail.setdefault("substep", None)
    mapped = _MESSAGES.get(str(detail["code"]))
    detail.setdefault("reason", mapped[0] if mapped else (str(error) or "Cause is not yet known."))
    detail.setdefault("objects", [])
    detail.setdefault("suggested_action", mapped[1] if mapped else "See the detailed record and retry.")
    detail["raw_error"] = f"{type(error).__name__}: {error}"
    if evidence:
        detail["software_feedback"] = evidence
    if evidence_path is not None:
        detail["evidence_path"] = str(evidence_path)
    return detail
