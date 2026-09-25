from pathlib import Path

import pytest

from src.config import RuntimeConfig
from src.nodes import cad
from src.services.contracts import CadSelectionPlan, CadSelectionReview, CadSelectionScreening
from src.services.errors import PipelineError
from src.services.geometry_models import GeometryCatalog
from src.services.grounding import plan_cad_selection


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

        def invoke(self, **kwargs):
            return next(self.responses)

    client = Client()
    monkeypatch.setattr(
        "src.services.grounding.GroundingLLMClient.from_runtime_config",
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
    install_client(monkeypatch, [screening, another_view, CadSelectionReview(
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
