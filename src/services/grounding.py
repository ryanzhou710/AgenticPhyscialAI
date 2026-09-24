"""LLM planning over a neutral SpaceClaim geometry catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.adapters.llm import GroundingLLMClient
from src.config import RuntimeConfig
from src.services.contracts import CadSelectionPlan, MeshRequirements
from src.services.geometry_models import GeometryCatalog
from src.services.prompts import load_prompt


def _model_images(catalog: GeometryCatalog) -> list[Path]:
    references: dict[str, Path] = {}
    sheets: list[Path] = []
    for row in catalog.images:
        path = Path(str(row.get("path", "")))
        if not path.is_file():
            continue
        view = row.get("view")
        if view in {"Front", "Top", "Right", "Isometric"} and not row.get("candidate_id"):
            references[str(view)] = path
        elif view == "CandidateContactSheet":
            sheets.append(path)
    return [
        references[name] for name in ("Front", "Top", "Right", "Isometric") if name in references
    ] + sheets


def _native_open_edges(catalog: GeometryCatalog) -> list[dict[str, Any]]:
    native = getattr(catalog, "native_catalog", {})
    public = native.get("public", {}) if isinstance(native, dict) else {}
    return [
        row
        for row in public.get("edges", [])
        if len(row.get("face_ids", [])) == 1
        and (row.get("curve_type") == "Circle" or row.get("closed") is True)
    ]


def _opening_candidate_context(catalog: GeometryCatalog) -> dict[str, Any]:
    """Describe the topology representations accepted by volume extraction."""
    return {
        "planar_faces": [
            row.id for row in catalog.faces
            if row.surface_type == "Plane"
        ],
        "closed_loops": [
            row.id for row in catalog.loops
            if row.closed is True
        ],
        "single_face_edges": _native_open_edges(catalog),
        "selection_guidance": (
            "Prefer a planar face or a closed loop for an arbitrary opening. "
            "A face may use its one inner loop (a cutout) or its one outer loop "
            "(a flush end). Use an edge only when it is itself a closed boundary."
        ),
    }


def plan_cad_selection(
    *,
    catalog: GeometryCatalog,
    user_prompt: str,
    audit_dir: str | Path,
    config: RuntimeConfig | None = None,
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
        + "\n\nIMAGE ORDER: Front, Top, Right, Isometric, followed by neutral candidate "
        "contact sheets. "
        "Candidate sheets label every visible candidate with its temporary ID."
    )
    answer = client.invoke(
        system_prompt=load_prompt("selection"),
        user_prompt=prompt,
        images=images,
        response_model=CadSelectionPlan,
    )
    if answer.status != "selected" or answer.fluid_domain_action == "ambiguous":
        return answer
    universe = catalog.by_id()
    requested = [item.candidate_id for item in answer.openings]
    requested.append(str(answer.seed_inner_wall_id))
    unknown = [candidate_id for candidate_id in requested if candidate_id not in universe]
    if unknown:
        raise ValueError("The model returned unavailable candidate IDs: " + ", ".join(unknown))
    if not str(answer.seed_inner_wall_id).startswith("F"):
        raise ValueError("The model-selected extraction seed is not a face")
    return answer


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
        system_prompt=load_prompt("requirements"),
        user_prompt=prompt,
        response_model=MeshRequirements,
    )
