"""CAD selection rules: explicit objects, target fluid body and boundary groups.

Keep model decisions and host-side validation here. Execution, clarification and
restart behavior belong in test_cad_pipeline.py; native rendering belongs in
test_spaceclaim_catalog.py.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import RuntimeConfig
from src.services.contracts import (
    BoundaryGroupPlan,
    BoundaryGroupReview,
    CadSelectionPlan,
    CadSelectionReview,
    CadSelectionScreening,
    FluidBodySelection,
)
from src.services.errors import PipelineError
from src.services.geometry_catalog import GeometryCatalog
from src.services.openings import resolve_extraction_selection
from src.services.selection import (
    _opening_candidate_context,
    plan_boundary_groups,
    plan_cad_selection,
    select_fluid_body,
)

# Selection fixtures


def catalog(*, bodies=None, faces=None, loops=None, edges=None) -> GeometryCatalog:
    return GeometryCatalog(
        catalog_id="catalog",
        geometry_id="geometry",
        bodies=bodies or [],
        faces=faces or [],
        loops=loops or [],
        edges=edges or [],
    )


def extract_plan() -> CadSelectionPlan:
    return CadSelectionPlan(
        status="selected",
        reference_view="Isometric",
        fluid_domain_action="extract",
        extraction_strategy="faces",
        seed_inner_wall_id="F_seed",
        openings=[
            {
                "selection_kind": "face",
                "object_ids": ["F_open"],
                "name": "inlet",
                "role": "inlet",
                "description": "inlet",
                "reason": "request",
            }
        ],
        explanation="explicit selection",
    )


def install_client(monkeypatch, responses):
    class Client:
        def __init__(self):
            self.calls = []
            self.responses = iter(responses)

        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            return next(self.responses)

        def probe_vision(self):
            return type("Probe", (), {"status": type("Status", (), {"value": "verified"})()})()

    client = Client()
    monkeypatch.setattr(
        "src.services.selection.GroundingLLMClient.from_runtime_config", lambda **_: client
    )
    return client


def render_to(tmp_path: Path, calls: list):
    def render(requests):
        calls.append(requests)
        result = []
        for request in requests:
            for view in request["detail_views"]:
                path = tmp_path / f"{request['candidate_id']}-{view}.png"
                path.write_bytes(b"detail")
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


def selected_group_plan(*, groups) -> BoundaryGroupPlan:
    return BoundaryGroupPlan(
        status="selected",
        target_catalog_id="catalog",
        groups=groups,
        explanation="all faces assigned",
    )


def target_catalog() -> GeometryCatalog:
    return catalog(
        bodies=[{"id": "B1", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 1.0}],
        faces=[
            {"id": "F1", "kind": "face", "body_id": "B1"},
            {"id": "F2", "kind": "face", "body_id": "B1"},
            {"id": "F3", "kind": "face", "body_id": "B1"},
        ],
    )


def opening_catalog(*, faces=None, edges=None, loops=None):
    return {
        "public": {
            "bodies": [],
            "faces": faces or [],
            "edges": edges or [],
            "loops": loops or [],
        }
    }


def opening_plan(*, strategy="faces", object_ids=None):
    return {
        "fluid_domain_action": "extract",
        "extraction_strategy": strategy,
        "seed_inner_wall_id": "F_seed",
        "openings": [
            {
                "selection_kind": "face" if strategy == "faces" else "loop",
                "object_ids": object_ids or (["F_open"] if strategy == "faces" else ["L_open"]),
                "name": "inlet",
                "role": "inlet",
                "description": "requested inlet",
                "reason": "user request",
            }
        ],
    }


# Explicit extraction and reuse protocol


def test_opening_context_lists_all_real_objects_without_planarity_or_closure_filters():
    context = _opening_candidate_context(
        catalog(
            bodies=[{"id": "B1", "kind": "body"}],
            faces=[{"id": "F_curved", "kind": "face", "surface_type": "Cylinder"}],
            loops=[{"id": "L_open", "kind": "loop", "closed": False}],
            edges=[{"id": "E_many", "kind": "edge", "face_ids": ["F1", "F2", "F3"]}],
        )
    )

    assert context["face_candidates"] == ["F_curved"]
    assert context["loop_candidates"] == ["L_open"]
    assert context["edge_candidates"] == ["E_many"]
    assert context["body_candidates"] == ["B1"]
    assert "Never infer" in context["selection_guidance"]


def test_selection_protocol_requires_explicit_extract_or_reuse_fields():
    with pytest.raises(ValidationError, match="extract requires"):
        CadSelectionPlan(
            status="selected",
            reference_view="Front",
            fluid_domain_action="extract",
            explanation="incomplete",
        )
    with pytest.raises(ValidationError, match="reuse must select only"):
        CadSelectionPlan(
            status="selected",
            reference_view="Front",
            fluid_domain_action="reuse",
            fluid_body_id="B1",
            seed_inner_wall_id="F1",
            explanation="mixed protocol",
        )


def test_selected_plan_requires_seed_and_opening():
    with pytest.raises(ValueError):
        CadSelectionPlan.model_validate(
            {
                "status": "selected",
                "reference_view": "Isometric",
                "openings": [],
                "seed_inner_wall_id": None,
                "explanation": "missing",
            }
        )


def test_face_method_preserves_an_explicit_nonplanar_face_without_loop_inference():
    selection = resolve_extraction_selection(
        opening_catalog(
            faces=[
                {"id": "F_open", "surface_type": "Cylinder"},
                {"id": "F_seed", "surface_type": "Nurbs"},
            ],
            loops=[
                {
                    "id": "L_open",
                    "face_id": "F_open",
                    "is_outer": False,
                    "closed": False,
                    "edge_ids": ["E1"],
                }
            ],
        ),
        opening_plan(),
    )

    assert selection["extraction_strategy"] == "faces"
    assert selection["terminal_records"][0]["object_ids"] == ["F_open"]
    assert selection["terminal_records"][0]["native_ids"] == ["F_open"]


def test_edge_method_expands_only_the_explicit_loop_without_closure_or_adjacency_gate():
    selection = resolve_extraction_selection(
        opening_catalog(
            faces=[{"id": "F_seed"}],
            edges=[
                {"id": "E1", "closed": False, "face_ids": []},
                {"id": "E2", "closed": False, "face_ids": ["F_other", "F_more", "F_last"]},
            ],
            loops=[
                {"id": "L_open", "face_id": "F_other", "closed": False, "edge_ids": ["E1", "E2"]}
            ],
        ),
        opening_plan(strategy="edges"),
    )

    assert selection["terminal_records"][0]["native_ids"] == ["E1", "E2"]


def test_duplicate_or_overlapping_native_edges_are_left_for_spaceclaim_to_evaluate():
    plan = opening_plan(strategy="edges", object_ids=["E1"])
    plan["openings"][0]["selection_kind"] = "edges"
    plan["openings"].append(
        {
            "selection_kind": "edges",
            "object_ids": ["E1"],
            "name": "outlet",
            "role": "outlet",
            "description": "same native edge by explicit request",
            "reason": "exercise native validation",
        }
    )

    selection = resolve_extraction_selection(
        opening_catalog(faces=[{"id": "F_seed"}], edges=[{"id": "E1", "face_ids": ["F1"]}]), plan
    )

    assert [record["native_ids"] for record in selection["terminal_records"]] == [["E1"], ["E1"]]


@pytest.mark.parametrize(
    ("plan_change", "code"),
    [
        (lambda plan: plan.update(seed_inner_wall_id="E_missing"), "CAD_SEED_INVALID"),
        (
            lambda plan: plan["openings"][0].update(object_ids=["E_missing"]),
            "CAD_CANDIDATE_UNKNOWN",
        ),
        (
            lambda plan: plan.update(extraction_strategy="edges"),
            "CAD_EXTRACTION_OBJECT_TYPE",
        ),
    ],
)
def test_only_object_existence_and_explicit_type_are_checked_before_native_extraction(plan_change, code):
    plan = opening_plan()
    plan_change(plan)
    with pytest.raises(PipelineError) as error:
        resolve_extraction_selection(
            opening_catalog(faces=[{"id": "F_open"}, {"id": "F_seed"}]), plan
        )
    assert error.value.detail["code"] == code


# Model detail requests for extraction


def test_screening_renders_only_requested_details_and_returns_explicit_face_method(
    tmp_path, monkeypatch
):
    geometry = catalog(
        faces=[{"id": "F_open", "kind": "face"}, {"id": "F_seed", "kind": "face"}]
    )
    client = install_client(
        monkeypatch,
        [
            CadSelectionScreening(
                status="needs_details",
                reference_view="Isometric",
                candidates=[
                    {"candidate_id": "F_open", "purpose": "opening", "reason": "opening"},
                    {"candidate_id": "F_seed", "purpose": "seed", "reason": "seed"},
                ],
                explanation="need local views",
            ),
            CadSelectionReview(
                status="selected", selection=extract_plan(), explanation="identified"
            ),
        ],
    )
    render_calls = []

    result = plan_cad_selection(
        catalog=geometry,
        user_prompt="extract the duct",
        audit_dir=tmp_path,
        config=RuntimeConfig(selection_max_candidates_per_round=12, selection_max_detail_rounds=3),
        detail_renderer=render_to(tmp_path, render_calls),
    )

    assert result.extraction_strategy == "faces"
    assert result.openings[0].object_ids == ["F_open"]
    assert {item["candidate_id"] for item in render_calls[0]} == {"F_open", "F_seed"}
    assert len(client.calls) == 2


# Target fluid-body selection


def test_multiple_positive_bodies_require_an_llm_choice_not_order_or_volume(tmp_path, monkeypatch):
    geometry = catalog(
        bodies=[
            {"id": "B1", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 1.0},
            {"id": "B2", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 99.0},
        ]
    )
    client = install_client(
        monkeypatch,
        [FluidBodySelection(status="selected", body_id="B1", explanation="matches request")],
    )

    selected = select_fluid_body(
        catalog=geometry,
        user_prompt="use the left small chamber",
        selection_plan=extract_plan(),
        audit_dir=tmp_path,
    )

    assert selected.body_id == "B1"
    assert len(client.calls) == 1


def test_target_body_selection_can_request_cached_detail_views(tmp_path, monkeypatch):
    geometry = catalog(
        bodies=[
            {"id": "B1", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 1.0},
            {"id": "B2", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 2.0},
        ]
    )
    client = install_client(
        monkeypatch,
        [
            FluidBodySelection(
                status="needs_details",
                detail_requests=[{"candidate_id": "B1", "purpose": "body", "reason": "compare body"}],
                explanation="need body view",
            ),
            FluidBodySelection(status="selected", body_id="B1", explanation="matched body"),
        ],
    )
    render_calls = []

    selected = select_fluid_body(
        catalog=geometry,
        user_prompt="use the body with the requested channel",
        selection_plan=extract_plan(),
        audit_dir=tmp_path,
        config=RuntimeConfig(selection_max_detail_rounds=1),
        detail_renderer=render_to(tmp_path, render_calls),
    )

    assert selected.body_id == "B1"
    assert render_calls[0][0]["candidate_id"] == "B1"
    assert len(client.calls) == 2


def test_single_positive_body_is_used_without_a_second_model_request(tmp_path, monkeypatch):
    geometry = catalog(
        bodies=[{"id": "B1", "kind": "body", "solid_or_sheet": "solid", "volume_m3": 1.0}]
    )
    client = install_client(monkeypatch, [])

    selected = select_fluid_body(
        catalog=geometry,
        user_prompt="extract fluid",
        selection_plan=extract_plan(),
        audit_dir=tmp_path,
    )

    assert selected.status == "selected"
    assert selected.body_id == "B1"
    assert not client.calls


# Boundary-group planning and correction


def test_boundary_group_plan_allows_multiple_faces_in_one_named_group(tmp_path, monkeypatch):
    client = install_client(
        monkeypatch,
        [
            BoundaryGroupReview(
                status="selected",
                selection=selected_group_plan(
                    groups=[
                        {"name": "inlet", "role": "inlet", "face_ids": ["F1"], "reason": "inlet"},
                        {"name": "walls", "role": "wall", "face_ids": ["F2", "F3"], "reason": "walls"},
                    ]
                ),
                explanation="complete",
            )
        ],
    )

    result = plan_boundary_groups(
        catalog=target_catalog(),
        target_body_id="B1",
        user_prompt="group actual fluid faces",
        selection_plan=extract_plan(),
        audit_dir=tmp_path,
    )

    assert result.groups[1].face_ids == ["F2", "F3"]
    assert len(client.calls) == 1
    assert '"prior_boundary_intent"' in client.calls[0]["user_prompt"]


def test_invalid_boundary_coverage_returns_host_feedback_to_the_model(tmp_path, monkeypatch):
    client = install_client(
        monkeypatch,
        [
            BoundaryGroupReview(
                status="selected",
                selection=selected_group_plan(
                    groups=[{"name": "inlet", "role": "inlet", "face_ids": ["F1"], "reason": "partial"}]
                ),
                explanation="partial",
            ),
            BoundaryGroupReview(
                status="selected",
                selection=selected_group_plan(
                    groups=[
                        {"name": "inlet", "role": "inlet", "face_ids": ["F1"], "reason": "inlet"},
                        {"name": "walls", "role": "wall", "face_ids": ["F2", "F3"], "reason": "walls"},
                    ]
                ),
                explanation="corrected",
            ),
        ],
    )

    result = plan_boundary_groups(
        catalog=target_catalog(), target_body_id="B1", user_prompt="group actual fluid faces", audit_dir=tmp_path
    )

    assert result.status == "selected"
    assert len(client.calls) == 2
    assert "Boundary groups omit target faces" in client.calls[1]["user_prompt"]


@pytest.mark.parametrize("groups", [
    [],
    [{"name": "", "role": "wall", "face_ids": [], "reason": "invalid"}],
    [
        {"name": "same", "role": "wall", "face_ids": ["F1"], "reason": "duplicate"},
        {"name": "same", "role": "wall", "face_ids": ["F1", "unknown"], "reason": "invalid"},
    ],
])
def test_invalid_groups_can_be_corrected_on_last_review(tmp_path, monkeypatch, groups):
    corrected = selected_group_plan(groups=[
        {"name": "all", "role": "wall", "face_ids": ["F1", "F2", "F3"], "reason": "corrected"}
    ])
    client = install_client(monkeypatch, [
        BoundaryGroupReview(status="selected", selection=selected_group_plan(groups=groups), explanation="proposal"),
        BoundaryGroupReview(status="selected", selection=corrected, explanation="corrected"),
    ])
    result = plan_boundary_groups(
        catalog=target_catalog(), target_body_id="B1", user_prompt="group faces",
        audit_dir=tmp_path, config=RuntimeConfig(selection_max_detail_rounds=1),
    )
    assert result == corrected
    assert "host_validation_errors" in client.calls[1]["user_prompt"]
    assert '"review"' in client.calls[1]["user_prompt"]
    assert len(client.calls) == 2


@pytest.mark.parametrize("repeat", [False, True])
def test_boundary_final_detail_response_and_repeated_evidence(tmp_path, monkeypatch, repeat):
    request = BoundaryGroupReview(
        status="needs_details", explanation="inspect",
        detail_requests=[{"candidate_id": "F1", "purpose": "boundary", "reason": "inspect"}],
    )
    selected = BoundaryGroupReview(
        status="selected", explanation="complete",
        selection=selected_group_plan(groups=[
            {"name": "all", "role": "wall", "face_ids": ["F1", "F2", "F3"], "reason": "all"}
        ]),
    )
    client = install_client(monkeypatch, [request, request if repeat else selected])
    renders = []
    result = plan_boundary_groups(
        catalog=target_catalog(), target_body_id="B1", user_prompt="group faces", audit_dir=tmp_path,
        config=RuntimeConfig(selection_max_detail_rounds=3 if repeat else 1),
        detail_renderer=render_to(tmp_path, renders),
    )
    assert result.status == ("ambiguous" if repeat else "selected")
    assert len(renders) == 1
    assert len(client.calls) == 2
