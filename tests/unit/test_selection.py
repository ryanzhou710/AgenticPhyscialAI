"""Model candidate selection, detail requests and clarification budgets."""

from pathlib import Path

import pytest

from src.config import RuntimeConfig
from src.nodes import cad
from src.services.contracts import CadSelectionPlan, CadSelectionReview, CadSelectionScreening
from src.services.errors import PipelineError
from src.services.geometry_catalog import GeometryCatalog
from src.services.selection import _opening_candidate_context, plan_cad_selection


def test_opening_context_exposes_planar_faces_and_closed_arbitrary_loops():
    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        faces=[
            {"id": "F_RECT", "kind": "face", "surface_type": "Plane"},
            {"id": "F_WALL", "kind": "face", "surface_type": "Cylinder"},
        ],
        loops=[
            {
                "id": "L_RECT",
                "kind": "loop",
                "face_id": "F_RECT",
                "closed": True,
                "is_outer": True,
                "edge_ids": ["E1", "E2", "E3", "E4"],
            },
            {
                "id": "L_OPEN",
                "kind": "loop",
                "face_id": "F_RECT",
                "closed": False,
            },
        ],
    )

    context = _opening_candidate_context(catalog)

    assert context["planar_faces"] == ["F_RECT"]
    assert context["closed_loops"] == ["L_RECT"]
    assert "arbitrary opening" in context["selection_guidance"]


def test_visual_edge_candidates_require_native_closed_single_face_edges():
    from src.services.geometry_catalog import GeometryCatalog
    from src.services.selection import _native_open_edges

    native_edges = [
        {"id": "E-circle-open", "curve_type": "Circle", "closed": True, "face_ids": ["F1"]},
        {"id": "E-circle-seam", "curve_type": "Circle", "face_ids": ["F1", "F2"]},
        {"id": "E-line-open", "curve_type": "Line", "face_ids": ["F1"]},
        {"id": "E-circle-not-closed", "curve_type": "Circle", "closed": False, "face_ids": ["F1"]},
        {"id": "E-spline-closed", "curve_type": "Spline", "closed": True, "face_ids": ["F1"]},
        {"id": "E-line-seam", "curve_type": "Line", "face_ids": ["F1", "F2"]},
    ]
    catalog = GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        native_catalog={"public": {"edges": native_edges}},
    )

    assert _native_open_edges(catalog) == [native_edges[0], native_edges[4]]


def catalog() -> GeometryCatalog:
    return GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        faces=[
            {"id": "F1", "kind": "face", "surface_type": "Plane"},
            {"id": "F2", "kind": "face", "surface_type": "Cylinder"},
        ],
        loops=[{"id": "L1", "kind": "loop", "face_id": "F1", "closed": True}],
        native_catalog={"public": {"edges": []}},
    )


def selected_plan() -> CadSelectionPlan:
    return CadSelectionPlan(
        status="selected",
        reference_view="Isometric",
        explanation="enough evidence",
        seed_inner_wall_id="F2",
        openings=[
            {
                "candidate_id": "F1",
                "role": "inlet",
                "name": "inlet",
                "description": "opening",
                "reason": "detail evidence",
            }
        ],
    )


def install_client(monkeypatch, responses):
    class Client:
        def __init__(self):
            self.responses = iter(responses)
            self.calls = []

        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            return next(self.responses)

    client = Client()
    monkeypatch.setattr(
        "src.services.selection.GroundingLLMClient.from_runtime_config",
        lambda **kwargs: client,
    )
    return client


def render_to(tmp_path, calls):
    def render(requests):
        calls.append(requests)
        result = []
        for request in requests:
            for view in request["detail_views"]:
                path = tmp_path / f"{request['candidate_id']}-{view}.png"
                path.write_bytes(b"evidence")
                result.append(
                    {
                        "candidate_id": request["candidate_id"],
                        "purpose": request["purpose"],
                        "view": view,
                        "path": str(path),
                    }
                )
        return result

    return render


def test_screening_requests_only_defaults_and_then_returns_final_plan(monkeypatch, tmp_path):
    screening = CadSelectionScreening(
        status="needs_details",
        reference_view="Isometric",
        explanation="need local evidence",
        candidates=[
            {"candidate_id": "F1", "purpose": "opening", "reason": "opening"},
            {"candidate_id": "F2", "purpose": "seed", "reason": "inner wall"},
        ],
    )
    review = CadSelectionReview(status="selected", selection=selected_plan(), explanation="confirmed")
    install_client(monkeypatch, [screening, review])
    calls = []

    plan = plan_cad_selection(
        catalog=catalog(),
        user_prompt="make a flow volume",
        audit_dir=tmp_path,
        config=RuntimeConfig(),
        detail_renderer=render_to(tmp_path, calls),
    )

    assert plan == selected_plan()
    assert calls == [
        [
            {
                "candidate_id": "F1",
                "purpose": "opening",
                "detail_views": ["Selected"],
                "reason": "opening",
            },
            {
                "candidate_id": "F2",
                "purpose": "seed",
                "detail_views": ["OwnerContext", "SelectedProxy"],
                "reason": "inner wall",
            },
        ]
    ]


def test_detail_requests_reuse_existing_evidence(monkeypatch, tmp_path):
    screening = CadSelectionScreening(
        status="needs_details",
        reference_view="Front",
        explanation="first look",
        candidates=[{"candidate_id": "F1", "purpose": "opening", "reason": "first"}],
    )
    another_view = CadSelectionReview(
        status="needs_details",
        explanation="need context",
        detail_requests=[
            {
                "candidate_id": "F1",
                "purpose": "opening",
                "views": ["Selected", "OwnerContext"],
                "reason": "compare context",
            }
        ],
    )
    client = install_client(monkeypatch, [screening, another_view, CadSelectionReview(
        status="selected", selection=selected_plan(), explanation="confirmed"
    )])
    calls = []

    plan_cad_selection(
        catalog=catalog(),
        user_prompt="make a flow volume",
        audit_dir=tmp_path,
        detail_renderer=render_to(tmp_path, calls),
    )

    assert [call[0]["detail_views"] for call in calls] == [["Selected"], ["OwnerContext"]]
    assert "need context" in client.calls[2]["user_prompt"]
    assert "first look" in client.calls[2]["user_prompt"]
    assert "AVAILABLE EVIDENCE" in client.calls[2]["user_prompt"]


def test_three_detail_rounds_pause_for_clarification(monkeypatch, tmp_path):
    screening = CadSelectionScreening(
        status="needs_details",
        reference_view="Front",
        explanation="first look",
        candidates=[
            {"candidate_id": "F1", "purpose": "opening", "views": ["Selected"], "reason": "one"}
        ],
    )
    second = CadSelectionReview(
        status="needs_details",
        explanation="second look",
        detail_requests=[
            {"candidate_id": "F1", "purpose": "opening", "views": ["OwnerContext"], "reason": "two"}
        ],
    )
    third = CadSelectionReview(
        status="needs_details",
        explanation="third look",
        detail_requests=[
            {"candidate_id": "F1", "purpose": "opening", "views": ["SelectedProxy"], "reason": "three"}
        ],
    )
    install_client(monkeypatch, [screening, second, third, third])
    calls = []

    plan = plan_cad_selection(
        catalog=catalog(),
        user_prompt="make a flow volume",
        audit_dir=tmp_path,
        detail_renderer=render_to(tmp_path, calls),
    )

    assert plan.status == "ambiguous"
    assert len(calls) == 3


def test_screening_rejects_unknown_candidate_before_rendering(monkeypatch, tmp_path):
    screening = CadSelectionScreening(
        status="needs_details",
        reference_view="Front",
        explanation="bad id",
        candidates=[{"candidate_id": "F999", "purpose": "opening", "reason": "bad"}],
    )
    install_client(monkeypatch, [screening])

    with pytest.raises(PipelineError) as error:
        plan_cad_selection(
            catalog=catalog(),
            user_prompt="make a flow volume",
            audit_dir=tmp_path,
            detail_renderer=lambda requests: pytest.fail("render must not start"),
        )
    assert error.value.detail["code"] == "CAD_CANDIDATE_UNKNOWN"


def test_query_geometry_does_not_render_candidate_images(monkeypatch, tmp_path):
    from src.services import spaceclaim_runtime

    calls = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        def catalog(self, path, **kwargs):
            calls.append(kwargs)
            return catalog(), Path(tmp_path / "catalog.json")

        def close(self):
            pass

    monkeypatch.setattr(spaceclaim_runtime, "SpaceClaimRunner", Runner)
    result = cad.query_geometry(
        {
            "run_dir": str(tmp_path),
            "runtime_dir": str(tmp_path),
            "working_geometry": str(tmp_path / "working.scdoc"),
            "ui_mode": "hidden",
            "runtime_config": RuntimeConfig().model_dump(mode="json"),
        }
    )

    assert result["error"] == ""
    assert calls == [{"render_candidates": False}]


@pytest.mark.parametrize("limit", [1, 2])
def test_configured_round_budget(monkeypatch, tmp_path, limit):
    screening = CadSelectionScreening(
        status="needs_details", reference_view="Front", explanation="initial",
        candidates=[{"candidate_id": "F1", "purpose": "opening", "reason": "check"}],
    )
    reviews = [CadSelectionReview(
        status="needs_details", explanation="unresolved",
        detail_requests=[{"candidate_id": "F1", "purpose": "opening", "reason": "check",
                          "views": [view]}],
    ) for view in ("OwnerContext", "SelectedProxy")]
    install_client(monkeypatch, [screening, *reviews])
    calls = []
    result = plan_cad_selection(
        catalog=catalog(), user_prompt="flow", audit_dir=tmp_path,
        config=RuntimeConfig(selection_max_detail_rounds=limit),
        detail_renderer=render_to(tmp_path, calls),
    )
    assert result.status == "ambiguous"
    assert len(calls) == limit
    assert result.explanation.startswith(str(limit))


def test_configured_candidate_limit_checked_before_render(monkeypatch, tmp_path):
    screening = CadSelectionScreening(
        status="needs_details", reference_view="Front", explanation="initial",
        candidates=[{"candidate_id": face, "purpose": "opening", "reason": "check"}
                    for face in ("F1", "F2")],
    )
    install_client(monkeypatch, [screening])
    with pytest.raises(PipelineError, match="1-candidate limit"):
        plan_cad_selection(
            catalog=catalog(), user_prompt="flow", audit_dir=tmp_path,
            config=RuntimeConfig(selection_max_candidates_per_round=1),
            detail_renderer=lambda requests: pytest.fail("must not render"),
        )


@pytest.mark.parametrize("issue", ["missing_information", "unsupported_requirements"])
def test_requirement_issues_use_existing_clarification_route(monkeypatch, tmp_path, issue):
    from contextlib import nullcontext

    from src.services.contracts import MeshRequirements

    monkeypatch.setattr(cad, "open_spaceclaim_reader", lambda *args: nullcontext(object()))
    monkeypatch.setattr(cad, "plan_cad_selection", lambda **kwargs: selected_plan())
    monkeypatch.setattr(cad, "extract_mesh_requirements", lambda **kwargs: MeshRequirements(**{
        issue: ["Specify the intended length unit or a supported method."],
    }))
    result = cad.understand_prompt({
        "catalog": catalog().model_dump(), "prompt": "size 20", "run_dir": str(tmp_path),
        "working_geometry": "model.scdoc",
    })
    request = result["error_evidence"]["human_request"]
    assert request["kind"] == "clarification"
    assert request["evidence"][issue]
    assert "mesh_requirements" not in result
