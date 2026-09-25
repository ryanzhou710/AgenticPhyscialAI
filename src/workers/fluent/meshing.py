"""Observable Fluent 2024 R1 Watertight Geometry workflow executor."""

from __future__ import annotations

import ast
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.services.artifacts import write_json
from src.services.units import METRES_PER_UNIT

from .job import MeshJob
from .repair import STEP_ORDER, RepairState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_task_value(task: Any, name: str, value: Any) -> None:
    # Assignment goes through PyFluent's workflow wrapper and persists Task
    # Arguments. Calling set_state on a nested command field only changes the
    # transient command object, which can be lost when the task is executed.
    getattr(task, name)  # Reject a missing field before assigning.
    setattr(task, name, value)


def _safe_call(value: Any, default: Any = None) -> Any:
    try:
        return value() if callable(value) else value
    except Exception:
        return default


def _as_messages(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _names_from_value(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return [value] if value.strip() else []
        if isinstance(parsed, (list, tuple, set)):
            return [str(item) for item in parsed if str(item).strip()]
        return [str(parsed)] if str(parsed).strip() else []
    return []


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<truncated>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in list(value)[:100]]
    return str(value)


class StepExecutionError(RuntimeError):
    def __init__(self, step: str, message: str, observation: dict[str, Any]):
        super().__init__(message)
        self.step = step
        self.observation = observation


def _workflow_task(watertight: Any, *names: str) -> Any:
    for name in names:
        task = getattr(watertight, name, None)
        if task is not None:
            return task
    raise RuntimeError(f"Fluent Watertight workflow is missing a task: {' / '.join(names)}")


def start_transcript(session: Any, path: Path) -> bool:
    transcript = getattr(session, "transcript", None)
    if transcript is None or not hasattr(transcript, "start"):
        return False
    try:
        transcript.start(file_name=str(path))
        return True
    except TypeError:
        try:
            transcript.start(str(path))
            return True
        except Exception:
            return False
    except Exception:
        return False


def stop_transcript(session: Any) -> None:
    transcript = getattr(session, "transcript", None)
    if transcript is not None and hasattr(transcript, "stop"):
        try:
            transcript.stop()
        except Exception:
            pass


class WatertightMeshingRunner:
    """Execute and observe the eight user-visible meshing steps in one session."""

    def __init__(
        self,
        session: Any,
        job: MeshJob,
        repair_state: RepairState,
        runtime_input: Path,
        runtime_output: Path,
        transcript_path: Path,
        event_callback: Callable[[str, str], None],
    ):
        self.session = session
        self.job = job
        self.repair_state = repair_state
        self.runtime_input = runtime_input
        self.runtime_output = runtime_output
        self.transcript_path = transcript_path
        self.event_callback = event_callback
        self.transcript_started = start_transcript(session, transcript_path)
        self.transcript_paths = [transcript_path]
        self._transcript_index = 0
        self.watertight = session.watertight()
        self.step_attempts: dict[str, int] = {step: 0 for step in STEP_ORDER}
        self.last_observations: dict[str, dict[str, Any]] = {}
        self.parameter_record = {
            "requested": copy.deepcopy(job.raw.get("parameter_sources", {})),
            "effective": {},
            "history": [],
        }
        self.repaired_sources: dict[str, str] = {}

    def _capture(
        self,
        key: str,
        field: Any,
        source: str = "native_default",
        *,
        length: bool = False,
        basis: str | None = None,
    ) -> Any:
        value = field.get_state()
        row = {"value": _json_safe(value), "source": source}
        if length:
            row["unit"] = self.repair_state.unit
        if basis:
            row["basis"] = basis
        self.parameter_record["effective"][key] = row
        self.parameter_record["history"].append({"parameter": key, **row})
        write_json(self.runtime_output.parent / "execution-parameters.json", self.parameter_record)
        return value

    def _source(self, key: str, control: dict | None = None) -> str:
        if key in self.repaired_sources:
            return "runtime_repair"
        origin = (control or {}).get("source")
        return (
            "llm_inferred"
            if origin == "inferred"
            else "user"
            if origin == "user"
            else "native_default"
        )

    def record_repair(self, action: str, parameters: dict) -> None:
        key = {
            "set_global_size": "global_size",
            "set_local_size": "local_refinements",
            "set_growth_rate": "boundary_layers.growth_rate",
            "set_layer_count": "boundary_layers.layers",
            "set_first_layer_height": "boundary_layers.first_layer_height",
            "set_layer_targets": "boundary_layers.zones",
        }.get(action)
        if key:
            self.repaired_sources[key] = action
        self.parameter_record["history"].append({"repair": action, "parameters": parameters})
        write_json(self.runtime_output.parent / "execution-parameters.json", self.parameter_record)

    def close_transcript(self) -> None:
        if self.transcript_started:
            stop_transcript(self.session)
            self.transcript_started = False
        if not self.transcript_started and not self.transcript_path.exists():
            self.transcript_path.write_text(
                "Fluent transcript service unavailable; structured events remain in the run state records.\n",
                encoding="utf-8",
            )

    def _task_for_step(self, step: str) -> Any | None:
        mapping = {
            "import_geometry": ("import_geometry",),
            "local_sizing": ("add_local_sizing", "add_local_sizing_wtm"),
            "surface_mesh": ("create_surface_mesh",),
            "describe_geometry": ("describe_geometry",),
            "update_boundaries": ("update_boundaries",),
            "update_regions": ("update_regions",),
            "boundary_layers": ("add_boundary_layer", "add_boundary_layers"),
            "volume_mesh": ("create_volume_mesh", "create_volume_mesh_wtm"),
        }
        names = mapping.get(step)
        if names is None:
            return None
        return _workflow_task(self.watertight, *names)

    def _tasks_with_display_prefix(self, prefix: str) -> list[Any]:
        """Find compound-control children that already exist in the workflow."""
        try:
            tasks = self.watertight.tasks()
        except Exception:
            return []
        matches: list[Any] = []
        for candidate in tasks:
            display_name = str(_safe_call(getattr(candidate, "display_name", None), "") or "")
            if display_name.casefold().startswith(prefix.casefold()):
                matches.append(candidate)
        return matches

    def workflow_snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            tasks = self.watertight.tasks()
        except Exception:
            return result
        for task in tasks:
            try:
                name = task.python_name()
            except Exception:
                name = str(task)
            result.append(
                {
                    "name": name,
                    "display_name": _safe_call(getattr(task, "display_name", None)),
                    "state": _safe_call(getattr(task, "state", None)),
                    "errors": _as_messages(_safe_call(getattr(task, "errors", None), [])),
                    "warnings": _as_messages(_safe_call(getattr(task, "warnings", None), [])),
                }
            )
        return result

    def observe(self, step: str, task: Any | None, result: Any = None) -> dict[str, Any]:
        observation = {
            "step": step,
            "at": utc_now(),
            "attempt": self.step_attempts[step],
            "result": _json_safe(result),
            "state": _safe_call(getattr(task, "state", None)) if task is not None else None,
            "errors": _as_messages(_safe_call(getattr(task, "errors", None), []))
            if task is not None
            else [],
            "warnings": _as_messages(_safe_call(getattr(task, "warnings", None), []))
            if task is not None
            else [],
            "arguments": _json_safe(_safe_call(getattr(task, "arguments", None), {}))
            if task is not None
            else {},
            "explicit_arguments": _json_safe(
                _safe_call(lambda: task.arguments.get_state(explicit_only=True), {})
            )
            if task is not None
            else {},
            "workflow": self.workflow_snapshot(),
            "effective_parameters": copy.deepcopy(self.parameter_record["effective"]),
        }
        self.last_observations[step] = observation
        return observation

    @staticmethod
    def _observation_failed(observation: dict[str, Any]) -> bool:
        if observation["result"] is False or observation["errors"]:
            return True
        state = str(observation.get("state") or "").casefold()
        return state != "up-to-date"

    def _run_callable_step(
        self, step: str, task: Any, operation: Callable[[], Any]
    ) -> dict[str, Any]:
        self.step_attempts[step] += 1
        try:
            result = operation()
            observation = self.observe(step, task, result)
        except Exception as error:
            observation = self.observe(step, task)
            observation["exception"] = {"type": type(error).__name__, "message": str(error)}
            raise StepExecutionError(
                step, f"{type(error).__name__}: {error}", observation
            ) from error
        if self._observation_failed(observation):
            raise StepExecutionError(step, f"Fluent task {step} did not complete.", observation)
        return observation

    def execute_step(self, step: str) -> dict[str, Any]:
        self.event_callback(step, f"Starting step {step}.")
        handler = getattr(self, f"_step_{step}")
        try:
            observation = handler()
        except Exception as error:
            controls = self.repair_state.snapshot()
            if isinstance(error, StepExecutionError):
                error.observation["controls"] = controls
                raise
            raise StepExecutionError(
                step,
                f"{type(error).__name__}: {error}",
                {
                    "step": step,
                    "controls": controls,
                    "parameter_sources": self.job.raw.get("parameter_sources", {}),
                    "exception": {"type": type(error).__name__, "message": str(error)},
                },
            ) from error
        self.event_callback(step, f"Step {step} completed.")
        return observation

    def _step_import_geometry(self) -> dict[str, Any]:
        task = self._task_for_step("import_geometry")
        set_task_value(task, "file_name", str(self.runtime_input))
        if self.job.length_unit is not None:
            set_task_value(task, "length_unit", self.job.length_unit)
        unit = task.length_unit.get_state()
        if unit not in METRES_PER_UNIT:
            raise ValueError("Fluent returned an unsupported length unit: " + str(unit))
        self.repair_state.resolve_units(unit)
        self._capture(
            "import_geometry.length_unit",
            task.length_unit,
            "user" if self.job.length_unit is not None else "native_default",
        )
        return self._run_callable_step("import_geometry", task, task)

    def _step_local_sizing(self) -> dict[str, Any]:
        task = self._task_for_step("local_sizing")
        controls = self.repair_state.snapshot()
        self.step_attempts["local_sizing"] += 1
        if not controls["local_refinements"]:
            observation = self.observe("local_sizing", task, "skipped-no-controls")
            observation["skipped"] = True
            return observation
        try:
            existing_children = self._tasks_with_display_prefix("local-size-")
            for index, refinement in enumerate(controls["local_refinements"], start=1):
                control = existing_children[index - 1] if index <= len(existing_children) else task
                if control is task:
                    control.add_child = "yes"
                    control.boi_control_name = (
                        f"local-size-{self.step_attempts['local_sizing']}-{index}"
                    )
                control.boi_execution = "Face Size"
                control.boi_size = refinement["size"]
                control.boi_min_size = refinement["size"] * 0.5
                control.boi_max_size = refinement["size"]
                control.boi_zoneor_label = "label"
                control.boi_face_label_list = [refinement["zone"]]
                control.draw_size_control = True
                requested = next(
                    (
                        row["size"]
                        for row in self.job.raw.get("parameter_sources", {}).get(
                            "local_refinements", []
                        )
                        if row.get("boundary_name")
                        == refinement.get("source_boundary_name", refinement["zone"])
                    ),
                    None,
                )
                self._capture(
                    f"local_sizing.{refinement['zone']}.size",
                    control.boi_size,
                    self._source("local_refinements", requested),
                    length=True,
                )
                self._capture(
                    f"local_sizing.{refinement['zone']}.min_size",
                    control.boi_min_size,
                    "project_derived",
                    length=True,
                    basis="half of requested face size",
                )
                self._capture(
                    f"local_sizing.{refinement['zone']}.growth_rate", control.boi_growth_rate
                )
                result = (
                    control()
                    if control is not task
                    else control.add_child_and_update(defer_update=False)
                )
                observation = self.observe("local_sizing", control, result)
                if self._observation_failed(observation):
                    raise StepExecutionError(
                        "local_sizing", "Fluent local sizing did not complete.", observation
                    )
        except Exception as error:
            observation = self.observe("local_sizing", locals().get("control", task))
            observation["exception"] = {"type": type(error).__name__, "message": str(error)}
            raise StepExecutionError(
                "local_sizing", f"{type(error).__name__}: {error}", observation
            ) from error
        if self._observation_failed(observation):
            raise StepExecutionError(
                "local_sizing", "Fluent local sizing did not complete.", observation
            )
        return observation

    def _step_surface_mesh(self) -> dict[str, Any]:
        controls = self.repair_state.snapshot()
        task = self._task_for_step("surface_mesh")
        if controls["global_size"] is not None:
            set_task_value(task.cfd_surface_mesh_controls, "max_size", controls["global_size"])
            set_task_value(
                task.cfd_surface_mesh_controls, "min_size", controls["global_size"] * 0.5
            )
        requested = self.job.raw.get("parameter_sources", {}).get("global_size")
        source = self._source("global_size", requested)
        self._capture(
            "surface_mesh.max_size", task.cfd_surface_mesh_controls.max_size, source, length=True
        )
        self._capture(
            "surface_mesh.min_size",
            task.cfd_surface_mesh_controls.min_size,
            "project_derived" if controls["global_size"] is not None else "native_default",
            length=True,
            basis="half of requested global size" if controls["global_size"] is not None else None,
        )
        self._capture("surface_mesh.growth_rate", task.cfd_surface_mesh_controls.growth_rate)
        if controls["quality_improvement"]:
            set_task_value(task.surface_mesh_preferences, "sm_quality_improve", "yes")
        return self._run_callable_step("surface_mesh", task, task)

    def _step_describe_geometry(self) -> dict[str, Any]:
        task = self._task_for_step("describe_geometry")

        def operation() -> Any:
            task.update_child_tasks(setup_type_changed=False)
            set_task_value(
                task, "setup_type", "The geometry consists of only fluid regions with no voids"
            )
            task.update_child_tasks(setup_type_changed=True)
            return task()

        return self._run_callable_step("describe_geometry", task, operation)

    def _step_update_boundaries(self) -> dict[str, Any]:
        task = self._task_for_step("update_boundaries")
        controls = self.repair_state.snapshot()
        names: list[str] = []
        types: list[str] = []
        for role, boundary_type in (
            ("inlet", "velocity-inlet"),
            ("outlet", "pressure-outlet"),
            ("wall", "wall"),
            ("symmetry", "symmetry"),
        ):
            for name in controls["boundaries"][role]:
                names.append(name)
                types.append(boundary_type)
        if names:
            set_task_value(task, "selection_type", "label")
            # The GUI's current-list may stay empty until this task first executes.
            # Surface meshing's original_zones are observed imported labels, not requested names.
            current = sorted(self.available_names())
            missing = sorted(set(names) - set(current))
            if missing:
                observation = self.observe("update_boundaries", task)
                observation["missing_boundary_references"] = missing
                observation["actual_boundaries"] = current
                raise StepExecutionError(
                    "update_boundaries", "Unknown boundary references: " + str(missing), observation
                )
            for field, value in (
                ("boundary_label_list", names),
                ("boundary_label_type_list", types),
            ):
                set_task_value(task, field, value)
        return self._run_callable_step("update_boundaries", task, task)

    def _step_update_regions(self) -> dict[str, Any]:
        task = self._task_for_step("update_regions")
        objects = self.session.scheme.eval("(tgapi-util-get-object-name-list-of-type 'mesh)")
        if not isinstance(objects, (list, tuple)) or len(objects) != 1:
            raise StepExecutionError(
                "update_regions", "Expected one imported fluid mesh object", {"objects": objects}
            )
        name = str(objects[0])
        regions = self.session.scheme.eval(
            "(tgapi-util-get-region-name-list-of-object " + json.dumps(name) + ")"
        )
        if not isinstance(regions, (list, tuple)) or len(regions) != 1:
            raise StepExecutionError(
                "update_regions",
                "Expected one closed fluid region",
                {"object": name, "regions": regions},
            )
        region = str(regions[0])
        volume = self.session.scheme.eval(
            "(tgapi-util-get-region-volume " + json.dumps(name) + " " + json.dumps(region) + ")"
        )
        if not isinstance(volume, (int, float)) or volume <= 0:
            raise StepExecutionError(
                "update_regions", "Region volume is missing or non-positive", {"volume": volume}
            )
        set_task_value(task, "mesh_object", name)
        set_task_value(task, "region_name_list", [region])
        set_task_value(task, "region_type_list", ["fluid"])
        observation = self._run_callable_step("update_regions", task, task)
        observation["region_check"] = {
            "object": name,
            "regions": [region],
            "assigned_type": "fluid",
            "volume_in_geometry_units": volume,
        }
        return observation

    def _step_boundary_layers(self) -> dict[str, Any]:
        task = self._task_for_step("boundary_layers")
        settings = self.repair_state.snapshot()["boundary_layers"]
        scoped = settings.get("scope_specified", bool(settings["zones"]))
        requested = self.job.raw.get("parameter_sources", {}).get("boundary_layers") or {}
        self.step_attempts["boundary_layers"] += 1
        if settings["layers"] == 0:
            task.add_child = "no"
            self._capture(
                "boundary_layers.add_child",
                task.add_child,
                self._source("boundary_layers.layers", {"source": requested.get("layers_source")}),
            )
            observation = self.observe(
                "boundary_layers",
                task,
                "disabled-by-repair"
                if "boundary_layers.layers" in self.repaired_sources
                else "disabled-by-user",
            )
            observation["skipped"] = True
            return observation
        try:
            existing_children = self._tasks_with_display_prefix("boundary-layer-")
            control = existing_children[0] if existing_children else task
            has_request = (
                any(
                    settings.get(key) is not None
                    for key in ("layers", "growth_rate", "first_layer_height")
                )
                or scoped
            )
            if not existing_children:
                if has_request:
                    control.add_child = "yes"
                native_add = self._capture(
                    "boundary_layers.add_child",
                    control.add_child,
                    "project_derived" if has_request else "native_default",
                    basis="enable the requested layer controls" if has_request else None,
                )
                if native_add == "no":
                    observation = self.observe(
                        "boundary_layers", task, "disabled-by-native-default"
                    )
                    observation["skipped"] = True
                    return observation
                control.bl_control_name = f"boundary-layer-{self.step_attempts['boundary_layers']}"
            if scoped:
                set_task_value(control.face_scope, "grow_on", "selected-labels")
                # Labels use CompleteBlLabelList. ZoneSelectionList is a
                # different GUI control and must not receive label names.
                control.bl_label_list = settings["zones"]
                control.complete_bl_label_list = settings["zones"]
            if settings["layers"] is not None:
                control.number_of_layers = settings["layers"]
            if settings["growth_rate"] is not None:
                control.rate = settings["growth_rate"]
            if settings["first_layer_height"] is not None:
                control.offset_method_type = "uniform"
                control.first_height = settings["first_layer_height"]
            self._capture(
                "boundary_layers.grow_on",
                control.face_scope.grow_on,
                "project_derived" if scoped else "native_default",
                basis="submit requested scope to native software" if scoped else None,
            )
            self._capture(
                "boundary_layers.zones",
                control.complete_bl_label_list,
                "runtime_repair"
                if "boundary_layers.zones" in self.repaired_sources
                else "project_derived"
                if scoped
                else "native_default",
                basis="submit requested scope to native software" if scoped else None,
            )
            self._capture(
                "boundary_layers.layers",
                control.number_of_layers,
                self._source("boundary_layers.layers", {"source": requested.get("layers_source")}),
            )
            self._capture(
                "boundary_layers.growth_rate",
                control.rate,
                self._source(
                    "boundary_layers.growth_rate", {"source": requested.get("growth_rate_source")}
                ),
            )
            self._capture(
                "boundary_layers.first_height",
                control.first_height,
                self._source(
                    "boundary_layers.first_layer_height", requested.get("first_layer_height")
                ),
                length=True,
            )
            self._capture(
                "boundary_layers.offset_method",
                control.offset_method_type,
                "project_derived"
                if settings["first_layer_height"] is not None
                else "native_default",
                basis="uniform offset applies the requested first height"
                if settings["first_layer_height"] is not None
                else None,
            )
            result = (
                control() if existing_children else control.add_child_and_update(defer_update=False)
            )
            observation = self.observe("boundary_layers", control, result)
        except Exception as error:
            observation = self.observe("boundary_layers", locals().get("control", task))
            observation["exception"] = {"type": type(error).__name__, "message": str(error)}
            raise StepExecutionError(
                "boundary_layers", f"{type(error).__name__}: {error}", observation
            ) from error
        if self._observation_failed(observation):
            raise StepExecutionError(
                "boundary_layers", "Fluent boundary layers did not complete.", observation
            )
        return observation

    def _step_volume_mesh(self) -> dict[str, Any]:
        settings = self.repair_state.snapshot()
        task = self._task_for_step("volume_mesh")
        set_task_value(task, "volume_fill", self.job.volume_fill)
        if settings["global_size"] is not None:
            set_task_value(
                task.volume_fill_controls, "hex_max_cell_length", settings["global_size"]
            )
        if settings["boundary_layers"]["layers"] == 0:
            set_task_value(task, "prism_layers", False)
        elif "boundary_layers.layers" in self.repaired_sources:
            set_task_value(task, "prism_layers", True)
        # Scoped layer controls belong to Add Boundary Layers, not a global
        # volume-mesh override which would broaden the user's selected walls.
        self._capture(
            "volume_mesh.prism_layers",
            task.prism_layers,
            "runtime_repair"
            if "boundary_layers.layers" in self.repaired_sources
            else "user"
            if settings["boundary_layers"]["layers"] == 0
            else "native_default",
        )
        self._capture("volume_mesh.growth_rate", task.volume_fill_controls.growth_rate)
        self._capture(
            "volume_mesh.max_size",
            task.volume_fill_controls.hex_max_cell_length,
            self._source(
                "global_size", self.job.raw.get("parameter_sources", {}).get("global_size")
            ),
            length=True,
        )
        return self._run_callable_step("volume_mesh", task, task)

    def _step_final_validation(self) -> dict[str, Any]:
        self.step_attempts["final_validation"] += 1
        # Earlier attempts can contain a successful report for a now-invalid mesh.
        # Validate only this execution, with an explicit metric (never TUI defaults).
        if self.transcript_started:
            stop_transcript(self.session)
        validation_path = self.transcript_path.with_name(
            f"validation-{self.step_attempts['final_validation']}.trn"
        )
        self.transcript_started = start_transcript(self.session, validation_path)
        self.transcript_paths.append(validation_path)
        errors: list[str] = []
        if not self.transcript_started:
            errors.append("Cannot record this quality check; mesh validation is unavailable.")
        try:
            self.session.tui.mesh.check_mesh()
            self.session.tui.mesh.check_quality_level(1)
            self.session.tui.mesh.check_quality()
            # Fluent's default Tri/Tet skewness convention is "volume"; let
            # Fluent evaluate its mixed/polyhedral cells, do not derive 1-OQ here.
            self.session.tui.report.quality_method("skewness", "volume")
            self.session.tui.report.cell_quality_limits(["*"])
        except Exception as error:
            errors.append(f"Quality command failed: {type(error).__name__}: {error}")
        try:
            self.session.tui.file.cff_files("yes")
            self.session.tui.file.write_mesh(str(self.runtime_output))
        except Exception as error:
            errors.append(f"Mesh write failed: {type(error).__name__}: {error}")
        output_bytes = self.runtime_output.stat().st_size if self.runtime_output.is_file() else 0
        read_back = False
        if output_bytes:
            try:
                self.session.tui.file.read_mesh(str(self.runtime_output))
                read_back = True
            except Exception as error:
                errors.append(f"Mesh readback failed: {type(error).__name__}: {error}")
        else:
            errors.append("Fluent did not write a nonempty mesh.")
        observation = self.observe("final_validation", None, not errors)
        observation.update({"errors": errors, "output_bytes": output_bytes, "read_back": read_back})
        if errors:
            raise StepExecutionError("final_validation", "; ".join(errors), observation)
        if self.transcript_started:
            stop_transcript(self.session)
            self.transcript_started = False
        quality = parse_quality_report(
            self.transcript_paths[-1],
            self.job,
            self.repair_state.boundaries,
            read_back=read_back,
            actual_boundaries=self.actual_boundary_names(),
            actual_boundary_types=self.actual_boundary_types(),
        )
        observation["quality"] = quality
        if not quality["passed"]:
            raise StepExecutionError(
                "final_validation",
                "Mesh quality, boundary or readback checks did not all pass.",
                observation,
            )
        return observation

    def revert_from(self, step: str) -> dict[str, Any]:
        if not self.transcript_started:
            self._transcript_index += 1
            next_path = self.transcript_path.with_name(
                f"{self.transcript_path.stem}-repair-{self._transcript_index}{self.transcript_path.suffix}"
            )
            self.transcript_started = start_transcript(self.session, next_path)
            self.transcript_paths.append(next_path)
        if step == "final_validation":
            return {
                "step": step,
                "reverted": False,
                "reason": "Final validation can be retried directly.",
            }
        task = self._task_for_step(step)
        before = self.observe(step, task)
        try:
            result = task.revert()
        except Exception as error:
            raise StepExecutionError(
                step,
                f"Task revert failed: {type(error).__name__}: {error}",
                self.observe(step, task),
            ) from error
        after = self.observe(step, task)
        return {
            "step": step,
            "reverted": result if isinstance(result, bool) else None,
            "result": _json_safe(result),
            "state_before": before["state"],
            "state_after": after["state"],
        }

    def available_names(self) -> set[str]:
        observed_fields = {
            "boundary_current_list",
            "region_current_list",
            "original_zones",
            "complete_face_label_list",
            "complete_edge_label_list",
            "complete_topology_list",
            "complete_bl_label_list",
            "complete_bl_zone_list",
            "complete_bl_region_list",
            "complete_region_scope",
            "complete_zone_selection_list",
            "complete_label_selection_list",
        }
        names: set[str] = set()

        def visit(key: str, value: Any) -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(str(child_key), child)
            elif key.casefold() in observed_fields and isinstance(value, (list, tuple, set)):
                for child in value:
                    visit(key, child)
            elif key.casefold() in observed_fields and isinstance(value, str) and value.strip():
                names.update(_names_from_value(value))

        for observation in self.last_observations.values():
            visit("arguments", observation.get("arguments", {}))
        return names

    def actual_boundary_names(self) -> set[str]:
        return set(self.actual_boundary_types())

    def actual_boundary_types(self) -> dict[str, str]:
        observation = self.last_observations.get("update_boundaries", {})
        arguments = observation.get("arguments", {})
        if not isinstance(arguments, dict):
            return {}
        names = _names_from_value(arguments.get("boundary_current_list"))
        types = _names_from_value(arguments.get("boundary_current_type_list"))
        if len(names) != len(types):
            return {}
        return dict(zip(names, types, strict=True))

    def available_names_by_category(self) -> dict[str, set[str]]:
        """Return only observed names that are compatible with each repair target."""
        observation = self.last_observations.get("update_boundaries", {})
        arguments = observation.get("arguments", {})
        current_names = _names_from_value(arguments.get("boundary_current_list"))
        current_types = _names_from_value(arguments.get("boundary_current_type_list"))
        typed: dict[str, set[str]] = {
            "inlet": set(),
            "outlet": set(),
            "wall": set(),
            "symmetry": set(),
        }
        inlet_types = {
            "velocity-inlet",
            "pressure-inlet",
            "mass-flow-inlet",
            "inlet-vent",
            "intake-fan",
        }
        outlet_types = {
            "pressure-outlet",
            "mass-flow-outlet",
            "outflow",
            "outlet-vent",
            "exhaust-fan",
        }
        for name, boundary_type in zip(current_names, current_types):
            normalized = boundary_type.casefold()
            if normalized in inlet_types:
                typed["inlet"].add(name)
            elif normalized in outlet_types:
                typed["outlet"].add(name)
            elif normalized == "wall":
                typed["wall"].add(name)
            elif normalized == "symmetry":
                typed["symmetry"].add(name)
        all_names = self.available_names()
        return {
            "boundaries.inlet": typed["inlet"],
            "boundaries.outlet": typed["outlet"],
            "boundaries.wall": typed["wall"],
            "boundaries.symmetry": typed["symmetry"],
            "local_refinements": all_names,
            "boundary_layers": typed["wall"],
        }


def _quality_number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
        if matches:
            try:
                return float(matches[-1].group(1))
            except ValueError:
                continue
    return None


def boundary_check(
    text: str,
    job: MeshJob,
    current_boundaries: dict[str, list[str]],
    actual_boundaries: set[str] | None = None,
    actual_boundary_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    expected_types = {
        "inlet": "velocity-inlet",
        "outlet": "pressure-outlet",
        "wall": "wall",
        "symmetry": "symmetry",
    }
    requested = [
        (role, name)
        for role in ("inlet", "outlet", "wall", "symmetry")
        for name in current_boundaries[role]
    ]
    missing: list[str] = []
    type_mismatches: list[dict[str, str]] = []
    for role, name in requested:
        if actual_boundaries is not None:
            found = name in actual_boundaries
        else:
            quoted = rf"['\"]{re.escape(name)}['\"]"
            standalone = rf"(?m)^\s*{re.escape(name)}\s*$"
            found = bool(re.search(quoted, text) or re.search(standalone, text))
        if not found:
            missing.append(name)
        elif actual_boundary_types is not None:
            actual_type = actual_boundary_types.get(name)
            if actual_type != expected_types[role]:
                type_mismatches.append(
                    {"name": name, "expected": expected_types[role], "actual": str(actual_type)}
                )
    return {
        "ok": not missing and not type_mismatches,
        "requested_names_checked": bool(requested),
        "requested": [{"role": role, "name": name} for role, name in requested],
        "actual": sorted(actual_boundaries) if actual_boundaries is not None else None,
        "actual_types": actual_boundary_types,
        "missing": missing,
        "type_mismatches": type_mismatches,
    }


def parse_quality_report(
    transcript_paths: Path | list[Path],
    job: MeshJob,
    current_boundaries: dict[str, list[str]],
    read_back: bool,
    actual_boundaries: set[str] | None = None,
    actual_boundary_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    paths = [transcript_paths] if isinstance(transcript_paths, Path) else transcript_paths
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file()
    )
    cell_matches = re.findall(r"\bcells?\s*:\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    cell_matches += re.findall(r"([0-9][0-9,]*)\s+cells?\s+were\s+created", text, re.IGNORECASE)
    cells = max((int(item.replace(",", "")) for item in cell_matches), default=None)
    number = r"([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"
    orthogonal = _quality_number(
        text,
        (
            rf"minimum\s+orthogonal\s+quality\s*[:=]?\s*{number}",
            rf"orthogonal\s+quality\s*[:=]?\s*{number}",
        ),
    )
    surface_skewness = _quality_number(
        text,
        (
            rf"surface\s+mesh(?:ing)?.{{0,120}}maximum\s+skewness\s+of\s*[:=]?\s*{number}",
            rf"surface\s+mesh(?:ing)?.{{0,120}}maximum\s+skewness\s*[:=]?\s*{number}",
        ),
    )
    cell_skewness = _quality_number(
        text,
        (
            rf"maximum[-\s]+cell[-\s]+skew(?:ness)?\s*[:=]?\s*{number}",
            rf"cell\s+skew(?:ness)?\s+limits?.{{0,160}}max(?:imum)?\s*[:=]?\s*{number}",
            rf"skewness\s+limits?.{{0,160}}max(?:imum)?\s*[:=]?\s*{number}",
        ),
    )
    quality_limits = re.findall(
        rf"quality\s+limits\s*\(min\s+max\s+ave\)\s*=\s*\(\s*{number}\s+{number}",
        text,
        flags=re.IGNORECASE,
    )
    if quality_limits:
        cell_skewness = float(quality_limits[-1][1])
    minimum_volume = _quality_number(
        text,
        (rf"minimum\s+(?:cell\s+)?volume\s*[:=]?\s*{number}",),
    )
    negative: int | None = None
    negative_match = re.search(
        r"(?:negative\s+(?:cell\s+)?volumes?|negative\s+volume\s+cells?)\s*[:=]?\s*([0-9][0-9,]*)",
        text,
        re.IGNORECASE,
    )
    if negative_match:
        negative = int(negative_match.group(1).replace(",", ""))
    elif re.search(r"(?:no|zero|0)\s+negative\s+(?:cell\s+)?volumes?", text, re.IGNORECASE):
        negative = 0
    elif re.search(r"negative\s+volume", text, re.IGNORECASE):
        negative = 1
    elif minimum_volume is not None:
        negative = 0 if minimum_volume > 0 else 1
    boundaries = boundary_check(
        text,
        job,
        current_boundaries,
        actual_boundaries,
        actual_boundary_types,
    )
    available = (
        cells is not None
        and orthogonal is not None
        and cell_skewness is not None
        and negative is not None
    )
    metric_rejected = bool(re.search(r"invalid input|invalid command", text, re.IGNORECASE))
    passed = bool(
        available
        and cells > 0
        and not metric_rejected
        and orthogonal is not None
        and cell_skewness is not None
        and orthogonal >= job.quality["min_orthogonal_quality"]
        and cell_skewness <= job.quality["max_skewness"]
        and negative == 0
        and boundaries["ok"]
        and read_back
    )
    return {
        "cell_count": cells,
        "minimum_orthogonal_quality": orthogonal,
        "maximum_skewness": cell_skewness,
        "maximum_skewness_source": "report.cell_quality_limits" if quality_limits else None,
        "skewness_definition": "Fluent skewness with default volume measure (not normalized angle)",
        "quality_command": "/report/quality-method skewness volume",
        "metric_command_rejected": metric_rejected,
        "surface_maximum_skewness": surface_skewness,
        "minimum_cell_volume": minimum_volume,
        "negative_volume_cells": negative,
        "boundary_check": boundaries,
        "read_back": read_back,
        "thresholds": dict(job.quality),
        "available": available,
        "passed": passed,
    }
