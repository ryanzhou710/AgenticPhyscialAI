# Python Script, API Version = V241
"""SpaceClaim-side volume extraction and boundary grouping operations.

Loads the complete packaged helper file in SpaceClaim's scripting namespace.
Uses model-selected native references, not case-specific object IDs.
"""

import json
import os
import traceback

execfile(os.environ["CFD_AGENT_SC_COMMON"], globals())

from SpaceClaim.Api.V241 import FillMode, FillOptions, Group, IDocObject
from SpaceClaim.Api.V241.Scripting.Commands import Delete, DocumentOpen, DocumentSave, Fill, RenameObject, VolumeExtract
from System.Collections.Generic import List


with open(os.environ["CFD_AGENT_SC_BUILD_REQUEST"], "r") as request_stream:
    build_request = json.load(request_stream)

build_result = {
    "ok": False,
    "operation": build_request.get("operation"),
    "steps": [],
    "images": [],
}
failure_evidence = {}


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


def _temporary_cap_measurement(edges, terminal):
    """Measure a closed inner contour through a temporary native planar cap.

    A support-face centroid describes the entire annular face, not its inner
    opening.  Filling the selected contour produces the actual cap region for
    arbitrary closed planar loops (including eccentric and non-circular ones).
    The created face is deleted before volume extraction, so it never becomes
    part of the delivered CAD.
    """
    patches = []
    try:
        result = Fill.Execute(
            Selection.Create(edges), Selection.Empty(), FillOptions(), FillMode.ThreeD, None)
        faces = list(result.CreatedFaces)
        # Fill can create more than one face before it reports that the selected
        # contour is unsuitable.  Keep every native object so cleanup is complete
        # on both normal and exceptional paths.
        patches = faces
        if len(faces) != 1:
            raise ValueError(
                "Temporary cap creation did not produce exactly one face for " + terminal["name"])
        patch = patches[0]
        if not isinstance(patch.Shape.Geometry, Plane):
            raise ValueError("Temporary cap is not planar for " + terminal["name"])
        return {
            "center_m": vector3(MeasureHelper.GetCentroid(Selection.Create(patch))),
            "area_m2": float(patch.Area),
        }
    finally:
        # A failed cleanup makes the document state untrustworthy.  Deliberately
        # propagate it instead of silently continuing an extraction attempt.
        cleanup_errors = []
        for patch in reversed(patches):
            try:
                Delete.Execute(Selection.Create(patch))
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise RuntimeError("Temporary cap cleanup failed: " + "; ".join(cleanup_errors))


def _terminal_record(record, face, edges):
    if not isinstance(face.Shape.Geometry, Plane):
        raise ValueError("Selected opening support face is not planar: " + record["name"])
    perimeter = sum(float(edge.Shape.Length) for edge in edges)
    if record.get("is_outer"):
        measurement = {
            "center_m": vector3(MeasureHelper.GetCentroid(Selection.Create(face))),
            "area_m2": float(face.Area),
        }
    else:
        measurement = _temporary_cap_measurement(edges, record)
    terminal = {
        "name": record["name"],
        "role": record["role"],
        "source_candidate_id": record["source_candidate_id"],
        "source_loop_id": record.get("contour_loop_id"),
        "source_face_moniker": moniker_of(face),
        "boundary_edge_count": len(edges),
        "boundary_perimeter_m": perimeter,
        "center_m": measurement["center_m"],
        "normal": plane_normal(face),
        "area_m2": measurement["area_m2"],
    }
    return terminal


def terminal_from_record(record):
    """Resolve native identities for a Python-validated terminal record only."""
    try:
        face = LIVE_OBJECTS[record["support_face_id"]]
        edges = [LIVE_OBJECTS[edge_id] for edge_id in record["edge_ids"]]
    except KeyError:
        raise ValueError("Validated opening record no longer matches the CAD object identity")
    if not edges:
        raise ValueError("Validated opening record has no boundary edges")
    return edges, _terminal_record(record, face, edges)


def existing_terminal_from_record(record):
    """Reuse the confirmed source-face identity without temporary cap geometry."""
    try:
        face = LIVE_OBJECTS[record["support_face_id"]]
        edges = [LIVE_OBJECTS[edge_id] for edge_id in record["edge_ids"]]
    except KeyError:
        raise ValueError("Validated opening record no longer matches the CAD object identity")
    return {
        "name": record["name"],
        "role": record["role"],
        "source_candidate_id": record["source_candidate_id"],
        "source_loop_id": record.get("contour_loop_id"),
        "source_face_moniker": moniker_of(face),
        "boundary_edge_count": len(edges),
        "boundary_perimeter_m": sum(float(edge.Shape.Length) for edge in edges),
    }


try:
    operation = build_request["operation"]
    DocumentOpen.Execute(build_request["input"])

    if operation == "extract_volume":
        catalog = build_catalog()
        terminal_records = build_request["terminal_records"]
        seed_face_id = build_request["seed_face_id"]
        requested = [seed_face_id]
        for terminal_record in terminal_records:
            requested.extend([
                terminal_record["source_candidate_id"],
                terminal_record["support_face_id"],
            ])
            requested.extend(terminal_record["edge_ids"])
        validate_catalog_identity(build_request["catalog"], catalog, requested)

        positive_bodies = [
            body for body in catalog["public"]["bodies"]
            if body.get("kind") == "solid" and (body.get("volume_m3") or 0.0) > 0.0
        ]
        if build_request.get("existing_fluid_body"):
            terminals = [existing_terminal_from_record(record) for record in terminal_records]
            all_bodies = list(DocumentHelper.GetRootPart().GetAllBodies())
            if len(all_bodies) != 1 or len(positive_bodies) != 1:
                raise ValueError(
                    "Existing-fluid-body mode requires exactly one positive-volume solid body")
            fluid = LIVE_OBJECTS[positive_bodies[0]["id"]]
            seed_face = LIVE_OBJECTS[seed_face_id]
            if seed_face.Parent != fluid:
                raise ValueError("The selected seed face is not on the existing fluid body")
            seed_center = MeasureHelper.GetCentroid(Selection.Create(seed_face))
            seed_point = seed_face.Shape.Geometry.ProjectPoint(seed_center).Point
            free_edges = [
                edge["id"] for edge in catalog["public"]["edges"]
                if edge.get("body_id") == positive_bodies[0]["id"]
                if len(edge["face_ids"]) != 2
            ]
            if free_edges:
                raise ValueError(
                    "Existing fluid body contains free edges: " + str(free_edges))
            DocumentSave.Execute(build_request["output"])
            build_result["transfer"] = {
                "source_mode": "existing_fluid_body",
                "terminals": terminals,
                "seed_point_m": vector3(seed_point),
                "volume_m3": float(fluid.Shape.Volume),
                "face_count": int(fluid.Faces.Count),
                "free_edges": free_edges,
            }
            record("existing_fluid_body", build_result["transfer"])
            save_picture("existing-fluid")
        else:
            cap_edges = []
            terminals = []
            for terminal_record in terminal_records:
                edges, terminal = terminal_from_record(terminal_record)
                cap_edges.extend(edges)
                terminals.append(terminal)
            seed_face = LIVE_OBJECTS[seed_face_id]
            seed_center = MeasureHelper.GetCentroid(Selection.Create(seed_face))
            seed_point = seed_face.Shape.Geometry.ProjectPoint(seed_center).Point
            options = VolumeExtractOptions()
            options.SeedPoint = seed_face.Shape.Geometry.ProjectPoint(seed_center)
            options.CreateShareTopology = False
            strategy = build_request["extraction_strategy"]
            if strategy == "faces":
                if not all(record.get("face_cap_supported") for record in terminal_records):
                    raise ValueError("Face capping was requested for a non-equivalent opening contour")
                cap_faces = [LIVE_OBJECTS[record["support_face_id"]] for record in terminal_records]
                extraction = VolumeExtract.Create(
                    Selection.Create(cap_faces), Selection.Create(seed_face), options)
            elif strategy == "edges":
                extraction = VolumeExtract.Create(
                    Selection.Create(cap_edges), Selection.Create(seed_face), options)
            else:
                raise ValueError("Unsupported extraction strategy: " + str(strategy))
            volumes = list(extraction.CreatedVolumes)
            if not extraction.Success or len(volumes) != 1 or volumes[0].Shape.Volume <= 0:
                raise ValueError(
                    "VolumeExtract did not create exactly one positive fluid volume "
                    "(success=%s, created_volumes=%s, cap_edges=%s, seed_face=%s)" % (
                        extraction.Success, len(volumes), len(cap_edges),
                        seed_face_id))
            fluid = volumes[0]
            for group in list(Window.ActiveWindow.Groups):
                group.Delete()
            others = [body for body in DocumentHelper.GetRootPart().GetAllBodies() if body != fluid]
            if others:
                Delete.Execute(Selection.Create(others))
            RenameObject.Execute(Selection.Create(fluid), "fluid")
            extracted_catalog = build_catalog()
            free_edges = [
                edge["id"] for edge in extracted_catalog["public"]["edges"]
                if len(edge["face_ids"]) != 2
            ]
            if free_edges:
                raise ValueError("Extracted fluid body contains free edges: " + str(free_edges))
            DocumentSave.Execute(build_request["output"])
            build_result["transfer"] = {
                "source_mode": "volume_extract",
                "extraction_strategy": strategy,
                "terminals": terminals,
                "seed_point_m": vector3(seed_point),
                "volume_m3": float(fluid.Shape.Volume),
                "face_count": int(fluid.Faces.Count),
                "free_edges": free_edges,
            }
            record("extract_volume", build_result["transfer"])
            save_picture("extracted-fluid")

    elif operation == "label_faces":
        transfer = build_request["extraction"]["transfer"]
        fluid_bodies = [body for body in DocumentHelper.GetRootPart().GetAllBodies() if body.Shape.Volume > 0]
        if len(fluid_bodies) != 1:
            raise ValueError("Expected exactly one positive fluid body before grouping")
        fluid = fluid_bodies[0]
        for group in list(Window.ActiveWindow.Groups):
            group.Delete()

        assigned = []
        groups = []
        for terminal in transfer["terminals"]:
            matches = []
            if transfer.get("source_mode") == "existing_fluid_body":
                # Reusing an already-fluid body must not re-identify a face
                # from similar geometry. Its native face identity is the
                # evidence that the confirmed selection survived.
                matches = [
                    face for face in fluid.Faces
                    if moniker_of(face) == terminal["source_face_moniker"]
                ]
            else:
                target_area = terminal["area_m2"]
                target_perimeter = terminal["boundary_perimeter_m"]
                for face in fluid.Faces:
                    if not isinstance(face.Shape.Geometry, Plane):
                        continue
                    point = vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
                    delta = [point[i] - terminal["center_m"][i] for i in range(3)]
                    direction = vector3(face.Shape.Geometry.Frame.DirZ)
                    alignment = abs(sum(direction[i] * terminal["normal"][i] for i in range(3)))
                    if (
                        sum(value * value for value in delta) ** 0.5
                        < max(1e-7, target_perimeter * 1e-6)
                        and abs(alignment - 1.0) < 1e-6
                        and abs(face.Perimeter - target_perimeter)
                        < max(1e-8, target_perimeter * 1e-5)
                        and abs(face.Area - target_area)
                        < max(1e-8, target_area * 1e-5)
                    ):
                        matches.append(face)
            if len(matches) != 1 or matches[0] in assigned:
                candidate_measurements = []
                for face in fluid.Faces:
                    if not isinstance(face.Shape.Geometry, Plane):
                        continue
                    point = vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
                    normal = plane_normal(face)
                    delta = [point[i] - terminal["center_m"][i] for i in range(3)]
                    candidate_measurements.append({
                        "face_moniker": moniker_of(face),
                        "area_m2": float(face.Area),
                        "perimeter_m": float(face.Perimeter),
                        "center_m": point,
                        "normal": normal,
                        "center_distance_m": sum(value * value for value in delta) ** 0.5,
                        "area_difference_m2": abs(float(face.Area) - terminal["area_m2"]),
                        "perimeter_difference_m": abs(
                            float(face.Perimeter) - terminal["boundary_perimeter_m"]),
                        "normal_alignment": abs(sum(
                            normal[i] * terminal["normal"][i] for i in range(3))),
                    })
                failure_evidence["boundary_mapping"] = {
                    "terminal": terminal["name"],
                    "matching_face_count": len(matches),
                    "candidate_faces": candidate_measurements,
                }
                raise ValueError("Cannot uniquely map extracted cap for " + terminal["name"])
            assigned.append(matches[0])
            groups.append((terminal["name"], terminal["role"], matches))

        remaining = [face for face in fluid.Faces if face not in assigned]
        if remaining:
            groups.append(("wall", "wall", remaining))
        if len(set(name for name, role, faces in groups)) != len(groups):
            raise ValueError("Boundary group names are not unique")
        for name, role, faces in groups:
            Group.Create(DocumentHelper.GetRootPart(), name, List[IDocObject](faces))
        group_records = [
            {
                "name": name,
                "role": role,
                "count": len(faces),
                "member_monikers": [moniker_of(face) for face in faces],
            }
            for name, role, faces in groups
        ]
        coverage = sum(item["count"] for item in group_records)
        if coverage != fluid.Faces.Count:
            raise ValueError("Boundary groups do not cover every fluid face")
        final_catalog = build_catalog()
        DocumentSave.Execute(build_request["output"])
        build_result["groups"] = group_records
        build_result["catalog"] = final_catalog
        build_result["coverage"] = coverage
        build_result["total_faces"] = int(fluid.Faces.Count)
        record("label_faces", {"groups": group_records, "coverage": coverage})
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
    elif "cleanup" in message or "delete.execute" in message:
        code = "CAD_TEMPORARY_CLEANUP_FAILED"
    elif "license" in message:
        code = "CAD_LICENSE_UNAVAILABLE"
    elif "not planar" in message or "closed opening" in message or "boundary edges" in message:
        code = "CAD_OPENING_INVALID"
    build_result["error_detail"] = {
        "code": code,
        "stage": build_result.get("operation"),
        "reason": str(error) or "SpaceClaim native operation failed without a message",
        "raw_error": build_result["error"],
        "evidence": failure_evidence,
    }

temporary = build_request["response"] + ".tmp"
with open(temporary, "w") as response_stream:
    json.dump(build_result, response_stream, indent=2)
os.rename(temporary, build_request["response"])
