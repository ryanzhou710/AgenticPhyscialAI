# Python Script, API Version = V241
"""SpaceClaim-side volume extraction and boundary grouping operations.

Loads the complete packaged helper file in SpaceClaim's scripting namespace.
Uses model-selected native references, not case-specific object IDs.
"""

import json
import math
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


def terminal_from_selection(port, catalog):
    target = LIVE_OBJECTS[port["candidate_id"]]
    if port["candidate_id"].startswith("E"):
        if target.Faces.Count != 1 or not isinstance(target.Shape.Geometry, Circle):
            raise ValueError("Selected opening edge is not one circular open edge: " + port["name"])
        edge = target
    else:
        if not isinstance(target.Shape.Geometry, Plane):
            raise ValueError("Selected opening face is not planar: " + port["name"])
        inner_loops = [
            loop for loop in catalog["public"]["loops"]
            if loop["face_id"] == port["candidate_id"] and not loop["is_outer"]
        ]
        if len(inner_loops) != 1 or len(inner_loops[0]["edge_ids"]) != 1:
            raise ValueError("Selected opening face does not contain one circular inner loop: " + port["name"])
        edge = LIVE_OBJECTS[inner_loops[0]["edge_ids"][0]]
        if not isinstance(edge.Shape.Geometry, Circle):
            raise ValueError("Selected opening loop is not circular: " + port["name"])
    circle = edge.Shape.Geometry
    return edge, {
        "name": port["name"],
        "role": port["role"],
        "source_candidate_id": port["candidate_id"],
        "center_m": vector3(circle.Frame.Origin),
        "normal": vector3(circle.Frame.DirZ),
        "radius_m": float(circle.Radius),
    }


def direct_terminal_from_selection(port, catalog, fluid_body_id):
    """Resolve an opening face on an already closed fluid solid.

    A closed fluid body has real boundary faces, so the direct path groups the
    selected face itself.  An edge is intentionally rejected here because a
    closed solid edge belongs to two faces and cannot identify one boundary
    zone unambiguously.
    """

    candidate_id = port["candidate_id"]
    public = catalog["public"]
    face_rows = {row["id"]: row for row in public.get("faces", [])}
    loop_rows = {row["id"]: row for row in public.get("loops", [])}
    edge_rows = {row["id"]: row for row in public.get("edges", [])}
    if candidate_id.startswith("F"):
        face_ids = [candidate_id]
    elif candidate_id.startswith("L"):
        loop = loop_rows.get(candidate_id)
        face_ids = [] if loop is None else [loop["face_id"]]
    elif candidate_id.startswith("E"):
        edge = edge_rows.get(candidate_id)
        face_ids = [] if edge is None else list(edge.get("face_ids", []))
        if len(face_ids) != 1:
            raise ValueError(
                "Select the inlet/outlet face (not an edge) for an existing closed fluid solid: "
                + port["name"]
            )
    else:
        face_ids = []
    if len(face_ids) != 1 or face_ids[0] not in face_rows:
        raise ValueError("Opening selection is not one unique face: " + port["name"])
    face_id = face_ids[0]
    face_row = face_rows[face_id]
    if face_row.get("body_id") != fluid_body_id:
        raise ValueError("Opening is not on the detected fluid body: " + port["name"])
    face = LIVE_OBJECTS[face_id]
    refs = catalog.get("internal", {}).get("refs", {})
    moniker = refs.get(face_id, {}).get("moniker")
    if not moniker:
        raise ValueError("Opening face has no stable native reference: " + port["name"])
    return {
        "name": port["name"],
        "role": port["role"],
        "source_candidate_id": candidate_id,
        "source_face_ids": [face_id],
        "face_monikers": [moniker],
        "area_m2": float(face.Area),
        "perimeter_m": float(face.Perimeter),
    }


def direct_fluid_body(catalog):
    bodies = list(DocumentHelper.GetRootPart().GetAllBodies())
    positive = [body for body in bodies if body.Shape.Volume > 0]
    if len(bodies) != 1 or len(positive) != 1:
        raise ValueError("Expected exactly one positive-volume solid body for direct meshing")
    fluid = positive[0]
    free_edges = [edge for edge in fluid.Edges if edge.Faces.Count != 2]
    if free_edges:
        raise ValueError("Existing solid contains free edges; VolumeExtract is required")
    body_id = next(
        (row["id"] for row in catalog["public"].get("bodies", [])
         if catalog["internal"]["refs"].get(row["id"], {}).get("moniker") == moniker_of(fluid)),
        None,
    )
    if body_id is None:
        raise ValueError("Could not map the detected fluid body to the catalog")
    return fluid, body_id, []


try:
    operation = build_request["operation"]
    DocumentOpen.Execute(build_request["input"])

    if operation == "use_existing_fluid":
        catalog = build_catalog()
        plan = build_request["selection_plan"]
        requested = [item["candidate_id"] for item in plan["openings"]]
        requested.append(plan["seed_inner_wall_id"])
        validate_catalog_identity(build_request["catalog"], catalog, requested)
        fluid, fluid_body_id, free_edges = direct_fluid_body(catalog)
        terminals = [
            direct_terminal_from_selection(port, catalog, fluid_body_id)
            for port in plan["openings"]
        ]
        seed_face = LIVE_OBJECTS[plan["seed_inner_wall_id"]]
        if seed_face.Parent != fluid:
            raise ValueError("The selected seed face is not on the detected fluid body")
        for group in list(Window.ActiveWindow.Groups):
            group.Delete()
        RenameObject.Execute(Selection.Create(fluid), "fluid")
        DocumentSave.Execute(build_request["output"])
        build_result["transfer"] = {
            "mode": "existing_solid",
            "terminals": terminals,
            "seed_point_m": vector3(MeasureHelper.GetCentroid(Selection.Create(seed_face))),
            "volume_m3": float(fluid.Shape.Volume),
            "face_count": int(fluid.Faces.Count),
            "free_edges": free_edges,
        }
        record("use_existing_fluid", build_result["transfer"])
        save_picture("existing-fluid")

    elif operation == "extract_volume":
        catalog = build_catalog()
        plan = build_request["selection_plan"]
        requested = [item["candidate_id"] for item in plan["openings"]]
        requested.append(plan["seed_inner_wall_id"])
        validate_catalog_identity(build_request["catalog"], catalog, requested)

        cap_edges = []
        terminals = []
        for port in plan["openings"]:
            edge, terminal = terminal_from_selection(port, catalog)
            cap_edges.append(edge)
            terminals.append(terminal)
        seed_face = LIVE_OBJECTS[plan["seed_inner_wall_id"]]
        if plan["seed_inner_wall_id"].startswith("F") is False:
            raise ValueError("The fluid-volume seed must be a face")
        seed_center = MeasureHelper.GetCentroid(Selection.Create(seed_face))
        seed_point = seed_face.Shape.Geometry.ProjectPoint(seed_center).Point
        options = VolumeExtractOptions()
        options.SeedPoint = seed_face.Shape.Geometry.ProjectPoint(seed_center)
        options.CreateShareTopology = False
        extraction = VolumeExtract.Create(Selection.Create(cap_edges), Selection.Empty(), options)
        volumes = list(extraction.CreatedVolumes)
        if not extraction.Success or len(volumes) != 1 or volumes[0].Shape.Volume <= 0:
            raise ValueError("VolumeExtract did not create exactly one positive fluid volume")
        fluid = volumes[0]
        for group in list(Window.ActiveWindow.Groups):
            group.Delete()
        others = [body for body in DocumentHelper.GetRootPart().GetAllBodies() if body != fluid]
        if others:
            Delete.Execute(Selection.Create(others))
        RenameObject.Execute(Selection.Create(fluid), "fluid")
        DocumentSave.Execute(build_request["output"])
        extracted_catalog = build_catalog()
        free_edges = [
            edge["id"] for edge in extracted_catalog["public"]["edges"]
            if len(edge["face_ids"]) != 2
        ]
        if free_edges:
            raise ValueError("Extracted fluid body contains free edges: " + str(free_edges))
        build_result["transfer"] = {
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
            if terminal.get("face_monikers"):
                expected = set(terminal["face_monikers"])
                matches = [face for face in fluid.Faces if moniker_of(face) in expected]
            else:
                matches = []
                target_area = math.pi * terminal["radius_m"] * terminal["radius_m"]
                for face in fluid.Faces:
                    if not isinstance(face.Shape.Geometry, Plane):
                        continue
                    point = vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
                    delta = [point[i] - terminal["center_m"][i] for i in range(3)]
                    direction = vector3(face.Shape.Geometry.Frame.DirZ)
                    alignment = abs(sum(direction[i] * terminal["normal"][i] for i in range(3)))
                    if (
                        sum(value * value for value in delta) ** 0.5 < 1e-5
                        and abs(alignment - 1.0) < 1e-6
                        and abs(face.Area - target_area) < max(1e-10, target_area * 1e-5)
                    ):
                        matches.append(face)
            if len(matches) != 1 or matches[0] in assigned:
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
        DocumentSave.Execute(build_request["output"])
        final_catalog = build_catalog()
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
except Exception:
    build_result["error"] = traceback.format_exc()

temporary = build_request["response"] + ".tmp"
with open(temporary, "w") as response_stream:
    json.dump(build_result, response_stream, indent=2)
os.rename(temporary, build_request["response"])
