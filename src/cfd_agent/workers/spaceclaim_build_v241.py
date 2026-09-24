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


def _public_row(catalog, collection, candidate_id):
    for row in catalog["public"].get(collection, []):
        if row["id"] == candidate_id:
            return row
    return None


def _face_loops(catalog, face_id):
    return [row for row in catalog["public"].get("loops", [])
            if row["face_id"] == face_id]


def _boundary_edges(loop):
    edge_ids = loop.get("edge_ids") or []
    if not edge_ids:
        raise ValueError("Selected opening loop has no edges")
    try:
        return [LIVE_OBJECTS[edge_id] for edge_id in edge_ids]
    except KeyError:
        raise ValueError("Selected opening loop contains an unknown edge")


def _support_face(port, catalog):
    candidate_id = port["candidate_id"]
    if candidate_id.startswith("F"):
        return LIVE_OBJECTS[candidate_id]
    if candidate_id.startswith("L"):
        loop = _public_row(catalog, "loops", candidate_id)
        face_id = loop.get("face_id") if loop else None
        if face_id:
            return LIVE_OBJECTS[face_id]
    if candidate_id.startswith("E"):
        edge = LIVE_OBJECTS[candidate_id]
        if edge.Faces.Count == 1:
            return list(edge.Faces)[0]
    return None


def _face_can_cap_opening(port, catalog):
    """Return whether selecting the support face caps only this opening loop."""
    candidate_id = port["candidate_id"]
    if candidate_id.startswith("F"):
        loops = _face_loops(catalog, candidate_id)
    elif candidate_id.startswith("L"):
        loop = _public_row(catalog, "loops", candidate_id)
        face_id = loop.get("face_id") if loop else None
        loops = _face_loops(catalog, face_id) if face_id else []
    else:
        return False
    return len(loops) == 1 and bool(loops[0].get("is_outer"))


def _terminal_record(port, face, loop, edges):
    if not isinstance(face.Shape.Geometry, Plane):
        raise ValueError("Selected opening support face is not planar: " + port["name"])
    # Use SpaceClaim's actual support-face centroid. Averaging edge midpoints
    # changes when a contour is split into segments and is not stable for
    # irregular openings.
    center = vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
    perimeter = sum(float(edge.Shape.Length) for edge in edges)
    terminal = {
        "name": port["name"],
        "role": port["role"],
        "source_candidate_id": port["candidate_id"],
        "source_loop_id": loop["id"],
        "boundary_edge_count": len(edges),
        "boundary_perimeter_m": perimeter,
        "center_m": center,
        "normal": plane_normal(face),
    }
    # Area is exact for a planar face used directly as an opening, and for a
    # circular inner loop.  General inner-loop areas are deliberately optional:
    # the loop perimeter and centroid are enough to identify the extracted cap
    # without assuming a particular primitive shape.
    if loop.get("is_outer"):
        terminal["area_m2"] = float(face.Area)
    elif len(edges) == 1 and isinstance(edges[0].Shape.Geometry, Circle):
        terminal["area_m2"] = math.pi * float(edges[0].Shape.Geometry.Radius) ** 2
    return terminal


def terminal_from_selection(port, catalog):
    candidate_id = port["candidate_id"]
    if candidate_id.startswith("F"):
        face = LIVE_OBJECTS[candidate_id]
        loops = _face_loops(catalog, candidate_id)
        inner_loops = [loop for loop in loops if not loop.get("is_outer")]
        outer_loops = [loop for loop in loops if loop.get("is_outer")]
        if len(inner_loops) == 1:
            loop = inner_loops[0]
        elif not inner_loops and len(outer_loops) == 1:
            # A solid with a flush end is represented by its planar end face;
            # its outer loop is the complete inlet/outlet boundary.
            loop = outer_loops[0]
        else:
            raise ValueError(
                "Selected opening face must have exactly one closed opening loop: "
                + port["name"])
        edges = _boundary_edges(loop)
        return edges, _terminal_record(port, face, loop, edges)

    if candidate_id.startswith("L"):
        loop = _public_row(catalog, "loops", candidate_id)
        if loop is None or not loop.get("closed"):
            raise ValueError("Selected opening loop is not a closed loop: " + port["name"])
        face = LIVE_OBJECTS.get(loop.get("face_id"))
        if face is None:
            raise ValueError("Selected opening loop has no support face: " + port["name"])
        edges = _boundary_edges(loop)
        return edges, _terminal_record(port, face, loop, edges)

    if candidate_id.startswith("E"):
        edge = LIVE_OBJECTS[candidate_id]
        geometry = edge.Shape.Geometry
        closed = bool(getattr(edge.Shape, "IsClosed", False))
        if isinstance(geometry, Circle):
            closed = True
        if edge.Faces.Count != 1 or not closed:
            raise ValueError(
                "Selected opening edge must be a single closed boundary edge: "
                + port["name"])
        face = list(edge.Faces)[0]
        loop = {
            "id": None,
            "is_outer": False,
            "closed": True,
            "edge_ids": [candidate_id],
        }
        return [edge], _terminal_record(port, face, loop, [edge])

    raise ValueError("Unsupported opening candidate: " + candidate_id)


try:
    operation = build_request["operation"]
    DocumentOpen.Execute(build_request["input"])

    if operation == "extract_volume":
        catalog = build_catalog()
        plan = build_request["selection_plan"]
        requested = [item["candidate_id"] for item in plan["openings"]]
        requested.append(plan["seed_inner_wall_id"])
        boundaries = build_request["terminal_boundaries"]
        for boundary in boundaries.values():
            requested.extend(boundary["edge_ids"])
        validate_catalog_identity(build_request["catalog"], catalog, requested)

        terminals = []
        for port in plan["openings"]:
            edges, terminal = terminal_from_selection(port, catalog)
            terminals.append(terminal)

        positive_bodies = [
            body for body in catalog["public"]["bodies"]
            if body.get("kind") == "solid" and (body.get("volume_m3") or 0.0) > 0.0
        ]
        if build_request.get("existing_fluid_body"):
            all_bodies = list(DocumentHelper.GetRootPart().GetAllBodies())
            if len(all_bodies) != 1 or len(positive_bodies) != 1:
                raise ValueError(
                    "Existing-fluid-body mode requires exactly one positive-volume solid body")
            fluid = LIVE_OBJECTS[positive_bodies[0]["id"]]
            seed_face = LIVE_OBJECTS[plan["seed_inner_wall_id"]]
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
            for port in plan["openings"]:
                edges, _ = terminal_from_selection(port, catalog)
                cap_edges.extend(edges)
            seed_face = LIVE_OBJECTS[plan["seed_inner_wall_id"]]
            if plan["seed_inner_wall_id"].startswith("F") is False:
                raise ValueError("The fluid-volume seed must be a face")
            seed_center = MeasureHelper.GetCentroid(Selection.Create(seed_face))
            seed_point = seed_face.Shape.Geometry.ProjectPoint(seed_center).Point
            options = VolumeExtractOptions()
            options.SeedPoint = seed_face.Shape.Geometry.ProjectPoint(seed_center)
            options.CreateShareTopology = False
            # V241's face-selection mode is the stable API path for flush end
            # faces. It lets SpaceClaim derive and cap every edge in each
            # selected planar terminal, including multi-edge rectangular loops.
            cap_faces = []
            if all(_face_can_cap_opening(port, catalog) for port in plan["openings"]):
                for port in plan["openings"]:
                    face = _support_face(port, catalog)
                    if face is None:
                        cap_faces = []
                        break
                    cap_faces.append(face)
            if cap_faces:
                extraction = VolumeExtract.Create(
                    Selection.Create(cap_faces), Selection.Create(seed_face), options)
            else:
                extraction = VolumeExtract.Create(
                    Selection.Create(cap_edges), Selection.Create(seed_face), options)
            volumes = list(extraction.CreatedVolumes)
            if not extraction.Success or len(volumes) != 1 or volumes[0].Shape.Volume <= 0:
                raise ValueError(
                    "VolumeExtract did not create exactly one positive fluid volume "
                    "(success=%s, created_volumes=%s, cap_edges=%s, seed_face=%s)" % (
                        extraction.Success, len(volumes), len(cap_edges),
                        plan["seed_inner_wall_id"]))
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
                "source_mode": "volume_extract",
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
            target_area = terminal.get("area_m2", terminal.get("opening_area_m2"))
            target_perimeter = terminal["boundary_perimeter_m"]
            for face in fluid.Faces:
                if not isinstance(face.Shape.Geometry, Plane):
                    continue
                point = vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
                delta = [point[i] - terminal["center_m"][i] for i in range(3)]
                direction = vector3(face.Shape.Geometry.Frame.DirZ)
                alignment = abs(sum(direction[i] * terminal["normal"][i] for i in range(3)))
                if (
                    sum(value * value for value in delta) ** 0.5 < max(1e-7, target_perimeter * 1e-6)
                    and abs(alignment - 1.0) < 1e-6
                    and abs(face.Perimeter - target_perimeter)
                    < max(1e-8, target_perimeter * 1e-5)
                    and (
                        target_area is None
                        or abs(face.Area - target_area)
                        < max(1e-8, target_area * 1e-5)
                    )
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
