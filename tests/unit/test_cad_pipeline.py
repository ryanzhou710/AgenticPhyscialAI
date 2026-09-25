"""CAD preparation lifecycle: extract/reuse, isolate, group, clarify and restart.

Keep regressions for these CAD stages together, including downstream state
invalidation. Selection rules belong in test_cad_selection.py; final user
confirmation and document saving belong in test_confirmation.py.
"""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.adapters.spaceclaim_build import SpaceClaimBuildAdapter
from src.adapters.spaceclaim_query import SpaceClaimError
from src.nodes import confirmation
from src.services import reviewer
from src.services.contracts import (
    RepairDecision,
)
from src.services.errors import PipelineError
from src.services.execution import cad_restart_update
from src.services.geometry_catalog import GeometryCatalog

# CAD state and native catalog fixtures


def native_catalog() -> dict:
    return {"public": {"bodies": [], "faces": [], "edges": [], "loops": []}, "internal": {}}


def catalog(*, body_id="B1", moniker="body-1", faces=None) -> GeometryCatalog:
    faces = faces if faces is not None else ["F1", "F2"]
    return GeometryCatalog(
        catalog_id="catalog-" + body_id,
        geometry_id="geometry-" + body_id,
        bodies=[
            {
                "id": body_id,
                "kind": "body",
                "moniker": moniker,
                "solid_or_sheet": "solid",
                "volume_m3": 1.0,
                "face_ids": faces,
            }
        ],
        faces=[{"id": value, "kind": "face", "body_id": body_id} for value in faces],
        native_catalog=native_catalog(),
    )


def extraction_plan() -> dict:
    return {
        "fluid_domain_action": "extract",
        "extraction_strategy": "faces",
        "seed_inner_wall_id": "F_seed",
        "openings": [
            {
                "selection_kind": "face",
                "object_ids": ["F_open"],
                "name": "inlet",
                "role": "inlet",
                "description": "inlet",
                "reason": "user request",
            }
        ],
    }


def extraction_input_catalog() -> GeometryCatalog:
    result = catalog(body_id="B_input", moniker="input-body", faces=["F_open", "F_seed"])
    return result


def state(tmp_path: Path, *, input_catalog: GeometryCatalog, plan: dict) -> dict:
    (tmp_path / "state").mkdir(exist_ok=True)
    return {
        "runtime_dir": str(tmp_path),
        "run_dir": str(tmp_path),
        "ui_mode": "hidden",
        "runtime_config": {},
        "prompt": "extract the requested fluid body",
        "working_geometry": str(tmp_path / "original.scdoc"),
        "catalog": input_catalog.model_dump(mode="json"),
        "selection_plan": plan,
        "artifacts": {},
    }


# Explicit extraction and reuse execution


def test_extract_records_all_native_positive_candidates_without_a_host_topology_gate(tmp_path, monkeypatch):
    from src.nodes import cad

    produced = GeometryCatalog(
        catalog_id="extracted",
        geometry_id="extracted",
        bodies=[
            {"id": "B10", "kind": "body", "moniker": "created-one", "solid_or_sheet": "solid", "volume_m3": 1.0},
            {"id": "B11", "kind": "body", "moniker": "created-two", "solid_or_sheet": "solid", "volume_m3": 2.0},
        ],
        native_catalog=native_catalog(),
    )
    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def extract_volume(self, **kwargs):
            calls.append(kwargs)
            return {
                "transfer": {
                    "candidate_bodies": [
                        {"body_id": "B10", "moniker": "created-one", "volume_m3": 1.0},
                        {"body_id": "B11", "moniker": "created-two", "volume_m3": 2.0},
                    ]
                }
            }

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    monkeypatch.setattr(cad, "_catalog_for", lambda *_: produced)
    result = cad.extract_volume(
        state(tmp_path, input_catalog=extraction_input_catalog(), plan=extraction_plan())
    )

    assert result["error"] == ""
    assert calls[0]["selection_plan"]["extraction_strategy"] == "faces"
    assert result["extraction"]["candidate_body_monikers"] == ["created-one", "created-two"]
    assert result["extraction_catalog"]["catalog_id"] == "extracted"


def test_reuse_never_requires_seed_or_runs_volume_extraction(tmp_path, monkeypatch):
    from src.nodes import cad

    input_catalog = catalog(body_id="B2", moniker="reuse-body")

    class Builder:
        def __init__(self, **kwargs):
            pytest.fail("reuse must not launch volume extraction")

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    result = cad.extract_volume(
        state(
            tmp_path,
            input_catalog=input_catalog,
            plan={"fluid_domain_action": "reuse", "fluid_body_id": "B2", "openings": []},
        )
    )

    assert result["error"] == ""
    assert result["extraction"]["source_mode"] == "reuse"
    assert result["extraction"]["candidate_body_monikers"] == ["reuse-body"]


def test_native_failure_does_not_switch_extraction_method(monkeypatch, tmp_path):
    adapter = SpaceClaimBuildAdapter(runtime_dir=tmp_path, ui_mode="hidden")
    calls = []

    def execute(operation, payload, **kwargs):
        calls.append(payload)
        raise SpaceClaimError("VolumeExtract failed")

    monkeypatch.setattr(adapter, "_execute", execute)
    with pytest.raises(PipelineError) as error:
        adapter.extract_volume(
            source=tmp_path / "original.scdoc",
            output=tmp_path / "extracted.scdoc",
            catalog={
                "public": {
                    "bodies": [],
                    "faces": [{"id": "F_open"}, {"id": "F_seed"}],
                    "edges": [],
                    "loops": [],
                }
            },
            selection_plan=extraction_plan(),
        )

    assert error.value.detail["code"] == "CAD_VOLUME_EXTRACT_FAILED"
    assert [call["extraction_strategy"] for call in calls] == ["faces"]


# Isolate the selected fluid body and refresh its catalog


def test_select_fluid_body_isolates_the_explicit_choice_and_requeries_new_ids(tmp_path, monkeypatch):
    from src.nodes import cad

    extracted = GeometryCatalog(
        catalog_id="extracted",
        geometry_id="extracted",
        bodies=[
            {"id": "B10", "kind": "body", "moniker": "created-one", "solid_or_sheet": "solid", "volume_m3": 1.0},
            {"id": "B11", "kind": "body", "moniker": "created-two", "solid_or_sheet": "solid", "volume_m3": 2.0},
        ],
        native_catalog=native_catalog(),
    )
    isolated = catalog(body_id="B1", moniker="created-two", faces=["F-new"])
    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def isolate_body(self, **kwargs):
            calls.append(kwargs)
            return {"target_body": {"moniker": "created-two"}}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    monkeypatch.setattr(cad.selection, "select_fluid_body", lambda **_: type("Choice", (), {
        "status": "selected", "body_id": "B11", "explanation": "requested body"
    })())
    monkeypatch.setattr(cad, "_catalog_for", lambda *_: isolated)
    (tmp_path / "state").mkdir()
    result = cad.select_fluid_body(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "runtime_config": {},
            "ui_mode": "hidden",
            "prompt": "use the second extracted body",
            "selection_plan": {"fluid_domain_action": "extract"},
            "extraction": {
                "candidate_geometry": str(tmp_path / "extraction-candidates.scdoc"),
                "candidate_body_monikers": ["created-one", "created-two"],
            },
            "extraction_catalog": extracted.model_dump(mode="json"),
            "artifacts": {},
        }
    )

    assert result["error"] == ""
    assert calls[0]["target_body_id"] == "B11"
    assert result["target_body"]["body_id"] == "B1"
    assert result["working_geometry"].endswith("target-fluid.scdoc")


def test_reuse_selects_its_explicit_body_without_a_seed_or_second_model_call(tmp_path, monkeypatch):
    from src.nodes import cad

    extracted = catalog(body_id="B2", moniker="reuse-body", faces=["F_old"])
    isolated = catalog(body_id="B1", moniker="reuse-body", faces=["F_new"])
    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def isolate_body(self, **kwargs):
            calls.append(kwargs)
            return {"target_body": {"moniker": "reuse-body"}}

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    monkeypatch.setattr(cad, "_catalog_for", lambda *_: isolated)
    (tmp_path / "state").mkdir()
    result = cad.select_fluid_body(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "runtime_config": {},
            "ui_mode": "hidden",
            "prompt": "the supplied CAD is already the fluid body",
            "selection_plan": {
                "status": "selected",
                "reference_view": "Isometric",
                "fluid_domain_action": "reuse",
                "fluid_body_id": "B2",
                "explanation": "explicit reuse",
            },
            "extraction": {
                "candidate_geometry": str(tmp_path / "original.scdoc"),
                "candidate_body_monikers": ["reuse-body"],
            },
            "extraction_catalog": extracted.model_dump(mode="json"),
            "artifacts": {},
        }
    )

    assert result["error"] == ""
    assert calls[0]["target_body_id"] == "B2"
    assert result["target_body"]["body_id"] == "B1"


# Apply groups to the actual target faces


def test_labeling_accepts_a_multi_face_group_and_does_not_add_default_wall(tmp_path, monkeypatch):
    from src.nodes import cad

    target = catalog(body_id="B1", moniker="target", faces=["F1", "F2", "F3"])
    calls = []

    class Builder:
        def __init__(self, **kwargs):
            pass

        def label_faces(self, **kwargs):
            calls.append(kwargs)
            return {
                "groups": [
                    {"name": "inlet", "role": "inlet", "member_monikers": ["m1"]},
                    {"name": "walls", "role": "wall", "member_monikers": ["m2", "m3"]},
                ],
                "coverage": 3,
                "total_faces": 3,
            }

    monkeypatch.setattr(cad, "SpaceClaimBuildAdapter", Builder)
    (tmp_path / "state").mkdir()
    result = cad.label_faces(
        {
            "runtime_dir": str(tmp_path),
            "run_dir": str(tmp_path),
            "ui_mode": "hidden",
            "target_catalog": target.model_dump(mode="json"),
            "target_body": {"body_id": "B1"},
            "boundary_group_plan": {
                "groups": [
                    {"name": "inlet", "role": "inlet", "face_ids": ["F1"]},
                    {"name": "walls", "role": "wall", "face_ids": ["F2", "F3"]},
                ]
            },
            "working_geometry": str(tmp_path / "target-fluid.scdoc"),
            "artifacts": {},
        }
    )

    assert result["error"] == ""
    assert calls[0]["groups"][1]["face_ids"] == ["F2", "F3"]
    assert result["boundary_roles"] == {"inlet": "inlet", "walls": "wall"}


# Clarification, reselection and downstream invalidation


def test_reviewer_cad_reselection_requires_no_object_substitution_parameters():
    with pytest.raises(ValidationError):
        RepairDecision(
            diagnosis="reference",
            evidence="error",
            action="reselect_cad",
            target_step="verify_selection",
            parameters={"reference_view": "Isometric"},
        )
    decision = RepairDecision(
        diagnosis="reference",
        evidence="error",
        action="reselect_cad",
        target_step="verify_selection",
        parameters={},
    )
    assert decision.parameters == {}


@pytest.mark.parametrize("resume_step", ["understand_prompt", "select_fluid_body", "plan_boundary_groups"])
def test_clarification_returns_to_its_recorded_selection_stage(monkeypatch, resume_step):
    monkeypatch.setattr(
        confirmation,
        "interrupt",
        lambda request: {"action": "clarify", "clarification": "the intended target is explicit"},
    )

    result = confirmation.human_intervention(
        {
            "prompt": "original request",
            "runtime_dir": "runtime",
            "extraction": {"candidate_geometry": "candidates.scdoc"},
            "repair_rounds": 2,
            "human_request": {"kind": "clarification", "resume_step": resume_step},
        }
    )

    assert result.goto == resume_step
    assert result.update["prompt"].endswith("USER CLARIFICATION:\nthe intended target is explicit")


def test_cad_reselection_clears_all_downstream_selection_state(tmp_path):
    decision = RepairDecision(
        action="reselect_cad",
        target_step="extract_volume",
        diagnosis="different extraction objects are needed",
        evidence="native extraction rejected the current method",
    )

    outcome = reviewer.execute_repair(
        {
            "runtime_dir": str(tmp_path),
            "failed_step": "extract_volume",
            "repair_decision": decision.model_dump(mode="json"),
            "extraction": {"old": True},
            "extraction_catalog": {"old": True},
            "target_body": {"old": True},
            "target_catalog": {"old": True},
            "boundary_group_plan": {"old": True},
            "labeling": {"old": True},
            "mesh_requirements": {"old": True},
            "parsed_mesh_requirements": {"old": True},
        }
    )

    assert outcome.goto == "understand_prompt"
    assert outcome.update["working_geometry"].endswith("original.scdoc")
    assert all(
        outcome.update[name] == {}
        for name in (
            "selection_plan",
            "extraction",
            "extraction_catalog",
            "target_body",
            "target_catalog",
            "boundary_group_plan",
            "labeling",
            "mesh_requirements",
            "parsed_mesh_requirements",
        )
    )


@pytest.mark.parametrize("stage,source,preserved", [
    ("extract_volume", "original.scdoc", "selection_plan"),
    ("select_fluid_body", "candidates.scdoc", "extraction_catalog"),
    ("plan_boundary_groups", "target-fluid.scdoc", "target_catalog"),
    ("label_faces", "target-fluid.scdoc", "boundary_group_plan"),
])
def test_restart_keeps_inputs_and_failure_but_invalidates_downstream(tmp_path, stage, source, preserved):
    state = {
        "runtime_dir": str(tmp_path), "working_geometry": "labeled.scdoc",
        "selection_plan": {"intent": "extract"},
        "extraction": {"candidate_geometry": str(tmp_path / "candidates.scdoc")},
        "extraction_catalog": {"catalog_id": "candidates"},
        "target_catalog": {"catalog_id": "target"},
        "boundary_group_plan": {"groups": ["old"]},
        "labeling": {"old": True}, "boundary_roles": {"old": "wall"},
        "cad_validation": {"positive_volume": True}, "confirmed_geometry": "confirmed.scdoc",
        "fluent_job": {"old": True}, "result": {"status": "success"},
        "error": "native failure", "failed_step": stage,
    }
    before = deepcopy(state)
    update = cad_restart_update(state, stage)
    merged = {**state, **update}
    assert state == before
    assert merged[preserved] == state[preserved]
    assert Path(merged["working_geometry"]) == tmp_path / source
    for field in ("labeling", "boundary_roles", "cad_validation", "fluent_job", "result"):
        assert merged[field] == {}
    assert merged["confirmed_geometry"] == ""
    assert merged["error"] == "native failure"
    assert merged["failed_step"] == stage


def test_clarification_cannot_redirect_to_arbitrary_node(monkeypatch):
    monkeypatch.setattr(confirmation, "interrupt", lambda _: {"action": "clarify", "clarification": "details"})
    with pytest.raises(ValueError, match="Unsupported clarification resume step"):
        confirmation.human_intervention({
            "prompt": "request",
            "human_request": {"kind": "clarification", "resume_step": "launch_fluent"},
        })
