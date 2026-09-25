"""Meshing parameters, unit conversion, native controls and repair execution."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.boundaries import build_fluent_job, rebind_mesh_targets
from src.services.contracts import (
    MeshRequirements,
    NumericControl,
)
from src.services.geometry_catalog import GeometryCatalog
from src.services.selection import extract_mesh_requirements
from src.services.units import convert_length
from src.workers.fluent.job import MeshJob
from src.workers.fluent.meshing import WatertightMeshingRunner
from src.workers.fluent.repair import RepairState


def test_unspecified_meshing_values_remain_native_defaults(tmp_path: Path):
    geometry = tmp_path / "confirmed.scdoc"
    geometry.write_bytes(b"placeholder")
    job_data = build_fluent_job(
        geometry=str(geometry),
        roles={"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
        requirements={
            "length_unit": None,
            "global_size": None,
            "local_refinements": [],
            "boundary_layers": None,
        },
    )
    job = MeshJob.from_dict(job_data)
    assert job.length_unit is None
    assert job.global_size is None
    assert job.boundary_layers["layers"] is None


ROLES = {"feed": "inlet", "exit": "outlet", "wall_a": "wall", "wall_b": "wall"}


def length(value, unit="mm"):
    return {"value": value, "unit": unit, "source": "user", "basis": "explicit test input"}


def job_at(tmp_path, **requirements):
    geometry = tmp_path / "test.scdoc"
    geometry.write_bytes(b"unit-test placeholder, not a real CAD")
    return MeshJob.from_dict(
        build_fluent_job(geometry=str(geometry), roles=ROLES, requirements=requirements)
    )


@pytest.mark.parametrize(
    "source,value,expected",
    [("mm", 20, 0.02), ("cm", 20, 0.2), ("in", 1, 0.0254), ("ft", 1, 0.3048), ("m", 2, 2)],
)
def test_conversion(source, value, expected):
    assert convert_length(value, source, "m") == pytest.approx(expected)


def test_all_lengths_convert_once_and_preserve_input(tmp_path):
    job = job_at(
        tmp_path,
        global_size=length(2, "cm"),
        local_refinements=[{"target": "feed", "boundary_name": "feed", "size": length(5)}],
        boundary_layers={"target": "wall_a", "layers": 3, "first_layer_height": length(0.1)},
    )
    controls = RepairState(job)
    assert controls.global_size == pytest.approx(0.02)
    controls.resolve_units("mm")
    controls.resolve_units("mm")
    assert controls.global_size == pytest.approx(20)
    assert controls.local_refinements[0]["size"] == pytest.approx(5)
    assert controls.boundary_layers["first_layer_height"] == pytest.approx(0.1)
    assert job.raw["parameter_sources"]["global_size"]["unit"] == "cm"


def test_layer_scope_does_not_expand_to_all_walls(tmp_path):
    job = job_at(tmp_path, boundary_layers={"target": "wall_a", "layers": 3})
    assert job.boundary_layers["zones"] == ["wall_a"]


@pytest.mark.parametrize("target", ["missing", "feed", "the wall somewhere"])
def test_unknown_or_nonwall_layer_target_is_not_replaced(tmp_path, target):
    job = job_at(tmp_path, boundary_layers={"target": target, "layers": 3})
    runner, tasks = runner_at(tmp_path, job)
    runner.execute_step("boundary_layers")
    assert tasks["boundary_layers"].complete_bl_label_list.get_state() == [target]


class Value:
    def __init__(self, value):
        self.value = value
        self.writes = []

    def get_state(self):
        return self.value

    def set_state(self, value):
        self.writes.append(value)
        self.value = value


class Task:
    def __init__(self, **values):
        for key, value in values.items():
            object.__setattr__(self, key, value if isinstance(value, Task) else Value(value))
        object.__setattr__(self, "calls", 0)

    def __setattr__(self, key, value):
        current = self.__dict__.get(key)
        if isinstance(current, Value):
            current.set_state(value)
        else:
            object.__setattr__(self, key, value)

    def __call__(self):
        self.calls += 1
        return True

    def add_child_and_update(self, **kwargs):
        return self()

    def state(self):
        return "Up-to-date"

    def errors(self):
        return []

    def warnings(self):
        return []

    def arguments(self):
        return {}

    def revert(self):
        return False


def runner_at(tmp_path, job):
    surface = Task(cfd_surface_mesh_controls=Task(max_size=50.0, min_size=2.0, growth_rate=1.1))
    boundary = Task(
        add_child="yes",
        number_of_layers=3,
        rate=1.2,
        first_height=0.01,
        offset_method_type="smooth-transition",
        zone_selection_list=[],
        complete_bl_label_list=[],
        bl_label_list=[],
        face_scope=Task(grow_on="only-walls", regions_type="fluid-regions"),
    )
    volume = Task(
        volume_fill="tet",
        prism_layers=True,
        volume_fill_controls=Task(growth_rate=1.3, hex_max_cell_length=50.0),
    )
    tasks = {
        "import_geometry": Task(length_unit="mm", file_name=""),
        "surface_mesh": surface,
        "boundary_layers": boundary,
        "volume_mesh": volume,
    }
    runner = WatertightMeshingRunner(
        SimpleNamespace(watertight=lambda: SimpleNamespace()),
        job,
        RepairState(job),
        job.geometry_path,
        tmp_path / "mesh.msh.h5",
        tmp_path / "fluent.trn",
        lambda *args: None,
    )
    runner._task_for_step = tasks.get
    return runner, tasks


def test_omitted_layers_execute_and_report_native_defaults(tmp_path):
    runner, tasks = runner_at(tmp_path, job_at(tmp_path))
    runner._step_import_geometry()
    result = runner._step_boundary_layers()
    runner._step_volume_mesh()
    assert not result.get("skipped")
    assert tasks["boundary_layers"].calls == 1
    assert tasks["boundary_layers"].number_of_layers.writes == []
    assert tasks["volume_mesh"].prism_layers.writes == []
    assert runner.parameter_record["effective"]["boundary_layers.layers"] == {
        "value": 3,
        "source": "native_default",
    }


def test_explicit_disable_does_not_mean_omitted(tmp_path):
    runner, tasks = runner_at(
        tmp_path, job_at(tmp_path, boundary_layers={"layers": 0, "layers_source": "user"})
    )
    assert runner._step_boundary_layers()["skipped"]
    runner._step_volume_mesh()
    assert tasks["boundary_layers"].calls == 0
    assert tasks["volume_mesh"].prism_layers.get_state() is False


def test_requested_scope_and_growth_do_not_leak(tmp_path):
    job = job_at(
        tmp_path,
        boundary_layers={
            "target": "wall_a",
            "layers": 3,
            "growth_rate": 1.6,
            "growth_rate_source": "user",
        },
    )
    runner, tasks = runner_at(tmp_path, job)
    runner._step_surface_mesh()
    runner._step_boundary_layers()
    runner._step_volume_mesh()
    assert tasks["boundary_layers"].face_scope.grow_on.get_state() == "selected-labels"
    assert tasks["boundary_layers"].complete_bl_label_list.get_state() == ["wall_a"]
    assert tasks["boundary_layers"].bl_label_list.get_state() == ["wall_a"]
    assert not tasks["boundary_layers"].zone_selection_list.writes
    assert tasks["boundary_layers"].rate.get_state() == 1.6
    assert tasks["surface_mesh"].cfd_surface_mesh_controls.growth_rate.writes == []
    assert tasks["volume_mesh"].volume_fill_controls.growth_rate.writes == []


def test_revert_false_is_not_reported_as_true(tmp_path):
    runner, _ = runner_at(tmp_path, job_at(tmp_path))
    runner.transcript_started = True
    result = runner.revert_from("surface_mesh")
    assert result["reverted"] is False
    assert result["result"] is False
    assert result["state_before"] == result["state_after"] == "Up-to-date"


def test_renamed_group_rebinds_by_exact_native_membership():
    catalog = GeometryCatalog(
        catalog_id="new",
        geometry_id="input",
        faces=[
            {"id": "F2", "kind": "face", "moniker": "native-a"},
            {"id": "F3", "kind": "face", "moniker": "native-b"},
        ],
        native_catalog={
            "internal": {
                "raw_groups": [
                    {"raw_name": "renamed", "member_ids": ["F2"]},
                    {"raw_name": "other", "member_ids": ["F3"]},
                ]
            }
        },
    )
    source = {
        "local_refinements": [{"target": "old", "boundary_name": "old", "size": length(1)}],
        "boundary_layers": {"target": "old", "layers": 3},
    }
    result = rebind_mesh_targets(
        requirements=source,
        previous_groups=[{"name": "old", "member_monikers": ["native-a"]}],
        confirmed_catalog=catalog,
        roles={"renamed": "wall", "other": "wall"},
    )
    assert result["local_refinements"][0]["boundary_name"] == "renamed"
    assert result["boundary_layers"]["boundary_names"] == ["renamed"]
    assert source["local_refinements"][0]["boundary_name"] == "old"
    with pytest.raises(ValueError, match="Cannot uniquely bind"):
        rebind_mesh_targets(
            requirements=source,
            previous_groups=[],
            confirmed_catalog=catalog,
            roles={"renamed": "wall", "other": "wall"},
        )


def test_inferred_disable_is_not_an_explicit_user_request():
    from src.services.contracts import BoundaryLayerRequest

    with pytest.raises(ValueError, match="explicit user request"):
        BoundaryLayerRequest(layers=0, layers_source="inferred")


def test_nested_controls_use_workflow_assignment_not_transient_command():
    from src.workers.fluent.meshing import set_task_value

    class NativeProxy:
        def __init__(self):
            object.__setattr__(self, "written", {})

        def __getattr__(self, name):
            return Value("transient")

        def __setattr__(self, name, value):
            self.written[name] = value

    proxy = NativeProxy()
    set_task_value(proxy, "grow_on", "selected-labels")
    assert proxy.written == {"grow_on": "selected-labels"}


def test_unresolved_and_empty_layer_scopes_reach_native_interface(tmp_path):
    catalog = GeometryCatalog(
        catalog_id="new", geometry_id="input", native_catalog={"internal": {"raw_groups": []}}
    )
    for layers in ({"target": "unresolved", "layers": 3}, {"boundary_names": [], "layers": 3}):
        requirements = rebind_mesh_targets(
            requirements={"boundary_layers": layers},
            previous_groups=[],
            confirmed_catalog=catalog,
            roles=ROLES,
        )
        runner, tasks = runner_at(tmp_path, job_at(tmp_path, **requirements))
        runner.execute_step("boundary_layers")
        expected = ["unresolved"] if "target" in layers else []
        assert tasks["boundary_layers"].complete_bl_label_list.get_state() == expected
        assert tasks["boundary_layers"].face_scope.grow_on.get_state() == "selected-labels"


@pytest.mark.parametrize("value", [-2, 0, float("nan"), float("inf")])
def test_invalid_normalized_lengths_are_rejected(value):
    from src.services.contracts import MeshRequirements

    with pytest.raises(ValueError):
        MeshRequirements(global_size=length(value, "m"))


def test_normalized_lengths_reach_native_interface_and_repair_controls(tmp_path):
    from src.services.contracts import MeshRequirements

    requirements = MeshRequirements.model_validate(
        {
            "global_size": length(0.002, "m"),
            "local_refinements": [{"target": "feed", "boundary_name": "feed", "size": length(0.001, "m")}],
            "boundary_layers": {
                "layers": 80,
                "layers_source": "inferred",
                "growth_rate": 0.2,
                "growth_rate_source": "inferred",
                "first_layer_height": length(0.0005, "m"),
            },
        }
    ).model_dump()
    runner, tasks = runner_at(tmp_path, job_at(tmp_path, **requirements))
    runner.execute_step("import_geometry")
    runner.execute_step("surface_mesh")
    runner.execute_step("boundary_layers")
    assert tasks["surface_mesh"].cfd_surface_mesh_controls.max_size.get_state() == 2
    assert runner.repair_state.local_refinements[0]["size"] == 1
    assert tasks["boundary_layers"].number_of_layers.get_state() == 80
    assert tasks["boundary_layers"].rate.get_state() == 0.2
    assert tasks["boundary_layers"].first_height.get_state() == 0.5
    controls = runner.repair_state
    for action, parameters in (
        ("set_global_size", {"value": -3}),
        ("set_local_size", {"zone": "feed", "value": -4}),
        ("set_growth_rate", {"value": 0.3}),
        ("set_layer_count", {"value": -2}),
        ("set_first_layer_height", {"value": -0.7}),
    ):
        controls.apply(action, parameters, {"feed"})
    runner.execute_step("surface_mesh")
    runner.execute_step("boundary_layers")
    assert tasks["surface_mesh"].cfd_surface_mesh_controls.max_size.get_state() == -3
    assert controls.local_refinements[0]["size"] == -4
    assert tasks["boundary_layers"].number_of_layers.get_state() == -2
    assert tasks["boundary_layers"].rate.get_state() == 0.3
    assert tasks["boundary_layers"].first_height.get_state() == -0.7


@pytest.mark.parametrize("scenario", ["inferred", "user", "scope", "height"])
def test_native_rejection_requires_approval_for_user_parameters_and_mappings(
    tmp_path, monkeypatch, scenario
):
    """Use the real graph/repair worker with native and model doubles, no Ansys calls."""
    import json

    from src import api
    from src.adapters.fluent import FluentWorkerError
    from src.graph import build_graph
    from src.nodes import fluent, results
    from src.services import reviewer
    from src.services.contracts import RepairDecision
    from src.workers.fluent.meshing import StepExecutionError
    from src.workers.fluent.session import FluentWorker

    source = "inferred" if scenario == "inferred" else "user"
    layers = {"layers": 80, "layers_source": source, "target": "wall_a"}
    if scenario == "scope":
        layers.update(layers=3, target="unknown")
    if scenario == "height":
        layers.update(layers=3, first_layer_height=length(-1))
    job = job_at(tmp_path, boundary_layers=layers)
    runner, tasks = runner_at(tmp_path, job)

    class NativeLayerTask(Task):
        def __call__(self):
            self.calls += 1
            if self.number_of_layers.get_state() > 50:
                raise ValueError("Native layer count rejected")
            if self.first_height.get_state() <= 0:
                raise ValueError("Native first height rejected")
            if self.complete_bl_label_list.get_state() == ["unknown"]:
                raise ValueError("Native layer label rejected")
            return True

    tasks["boundary_layers"].__class__ = NativeLayerTask
    runner.execute_step("import_geometry")
    runner.transcript_started = True
    worker = FluentWorker(tmp_path)
    worker.runner, worker.controls, worker.job = runner, runner.repair_state, job
    worker.session = SimpleNamespace(is_server_healthy=lambda: True)
    calls = []

    class Client:
        def call(self, operation, data=None):
            calls.append((operation, data))
            if operation == "picture":
                raise RuntimeError("Offline test has no picture")
            try:
                return worker.dispatch(operation, data or {})
            except StepExecutionError as error:
                raise FluentWorkerError(str(error), error.observation) from error

    class Model:
        def invoke(self, **kwargs):
            evidence = json.loads(kwargs["user_prompt"])
            assert evidence["error_evidence"]["controls"]
            action, value = "set_layer_count", 3
            if scenario == "scope":
                action, value = "set_layer_targets", ["feed"]
            elif scenario == "height":
                action, value = "set_first_layer_height", 0.2
            return RepairDecision(
                action=action,
                target_step="boundary_layers",
                diagnosis="Correct native rejection",
                evidence="native error",
                parameters={"zones" if scenario == "scope" else "value": value},
            )

    client = Client()
    monkeypatch.setattr(fluent, "get_client", lambda *args: client)
    monkeypatch.setattr(reviewer, "get_client", lambda *args: client)
    monkeypatch.setattr(reviewer.GroundingLLMClient, "from_codex_oauth", lambda **kwargs: Model())
    monkeypatch.setattr(api, "has_live_client", lambda run_id: True)
    monkeypatch.setattr(fluent, "validate_mesh", lambda state: {"error": ""})
    monkeypatch.setattr(results, "completed", lambda state: {"status": "success"})
    closed = []
    monkeypatch.setattr(reviewer, "close_client", closed.append)
    checkpoint = tmp_path / "checkpoints.sqlite"
    metadata = {
        "run_id": "numeric",
        "checkpoint": str(checkpoint),
        "max_repair_rounds": 4,
        "max_total_repair_rounds": 100,
    }
    (tmp_path / "run-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    graph = build_graph(checkpoint)
    config = {"configurable": {"thread_id": "numeric"}}
    graph.update_state(
        config,
        {
            **metadata,
            "run_dir": str(tmp_path),
            "runtime_dir": str(tmp_path),
            "working_geometry": str(job.geometry_path),
            "mesh_requirements": job.raw["parameter_sources"],
            "fluent_job": job.raw,
            "fluent_steps": {"launch": {"existing": True}},
            "repair_rounds": 0,
            "repair_history": [],
            "error": "",
        },
        as_node="update_regions",
    )
    result = graph.invoke(None, config)
    if scenario == "inferred":
        assert result["status"] == "success"
        assert not result.get("__interrupt__")
    else:
        pause = result["__interrupt__"][0].value
        assert pause["kind"] == ("boundary_mapping" if scenario == "scope" else "parameter_change")
        assert not any(operation == "repair" for operation, _ in calls)
        kwargs = (
            {"boundary_replacements": ["feed"]}
            if scenario == "scope"
            else {}
        )
        assert closed == []
        assert graph.get_state(config).values["fluent_steps"]["launch"] == {"existing": True}
        result = api.resume_pipeline(run_dir=tmp_path, action="approve", **kwargs)
        assert result["status"] == "success"
        snapshot = graph.get_state(config).values
        assert snapshot["repair_history"][-1]["application"]["controls"]
        repair_requests = [data for operation, data in calls if operation == "repair"]
        assert repair_requests[-1]["manual_approved"] is True
        assert not any(operation in {"initialize", "launch"} for operation, _ in calls)
        assert closed == []
    assert job.raw["parameter_sources"]["boundary_layers"]["layers"] == layers["layers"]
    if scenario == "scope":
        assert tasks["boundary_layers"].complete_bl_label_list.get_state() == ["feed"]
    if scenario == "height":
        assert tasks["boundary_layers"].first_height.get_state() == 0.2
        assert (
            runner.parameter_record["effective"]["boundary_layers.first_height"]["source"]
            == "runtime_repair"
        )


def test_native_setter_failure_retains_attempted_controls(tmp_path):
    from src.workers.fluent.meshing import StepExecutionError

    runner, tasks = runner_at(tmp_path, job_at(tmp_path, global_size=length(-1)))

    class NativeControls(Task):
        def __setattr__(self, name, value):
            if name == "max_size":
                raise ValueError("Native max_size rejected")
            super().__setattr__(name, value)

    tasks["surface_mesh"].cfd_surface_mesh_controls.__class__ = NativeControls
    runner.execute_step("import_geometry")
    with pytest.raises(StepExecutionError, match="Native max_size rejected") as error:
        runner.execute_step("surface_mesh")
    assert error.value.observation["controls"]["global_size"] == -1


def test_local_reference_repair_and_each_user_size_change_require_approval(
    tmp_path, monkeypatch
):
    from src.services import reviewer
    from src.services.contracts import RepairDecision

    original_size = {**length(0.5, "cm"), "source": "user"}
    job = job_at(
        tmp_path,
        local_refinements=[{"target": "feed", "boundary_name": "old", "size": original_size}],
    )
    controls = RepairState(job)
    controls.resolve_units("mm")
    operations = []

    class Client:
        def call(self, operation, data=None):
            operations.append(operation)
            if operation == "observe":
                return {"controls": controls.snapshot()}
            step, _ = controls.apply(
                data["action"],
                data["parameters"],
                {"middle", "feed"},
                manual_approved=bool(data.get("manual_approved")),
            )
            return {"resume": step, "controls": controls.snapshot()}

    monkeypatch.setattr(reviewer, "get_client", lambda *args: Client())
    state = {
        "run_id": "local-size",
        "run_dir": str(tmp_path),
        "working_geometry": str(job.geometry_path),
        "failed_step": "local_sizing",
        "runtime_dir": str(tmp_path),
        "mesh_requirements": job.raw["parameter_sources"],
        "repair_history": [],
        "error": "native rejection",
    }

    def propose(action, **parameters):
        decision = RepairDecision(
            action=action,
            target_step="local_sizing",
            diagnosis="repair",
            evidence="offline",
            parameters=parameters,
        ).model_dump(mode="json")
        state["repair_decision"] = decision
        state["repair_history"].append({"decision": decision})
        outcome = reviewer.execute_repair(state)
        state.update(outcome.update)
        return outcome

    outcome = propose("replace_zone_reference", category="local_refinements", old="old", new="feed")
    assert outcome.goto == "human_intervention"
    state["repair_approved"] = True
    outcome = reviewer.execute_repair(state)
    state.update(outcome.update)
    assert outcome.goto == "local_sizing"
    assert controls.local_refinements[0]["source_boundary_name"] == "old"
    operations.clear()
    outcome = propose("set_local_size", zone="feed", value=10)
    assert outcome.goto == "human_intervention"
    evidence = outcome.update["human_request"]["evidence"]
    assert evidence["requested_value"] == 0.5
    assert evidence["requested_unit"] == "cm"
    assert evidence["current_value"] == 5
    assert evidence["unit"] == "mm"
    assert evidence["target"] == "feed"
    assert evidence["proposed_value"] == 10
    assert operations == []
    state["repair_approved"] = True
    outcome = reviewer.execute_repair(state)
    state.update(outcome.update)
    assert outcome.goto == "local_sizing"
    assert controls.local_refinements[0]["size"] == 10
    assert operations == ["repair"]
    assert job.raw["parameter_sources"]["local_refinements"][0]["size"] == original_size
    operations.clear()
    outcome = propose("set_local_size", zone="feed", value=12)
    assert outcome.goto == "human_intervention"
    assert outcome.update["human_request"]["evidence"]["current_value"] == 10
    assert operations == []


def test_first_failed_local_sizing_child_is_not_hidden_by_a_later_child(tmp_path):
    from src.workers.fluent.meshing import StepExecutionError

    job = job_at(
        tmp_path,
        local_refinements=[
            {"target": "feed", "boundary_name": "feed", "size": length(1)},
            {"target": "exit", "boundary_name": "exit", "size": length(1)},
        ],
    )
    runner, _ = runner_at(tmp_path, job)

    class FailedLocal(Task):
        def __call__(self):
            self.calls += 1
            return False

    local_values = {
        "boi_execution": "Face Size",
        "boi_size": 1.0,
        "boi_min_size": 0.5,
        "boi_max_size": 1.0,
        "boi_zoneor_label": "label",
        "boi_face_label_list": [],
        "draw_size_control": False,
        "boi_growth_rate": 1.2,
    }
    first = FailedLocal(**local_values)
    second = Task(**local_values)
    runner._task_for_step = lambda step: Task(**local_values) if step == "local_sizing" else None
    runner._tasks_with_display_prefix = lambda prefix: [first, second]

    with pytest.raises(StepExecutionError, match="local sizing did not complete"):
        runner._step_local_sizing()
    assert first.calls == 1
    assert second.calls == 0


def test_final_boundary_check_requires_actual_names_and_types(tmp_path):
    from src.workers.fluent.meshing import boundary_check

    job = job_at(tmp_path)
    current = {"inlet": ["feed"], "outlet": ["exit"], "wall": ["wall_a"], "symmetry": []}
    result = boundary_check(
        "",
        job,
        current,
        actual_boundaries={"feed", "exit", "wall_a"},
        actual_boundary_types={"feed": "wall", "exit": "pressure-outlet", "wall_a": "wall"},
    )
    assert not result["ok"]
    assert result["type_mismatches"] == [
        {"name": "feed", "expected": "velocity-inlet", "actual": "wall"}
    ]


def test_model_converted_length_reaches_job_without_changing_import_unit(monkeypatch, tmp_path):
    requests = []
    requirements = MeshRequirements(global_size=NumericControl(
        value=0.00002, unit="m", source="user",
        original_expression="20 micrometres", basis="20 micrometres = 0.00002 m",
    ))

    def invoke(**kwargs):
        requests.append(kwargs)
        return requirements

    monkeypatch.setattr(
        "src.services.selection.GroundingLLMClient.from_runtime_config",
        lambda **kwargs: SimpleNamespace(invoke=invoke),
    )
    parsed = extract_mesh_requirements(
        catalog=GeometryCatalog(catalog_id="c", geometry_id="g"),
        user_prompt="Use 20 micrometres", boundary_names=[],
        audit_dir=tmp_path,
    )
    geometry = tmp_path / "confirmed.scdoc"
    geometry.write_bytes(b"fixture")
    job = build_fluent_job(
        geometry=str(geometry), roles={"feed": "inlet", "exit": "outlet", "wall": "wall"},
        requirements=parsed.model_dump(),
    )
    assert job["global_size"] == pytest.approx(0.00002)
    assert job["length_unit"] is None
    assert parsed.global_size.original_expression == "20 micrometres"
    assert "convert" in requests[0]["system_prompt"]
    assert "missing_information" in requests[0]["system_prompt"]
