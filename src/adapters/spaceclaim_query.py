"""Windows host for read-only SpaceClaim V241 geometry queries and selection."""

from __future__ import annotations

import atexit
import json
import msvcrt
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.config import RuntimeConfig
from src.services.geometry_catalog import GeometryCatalog, SelectionExecution

DEFAULT_EVIDENCE_VIEWS = ("Front", "Top", "Right", "Isometric")
ALLOWED_EVIDENCE_VIEWS = frozenset(
    {
        "Front",
        "Back",
        "Top",
        "Bottom",
        "Right",
        "Left",
        "Isometric",
    }
)
ALLOWED_DETAIL_VIEWS = frozenset({"Selected", "OwnerContext", "SelectedProxy"})


class SpaceClaimError(RuntimeError):
    def __init__(
        self, message: str, *, detail: dict | None = None, evidence: dict | None = None
    ) -> None:
        super().__init__(message)
        self.detail = detail or {}
        self.evidence = evidence or {}


class SpaceClaimRunner:
    """Launch a fresh SpaceClaim process for catalog or selection operations.

    A short ASCII-only staging path avoids SpaceClaim's intermittent failure to
    open Unicode paths. The source SCDOC is copied and never saved or modified.
    """

    def __init__(
        self,
        *,
        output_dir: Path,
        ui_mode: str = "hidden",
        timeout_s: float | None = None,
        config: RuntimeConfig | None = None,
    ):
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ui_mode = ui_mode
        self.config = config or RuntimeConfig()
        self.timeout_s = timeout_s if timeout_s is not None else self.config.spaceclaim_timeout_s
        self.script = Path(__file__).resolve().parents[1] / "workers" / "spaceclaim" / "query.py"
        # Every invocation owns a separate ASCII-only directory.  This avoids
        # response/image races when one runner is used from concurrent tasks.
        self._stages: set[Path] = set()
        atexit.register(self.close)
        if ui_mode not in {"gui", "hidden"}:
            raise ValueError("ui_mode must be gui or hidden")
        if not self.script.is_file():
            raise FileNotFoundError(self.script)

    @staticmethod
    def executable(config: RuntimeConfig | None = None) -> Path:
        return (config or RuntimeConfig()).spaceclaim_executable()

    def _stage_root(self) -> Path:
        base = self.config.staging_root() / "spaceclaim"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @contextmanager
    def _spaceclaim_process_slot(self):
        """Serialize SpaceClaim /RunScript calls across Python processes."""
        lock_path = self._stage_root() / "spaceclaim-v241-runscript.lock"
        stream = lock_path.open("a+b")
        acquired = False
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + self.timeout_s
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise SpaceClaimError(
                            "Timed out waiting for the exclusive SpaceClaim V241 "
                            f"/RunScript slot after {self.timeout_s}s"
                        ) from error
                    time.sleep(0.1)
            yield
        finally:
            if acquired:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            stream.close()

    @staticmethod
    def _diagnostic_log_roots() -> list[Path]:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return []
        root = Path(appdata) / "SpaceClaim"
        return [root / "Log Files", root / "Journal Files", root / "StrideJournals"]

    @classmethod
    def _diagnostic_snapshot(cls) -> dict[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        for root in cls._diagnostic_log_roots():
            if not root.is_dir():
                continue
            for path in root.iterdir():
                try:
                    if path.is_file():
                        stat = path.stat()
                        result[str(path)] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
        return result

    def _preserve_no_response_evidence(
        self,
        *,
        stage: Path,
        run_id: str,
        logs_before: dict[str, tuple[int, int]],
    ) -> Path:
        """Copy the otherwise ephemeral invocation and newly written host logs."""
        destination = self.output_dir / f"spaceclaim-no-response-{run_id}"
        shutil.copytree(stage, destination)
        log_output = destination / "spaceclaim-user-logs"
        for root in self._diagnostic_log_roots():
            if not root.is_dir():
                continue
            for path in root.iterdir():
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    current = (stat.st_size, stat.st_mtime_ns)
                    if logs_before.get(str(path)) == current:
                        continue
                    target = log_output / root.name / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                except OSError:
                    continue
        return destination

    @staticmethod
    def _font(size: int):
        try:
            return ImageFont.truetype("arialbd.ttf", size)
        except OSError:
            return ImageFont.load_default()

    def _annotate_candidate(self, path: Path, candidate_id: str, view: str | None = None) -> None:
        with Image.open(path).convert("RGB") as source:
            image = source.copy()
        draw = ImageDraw.Draw(image)
        font = self._font(42)
        label = candidate_id if not view else candidate_id + " / " + view
        box = draw.textbbox((0, 0), label, font=font)
        width = box[2] - box[0] + 30
        height = box[3] - box[1] + 20
        draw.rectangle((0, 0, width, height), fill=(255, 235, 0), outline=(0, 0, 0), width=3)
        draw.text((15, 8), label, fill=(0, 0, 0), font=font)
        image.save(path)

    def _contact_sheets(self, run_id: str, candidate_images: list[dict]) -> list[dict]:
        results = []
        columns, rows = 4, 5
        page_size = columns * rows
        cell_w, cell_h = 360, 270
        for page, offset in enumerate(range(0, len(candidate_images), page_size), start=1):
            chunk = candidate_images[offset : offset + page_size]
            sheet = Image.new("RGB", (cell_w * columns, cell_h * rows), "white")
            for index, item in enumerate(chunk):
                with Image.open(item["path"]).convert("RGB") as source:
                    thumb = ImageOps.contain(
                        source, (cell_w, cell_h), method=Image.Resampling.LANCZOS
                    )
                x = (index % columns) * cell_w + (cell_w - thumb.width) // 2
                y = (index // columns) * cell_h + (cell_h - thumb.height) // 2
                sheet.paste(thumb, (x, y))
            path = self.output_dir / f"{run_id}-candidate-contact-{page:02d}.png"
            sheet.save(path)
            results.append(
                {
                    "view": "CandidateContactSheet",
                    "path": str(path),
                    "candidate_ids": [item["candidate_id"] for item in chunk],
                    "grid_columns": columns,
                    "grid_rows": rows,
                    "cell_width": cell_w,
                    "cell_height": cell_h,
                    "model_visible": True,
                }
            )
        return results

    @staticmethod
    def _normalize_views(views: list[str] | None, *, default: tuple[str, ...]) -> list[str]:
        if views is None:
            result = list(default)
        elif not isinstance(views, list):
            raise ValueError("views must be a list")
        else:
            result = list(views)
        if any(not isinstance(view, str) or view not in ALLOWED_EVIDENCE_VIEWS for view in result):
            raise ValueError("views contains an unsupported SpaceClaim projection")
        if len(result) != len(set(result)):
            raise ValueError("views must not contain duplicates")
        return result

    @staticmethod
    def _normalize_detail_views(views: list[str] | None) -> list[str]:
        result = ["Selected"] if views is None else list(views)
        if any(not isinstance(view, str) or view not in ALLOWED_DETAIL_VIEWS for view in result):
            raise ValueError("detail_views contains an unsupported candidate detail view")
        if not result or len(result) != len(set(result)):
            raise ValueError("detail_views must be a non-empty list without duplicates")
        return result

    @staticmethod
    def _require_detail_evidence(images: list[dict], expected: list[str], *, context: str) -> None:
        for view in expected:
            rows = [row for row in images if row.get("view") == view]
            if len(rows) != 1:
                raise SpaceClaimError(f"{context} requires exactly one {view} image")
            path = Path(rows[0].get("path", ""))
            if rows[0].get("error") or not path.is_file():
                raise SpaceClaimError(f"{context} did not produce a usable {view} image")

    @staticmethod
    def _require_view_evidence(images: list[dict], expected: list[str], *, context: str) -> None:
        if not isinstance(images, list):
            raise SpaceClaimError(f"{context} did not return an image list")
        for view in expected:
            rows = [row for row in images if row.get("view") == view]
            if len(rows) != 1:
                raise SpaceClaimError(
                    f"{context} requires exactly one {view} image; received {len(rows)}"
                )
            row = rows[0]
            path = Path(row.get("path", ""))
            if row.get("error") or not path.is_file():
                raise SpaceClaimError(f"{context} did not produce a usable {view} image")
            expected_role = (
                "direction_reference"
                if view == "Front"
                else "selection_detail"
                if view == "Selected"
                else "auxiliary"
            )
            if row.get("role") != expected_role:
                raise SpaceClaimError(f"{context} returned the wrong role for {view}")
            if view != "Selected" and row.get("coordinate_frame") != "spaceclaim_global":
                raise SpaceClaimError(f"{context} did not use the global frame for {view}")

    def _invoke(self, geometry_path: Path, request_data: dict) -> dict:
        geometry_path = Path(geometry_path).resolve()
        if not geometry_path.is_file() or geometry_path.suffix.lower() != ".scdoc":
            raise ValueError("A real .scdoc geometry file is required")
        run_id = uuid.uuid4().hex
        stage = self._stage_root() / run_id
        stage.mkdir(parents=True)
        self._stages.add(stage)
        staged_geometry = stage / "input.scdoc"
        staged_script = stage / f"spaceclaim-query-{run_id}.py"
        response_path = stage / f"response-{run_id}.json"
        request_path = stage / f"request-{run_id}.json"
        launch_path = stage / f"launch-{run_id}.json"
        started_path = stage / f"started-{run_id}.txt"
        try:
            shutil.copy2(geometry_path, staged_geometry)
            shutil.copy2(self.script, staged_script)
            shutil.copy2(
                self.script.with_name("common.py"),
                stage / "common.py",
            )
            payload = {
                **request_data,
                "run_id": run_id,
                "ui_mode": self.ui_mode,
                "input": str(staged_geometry),
                "response": str(response_path),
                "started": str(started_path),
                "folder": str(stage),
                "image_stem": run_id + "-" + str(request_data.get("operation", "operation")),
            }
            request_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2), encoding="ascii"
            )
            environment = dict(
                os.environ,
                SPACECLAIM_GROUNDING_REQUEST=str(request_path),
                CFD_AGENT_SC_COMMON=str(stage / "common.py"),
            )
            args = [
                str(self.executable(self.config)),
                "/UseLicenseMode=true",
                "/RunScript=" + str(staged_script),
                "/ScriptAPI=241",
                "/ExitAfterScript=True",
                "/Splash=False",
                "/Welcome=False",
                "/Headless=" + ("False" if self.ui_mode == "gui" else "True"),
            ]
            startup = subprocess.STARTUPINFO()
            if self.ui_mode == "hidden":
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
            launch_record = {
                "run_id": run_id,
                "operation": request_data.get("operation"),
                "ui_mode": self.ui_mode,
                "command": args,
                "source_geometry": str(geometry_path),
                "source_size": geometry_path.stat().st_size,
                "source_mtime_ns": geometry_path.stat().st_mtime_ns,
                "started_ns": None,
                "ended_ns": None,
                "pid": None,
                "exit_code": None,
            }
            launch_path.write_text(
                json.dumps(launch_record, ensure_ascii=True, indent=2), encoding="ascii"
            )
            logs_before: dict[str, tuple[int, int]] = {}
            with self._spaceclaim_process_slot():
                logs_before = self._diagnostic_snapshot()
                launch_record["started_ns"] = time.time_ns()
                with (
                    (stage / "spaceclaim-stdout.txt").open("wb") as stdout_stream,
                    (stage / "spaceclaim-stderr.txt").open("wb") as stderr_stream,
                ):
                    process = subprocess.Popen(
                        args,
                        cwd=stage,
                        env=environment,
                        startupinfo=startup,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                    )
                    launch_record["pid"] = process.pid
                    try:
                        exit_code = process.wait(timeout=self.timeout_s)
                    except subprocess.TimeoutExpired as error:
                        process.kill()
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        raise SpaceClaimError(
                            f"SpaceClaim timed out after {self.timeout_s}s"
                        ) from error
                    finally:
                        launch_record["ended_ns"] = time.time_ns()
                        launch_record["exit_code"] = process.poll()
                        launch_path.write_text(
                            json.dumps(launch_record, ensure_ascii=True, indent=2),
                            encoding="ascii",
                        )
            if exit_code != 0:
                raise SpaceClaimError(f"SpaceClaim exited with code {exit_code}")
            if not response_path.is_file():
                evidence = self._preserve_no_response_evidence(
                    stage=stage,
                    run_id=run_id,
                    logs_before=logs_before,
                )
                raise SpaceClaimError(
                    f"SpaceClaim produced no response (exit code {exit_code}); "
                    f"evidence preserved at {evidence}"
                )
            response = json.loads(response_path.read_text(encoding="utf-8-sig"))
            if response.get("run_id") != run_id:
                raise SpaceClaimError("SpaceClaim response does not belong to this invocation")
            if response.get("operation") != request_data.get("operation"):
                raise SpaceClaimError("SpaceClaim response operation does not match the request")
            image_serial = 0

            def copied_image(item: dict) -> dict:
                nonlocal image_serial
                row = dict(item)
                source = Path(row.get("path", ""))
                if source.is_file():
                    image_serial += 1
                    # Batch task IDs and CAD names can make the native export
                    # filename exceed Win32's legacy path limit once copied
                    # into the evidence directory.  The record already carries
                    # task/candidate metadata, so use a short unique evidence
                    # filename instead of repeating the native basename.
                    suffix = source.suffix if source.suffix else ".png"
                    destination = self.output_dir / (
                        f"{run_id[:8]}-image-{image_serial:05d}{suffix}"
                    )
                    shutil.copy2(source, destination)
                    row["path"] = str(destination)
                    if row.get("candidate_id"):
                        self._annotate_candidate(
                            destination, str(row["candidate_id"]), str(row.get("view") or "")
                        )
                        row["model_visible"] = False
                    else:
                        row["model_visible"] = True
                return row

            copied_images = [copied_image(item) for item in response.get("images", [])]
            for result in response.get("results", []):
                result["images"] = [copied_image(item) for item in result.get("images", [])]
            for result in response.get("candidate_render_results", []):
                candidate_id = result.get("candidate_id")
                result["images"] = [
                    dict(item)
                    for item in copied_images
                    if item.get("candidate_id") == candidate_id
                ]
            candidate_images = [
                row for row in copied_images if row.get("candidate_id") and row.get("path")
            ]
            if request_data.get("render_contact_sheets"):
                copied_images.extend(self._contact_sheets(run_id, candidate_images))
            response["images"] = copied_images
            record_path = self.output_dir / f"spaceclaim-{run_id}.json"
            record_path.write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            response["record_path"] = str(record_path)
            if not response.get("ok"):
                raise SpaceClaimError(response.get("error", "SpaceClaim query operation failed"))
            return response
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            self._stages.discard(stage)

    def catalog(
        self,
        geometry_path: Path,
        *,
        render_candidates: bool = False,
        candidate_collections: list[str] | None = None,
    ) -> tuple[GeometryCatalog, Path]:
        response = self._invoke(
            geometry_path,
            {
                "operation": "catalog",
                "render_candidates": bool(render_candidates),
                "render_contact_sheets": bool(render_candidates),
                "candidate_collections": candidate_collections,
            },
        )
        self._require_view_evidence(
            response.get("images", []),
            list(DEFAULT_EVIDENCE_VIEWS),
            context="SpaceClaim catalog",
        )
        native_catalog = response.get("catalog") or response.get("geometry")
        if not isinstance(native_catalog, dict):
            raise SpaceClaimError("SpaceClaim response did not contain an object catalog")
        public = native_catalog.get("public", native_catalog)
        refs = native_catalog.get("internal", {}).get("refs", {})

        def converted(row: dict, kind: str) -> dict:
            item = dict(row)
            native_kind = item.get("kind")
            item["kind"] = kind
            if kind == "body":
                item["solid_or_sheet"] = native_kind
            box = item.pop("bbox_m", item.get("bbox", None))
            if isinstance(box, dict):
                item["bbox"] = {
                    "min_m": box.get("min_m", box.get("min")),
                    "max_m": box.get("max_m", box.get("max")),
                    "center_m": box.get("center_m", box.get("center")),
                }
            item["axis"] = item.pop("axis_direction", item.get("axis", None))
            item["moniker"] = refs.get(item["id"], {}).get("moniker")
            return item

        geometry_stem = Path(geometry_path).stem
        raw = {
            "catalog_id": uuid.uuid4().hex,
            "geometry_id": geometry_stem,
            "coordinate_unit": public.get("coordinate_unit", "m"),
            "bodies": [converted(row, "body") for row in public.get("bodies", [])],
            "faces": [converted(row, "face") for row in public.get("faces", [])],
            "edges": [converted(row, "edge") for row in public.get("edges", [])],
            "loops": [converted(row, "loop") for row in public.get("loops", [])],
            "images": response.get("images", []),
            "view_contract": response.get("view_contract"),
            "candidate_render_results": response.get("candidate_render_results", []),
            "candidate_render_summary": response.get("candidate_render_summary", {}),
            "native_catalog": native_catalog,
        }
        catalog = GeometryCatalog.model_validate(raw)
        catalog_path = self.output_dir / f"{catalog.geometry_id}-{catalog.catalog_id}-catalog.json"
        catalog_path.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
        return catalog, catalog_path

    def select(
        self,
        geometry_path: Path,
        catalog: GeometryCatalog,
        candidate_ids: list[str],
        *,
        views: list[str] | None = None,
    ) -> SelectionExecution:
        universe = catalog.by_id()
        unknown = sorted(set(candidate_ids) - set(universe))
        if unknown:
            raise ValueError("Unknown candidate IDs: " + ", ".join(unknown))
        if not candidate_ids:
            raise ValueError("At least one candidate ID is required")
        native_catalog = getattr(catalog, "native_catalog", None)
        if not isinstance(native_catalog, dict):
            raise ValueError("Catalog does not contain the private SpaceClaim execution map")
        requested_views = self._normalize_views(views, default=DEFAULT_EVIDENCE_VIEWS)
        request = {
            "operation": "select",
            "catalog": native_catalog,
            "candidate_ids": candidate_ids,
            "views": requested_views,
        }
        response = self._invoke(
            geometry_path,
            request,
        )
        if response.get("requested_candidate_ids") != candidate_ids:
            raise SpaceClaimError("SpaceClaim returned different requested candidate IDs")
        if not response.get("selection_verified") or sorted(
            response.get("active_candidate_ids", [])
        ) != sorted(response.get("expanded_candidate_ids", [])):
            raise SpaceClaimError("SpaceClaim did not verify the exact expanded active selection")
        self._require_view_evidence(
            response.get("images", []),
            requested_views + ["Selected"],
            context="SpaceClaim selection",
        )
        return SelectionExecution.model_validate(
            {
                "ok": response.get("ok", False),
                "selected_ids": response.get("requested_candidate_ids", []),
                "selected_monikers": response.get("active_monikers", []),
                "active_selection_verified": response.get("selection_verified", False),
                "images": response.get("images", []),
                "error": response.get("error"),
            }
        )

    def batch_select(
        self, geometry_path: Path, catalog: GeometryCatalog, selections: list[dict]
    ) -> list[dict]:
        """Execute predicted selections in one SpaceClaim session.

        Each item is ``{task_id, candidate_ids, views?, detail_views?}``.
        ``detail_views`` controls only the selected-object evidence and defaults
        to one fitted ``Selected`` image. Runtime failures remain attached to
        that task and never trigger a replacement choice.
        """
        if not isinstance(selections, list) or not selections:
            raise ValueError("selections must be a non-empty list")
        universe = catalog.by_id()
        normalized: list[dict] = []
        task_ids: set[str] = set()
        for item in selections:
            if not isinstance(item, dict):
                raise ValueError("Every batch selection must be an object")
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Every batch selection requires a non-empty task_id")
            if task_id in task_ids:
                raise ValueError("Batch task_id values must be unique")
            task_ids.add(task_id)
            candidate_ids = item.get("candidate_ids")
            if (
                not isinstance(candidate_ids, list)
                or not candidate_ids
                or any(not isinstance(value, str) or not value for value in candidate_ids)
            ):
                raise ValueError(f"{task_id}: candidate_ids must be a non-empty string list")
            if len(candidate_ids) != len(set(candidate_ids)):
                raise ValueError(f"{task_id}: candidate_ids must not contain duplicates")
            unknown = sorted(set(candidate_ids) - set(universe))
            if unknown:
                raise ValueError(f"{task_id}: unknown candidate IDs: " + ", ".join(unknown))
            task_views = self._normalize_views(item.get("views"), default=())
            detail_views = self._normalize_detail_views(item.get("detail_views"))
            normalized.append(
                {
                    "task_id": task_id,
                    "candidate_ids": list(candidate_ids),
                    "views": task_views,
                    "detail_views": detail_views,
                }
            )

        native_catalog = getattr(catalog, "native_catalog", None)
        if not isinstance(native_catalog, dict):
            raise ValueError("Catalog does not contain the private SpaceClaim execution map")
        response = self._invoke(
            geometry_path,
            {
                "operation": "batch_select",
                "catalog": native_catalog,
                "selections": normalized,
            },
        )
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(normalized):
            raise SpaceClaimError("SpaceClaim returned the wrong number of batch results")
        if [row.get("task_id") for row in results] != [row["task_id"] for row in normalized]:
            raise SpaceClaimError("SpaceClaim batch result order or task IDs changed")

        checked: list[dict] = []
        for request_item, result in zip(normalized, results):
            row = dict(result)
            if row.get("requested_candidate_ids") not in (None, request_item["candidate_ids"]):
                raise SpaceClaimError(
                    f"{request_item['task_id']}: SpaceClaim changed requested candidate IDs"
                )
            if row.get("ok"):
                if row.get("requested_candidate_ids") != request_item["candidate_ids"]:
                    raise SpaceClaimError(
                        f"{request_item['task_id']}: missing requested candidate IDs"
                    )
                if not row.get("selection_verified") or sorted(
                    row.get("active_candidate_ids", [])
                ) != sorted(row.get("expanded_candidate_ids", [])):
                    raise SpaceClaimError(
                        f"{request_item['task_id']}: exact active selection was not verified"
                    )
                if len(row.get("active_monikers", [])) != len(
                    row.get("expanded_candidate_ids", [])
                ):
                    raise SpaceClaimError(
                        f"{request_item['task_id']}: native Moniker evidence is incomplete"
                    )
                self._require_view_evidence(
                    row.get("images", []),
                    request_item["views"],
                    context=f"SpaceClaim batch task {request_item['task_id']}",
                )
                self._require_detail_evidence(
                    row.get("images", []),
                    request_item["detail_views"],
                    context=f"SpaceClaim batch task {request_item['task_id']}",
                )
            elif not row.get("error"):
                raise SpaceClaimError(f"{request_item['task_id']}: failed without error evidence")
            row["selected_ids"] = row.get("requested_candidate_ids", [])
            row["selected_monikers"] = row.get("active_monikers", [])
            row["active_selection_verified"] = bool(row.get("selection_verified", False))
            row["record_path"] = response.get("record_path")
            checked.append(row)
        return checked

    def render_candidate_details(
        self,
        geometry_path: Path,
        catalog: GeometryCatalog,
        requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Render selected candidate evidence in one SpaceClaim invocation per round."""

        if len({item.get("candidate_id") for item in requests}) != len(requests) or len(requests) > 12:
            raise ValueError("candidate detail rendering requires at most 12 distinct candidates")
        selections = [
            {
                "task_id": "detail-" + str(item["candidate_id"]),
                "candidate_ids": [item["candidate_id"]],
                "views": [],
                "detail_views": item["detail_views"],
            }
            for item in requests
        ]
        results = self.batch_select(geometry_path, catalog, selections)
        rendered: list[dict[str, Any]] = []
        for request, result in zip(requests, results, strict=True):
            candidate_id = request["candidate_id"]
            expected = set(request["detail_views"])
            for image in result.get("images", []):
                if image.get("view") not in expected:
                    continue
                row = {**image, "candidate_id": candidate_id, "purpose": request["purpose"]}
                path = Path(row["path"])
                self._annotate_candidate(path, candidate_id, str(image.get("view") or ""))
                rendered.append(row)
        return rendered

    def close(self) -> None:
        for stage in list(getattr(self, "_stages", set())):
            shutil.rmtree(stage, ignore_errors=True)
        if hasattr(self, "_stages"):
            self._stages.clear()
