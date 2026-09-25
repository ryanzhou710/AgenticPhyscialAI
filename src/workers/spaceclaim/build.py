# Python Script, API Version = V241
"""SpaceClaim-side extraction, isolation, and explicit boundary grouping."""

import json
import os
import traceback

execfile(os.environ["CFD_AGENT_SC_COMMON"], globals())

from SpaceClaim.Api.V241 import Group, IDocObject
from SpaceClaim.Api.V241.Scripting.Commands import Delete, DocumentOpen, DocumentSave, RenameObject, VolumeExtract
from System.Collections.Generic import List


with open(os.environ["CFD_AGENT_SC_BUILD_REQUEST"], "r") as request_stream:
    build_request = json.load(request_stream)

build_result = {
    "ok": False,
    "operation": build_request.get("operation"),
    "steps": [],
    "images": [],
}


def record(step, data):
    build_result["steps"].append({"step": step, "result": data})


def save_picture(name):
    Selection.Empty().SetActive()
    ViewHelper.SetProjection(ViewHelper.ViewProjection.Isometric, True, False)
    ViewHelper.ZoomToEntity(Selection.Create(list(DocumentHelper.GetRootPart().GetAllBodies())))
    refresh_for_export(build_request["ui_mode"])
    path = os.path.join(build_request["folder"], name + ".png")
    export_picture(path)
    build_result["images"].append({"view": name, "path": path})


def _body_row(catalog, body):
    moniker = moniker_of(body)
    for row in catalog["public"]["bodies"]:
        ref = catalog["internal"]["refs"].get(row["id"], {})
        if ref.get("moniker") == moniker:
            return {
                "body_id": row["id"],
                "moniker": moniker,
                "volume_m3": float(body.Shape.Volume),
                "face_count": int(body.Faces.Count),
            }
    raise ValueError("Current target body is missing from the rebuilt catalog")


def _positive_created_bodies(catalog, volumes):
    result = []
    for body in volumes:
        if body.Shape.Volume > 0:
            result.append(_body_row(catalog, body))
    return result


def _open_and_validate(catalog, requested_ids):
    current = build_catalog()
    validate_catalog_identity(catalog, current, requested_ids)
    return current


try:
    operation = build_request["operation"]
    DocumentOpen.Execute(build_request["input"])

    if operation == "extract_volume":
        terminal_records = build_request["terminal_records"]
        seed_face_id = build_request["seed_face_id"]
        requested = [seed_face_id]
        for terminal in terminal_records:
            requested.extend(terminal["object_ids"])
        catalog = _open_and_validate(build_request["catalog"], requested)
        seed_face = LIVE_OBJECTS[seed_face_id]
        seed_center = MeasureHelper.GetCentroid(Selection.Create(seed_face))
        options = VolumeExtractOptions()
        options.SeedPoint = seed_face.Shape.Geometry.ProjectPoint(seed_center)
        options.CreateShareTopology = False
        strategy = build_request["extraction_strategy"]
        if strategy == "faces":
            native_ids = [value for terminal in terminal_records for value in terminal["native_ids"]]
            cap_objects = [LIVE_OBJECTS[value] for value in native_ids]
            extraction = VolumeExtract.Create(Selection.Create(cap_objects), Selection.Create(seed_face), options)
        elif strategy == "edges":
            native_ids = [value for terminal in terminal_records for value in terminal["native_ids"]]
            cap_edges = [LIVE_OBJECTS[value] for value in native_ids]
            extraction = VolumeExtract.Create(Selection.Create(cap_edges), Selection.Create(seed_face), options)
        else:
            raise ValueError("Unsupported extraction strategy: " + str(strategy))
        volumes = list(extraction.CreatedVolumes)
        if not extraction.Success:
            raise ValueError("VolumeExtract did not report success")
        extracted_catalog = build_catalog()
        candidates = _positive_created_bodies(extracted_catalog, volumes)
        if not candidates:
            raise ValueError("VolumeExtract created no positive-volume body")
        DocumentSave.Execute(build_request["output"])
        build_result["transfer"] = {
            "source_mode": "volume_extract",
            "extraction_strategy": strategy,
            "candidate_bodies": candidates,
            "seed_point_m": vector3(seed_center),
        }
        build_result["catalog"] = extracted_catalog
        record("extract_volume", build_result["transfer"])
        save_picture("extraction-candidates")

    elif operation == "isolate_body":
        target_body_id = build_request["target_body_id"]
        catalog = _open_and_validate(build_request["catalog"], [target_body_id])
        body = LIVE_OBJECTS.get(target_body_id)
        if body is None or body.Shape.Volume <= 0:
            raise ValueError("Selected target body is not a positive-volume body")
        selected, selection = select_candidates({"candidate_ids": [target_body_id]}, catalog)
        if selected != [body] or not selection.get("selection_verified"):
            raise ValueError("SpaceClaim did not select the requested target body")
        for group in list(Window.ActiveWindow.Groups):
            group.Delete()
        others = [item for item in DocumentHelper.GetRootPart().GetAllBodies() if item != body]
        if others:
            Delete.Execute(Selection.Create(others))
        RenameObject.Execute(Selection.Create(body), "fluid")
        isolated_catalog = build_catalog()
        DocumentSave.Execute(build_request["output"])
        target = _body_row(isolated_catalog, body)
        build_result["target_body"] = target
        build_result["catalog"] = isolated_catalog
        record("isolate_body", target)
        save_picture("isolated-fluid")

    elif operation == "label_faces":
        target_body_id = build_request["target_body_id"]
        groups = build_request["groups"]
        requested = [target_body_id]
        for group in groups:
            requested.extend(group["face_ids"])
        catalog = _open_and_validate(build_request["catalog"], requested)
        fluid = LIVE_OBJECTS.get(target_body_id)
        if fluid is None or fluid.Shape.Volume <= 0:
            raise ValueError("Selected grouping body is not a positive-volume body")
        target_faces = list(fluid.Faces)
        target_monikers = set([moniker_of(face) for face in target_faces])
        assigned = set()
        records = []
        names = set()
        for group in groups:
            name = group["name"]
            if name in names:
                raise ValueError("Boundary group names are not unique")
            names.add(name)
            face_ids = list(group["face_ids"])
            selected, selection = select_candidates({"candidate_ids": face_ids}, catalog)
            if not selection.get("selection_verified"):
                raise ValueError("SpaceClaim did not select the requested boundary faces")
            monikers = set([moniker_of(face) for face in selected])
            if not monikers or not monikers.issubset(target_monikers):
                raise ValueError("Boundary group contains a face outside the target body")
            if assigned.intersection(monikers):
                raise ValueError("Boundary groups contain overlapping faces")
            assigned.update(monikers)
            records.append((name, group["role"], selected))
        if assigned != target_monikers:
            raise ValueError("Boundary groups do not cover every target-body face")
        for group in list(Window.ActiveWindow.Groups):
            group.Delete()
        for name, role, faces in records:
            Group.Create(DocumentHelper.GetRootPart(), name, List[IDocObject](faces))
        # Rebuild the catalog after group creation.  Group.Create is native
        # state, so the handoff record must be based on the members that
        # SpaceClaim actually persisted rather than only the requested faces.
        final_catalog = build_catalog()
        final_groups = dict([
            (row["raw_name"], row["member_ids"])
            for row in final_catalog["internal"].get("raw_groups", [])])
        final_refs = final_catalog["internal"]["refs"]
        group_records = [
            {
                "name": name,
                "role": role,
                "count": len(final_groups.get(name, [])),
                "member_monikers": [
                    final_refs[member]["moniker"]
                    for member in final_groups.get(name, [])
                ],
            }
            for name, role, faces in records
        ]
        for record_row, (_, _, faces) in zip(group_records, records):
            expected = set([moniker_of(face) for face in faces])
            actual = set(record_row["member_monikers"])
            if not actual or actual != expected:
                raise ValueError(
                    "SpaceClaim group members differ from the confirmed face selection: "
                    + record_row["name"])
        DocumentSave.Execute(build_request["output"])
        build_result["groups"] = group_records
        build_result["catalog"] = final_catalog
        build_result["coverage"] = len(assigned)
        build_result["total_faces"] = len(target_faces)
        record("label_faces", {"groups": group_records, "coverage": len(assigned)})
        save_picture("labeled-fluid")
        if build_request.get("keep_open"):
            execfile(os.environ["CFD_AGENT_SC_SAVE"], globals())
            build_result["save_bridge"] = install_save_bridge(
                Window.ActiveWindow.Document, build_request["output"], build_request["folder"]
            )
    else:
        raise ValueError("Unsupported build operation: " + str(operation))

    build_result["ok"] = True
except Exception as error:
    build_result["error"] = traceback.format_exc()
    message = (str(error) or "").lower()
    code = "CAD_BUILD_OPERATION_FAILED"
    if "identity" in message or "moniker" in message:
        code = "CAD_OBJECT_IDENTITY_CHANGED"
    elif "license" in message:
        code = "CAD_LICENSE_UNAVAILABLE"
    build_result["error_detail"] = {
        "code": code,
        "stage": build_result.get("operation"),
        "reason": str(error) or "SpaceClaim native operation failed without a message",
        "raw_error": build_result["error"],
    }

temporary = build_request["response"] + ".tmp"
with open(temporary, "w") as response_stream:
    json.dump(build_result, response_stream, indent=2)
os.rename(temporary, build_request["response"])
