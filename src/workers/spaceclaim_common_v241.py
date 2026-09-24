# Python Script, API Version = V241
"""Shared V241 geometry functions, loaded in the native scripting namespace.

The entrypoints load this complete file with IronPython execfile so built-in
SpaceClaim script bindings remain available. No source text is sliced at runtime.
"""

import json
import os
import traceback

# This marker is written before importing any SpaceClaim API assembly.  A
# preserved no-response bundle can therefore distinguish "script never began"
# from a failure inside API bootstrap.
_EARLY_REQUEST_PATH = (os.environ.get("SPACECLAIM_GROUNDING_REQUEST") or os.environ.get("CFD_AGENT_SC_BUILD_REQUEST"))
if _EARLY_REQUEST_PATH and os.path.isfile(_EARLY_REQUEST_PATH):
    with open(_EARLY_REQUEST_PATH, "r") as _early_stream:
        _early_request = json.load(_early_stream)
    _started_path = _early_request.get("started")
    if _started_path:
        with open(_started_path, "w") as _started_stream:
            _started_stream.write("cfd_agent_spaceclaim_script_started\n")

import clr

# Importing the concrete design-object and Moniker types also initialises the
# typed selection bindings in SpaceClaim's IronPython host.  If a V241 API
# binding fails before the main request handler starts, still emit a response
# so the Windows runner can report the real exception instead of "no response".
try:
    import SpaceClaim.Api.V241.Scripting.Extensions as scripting_extensions
    clr.ImportExtensions(scripting_extensions)
    from SpaceClaim.Api.V241 import (
        DesignBody, DesignCurve, DesignEdge, DesignFace, LineWeight, LineWeightType,
        Moniker, Window, WindowExportFormat)
    from SpaceClaim.Api.V241.Geometry import Circle, Frame, Matrix, Plane
    from SpaceClaim.Api.V241.Modeler import Body as ModelerBody
    from SpaceClaim.Api.V241.Scripting.Commands import DocumentOpen
    from SpaceClaim.Api.V241.Scripting.Commands.CommandOptions import (
        FaceColorTarget, SetColorOptions)
    from SpaceClaim.Api.V241.Scripting.Helpers import (
        ColorHelper, DocumentHelper, MeasureHelper, ViewHelper)
    from SpaceClaim.Api.V241.Scripting.Selection import Selection
    from System.Drawing import Bitmap, Color
    from System.Threading import Thread

    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import Application as FormsApplication
except Exception:
    bootstrap_path = (os.environ.get("SPACECLAIM_GROUNDING_REQUEST") or os.environ.get("CFD_AGENT_SC_BUILD_REQUEST"))
    if bootstrap_path and os.path.isfile(bootstrap_path):
        with open(bootstrap_path, "r") as bootstrap_stream:
            bootstrap_request = json.load(bootstrap_stream)
        bootstrap_response = {
            "ok": False,
            "run_id": bootstrap_request.get("run_id"),
            "operation": bootstrap_request.get("operation"),
            "ui_mode": bootstrap_request.get("ui_mode"),
            "error": traceback.format_exc(),
        }
        with open(bootstrap_request["response"], "w") as bootstrap_stream:
            json.dump(bootstrap_response, bootstrap_stream, indent=2)
    raise


SCHEMA_VERSION = 1
DIRECTION_REFERENCE_VIEW = "Front"
AUXILIARY_VIEWS = ["Top", "Right", "Isometric"]
DEFAULT_VIEWS = [DIRECTION_REFERENCE_VIEW] + AUXILIARY_VIEWS
ALLOWED_VIEWS = set([
    "Front", "Back", "Top", "Bottom", "Right", "Left", "Isometric"])
VIEW_ROLES = {
    "Front": "direction_reference",
    "Top": "auxiliary",
    "Right": "auxiliary",
    "Isometric": "auxiliary",
    "Back": "auxiliary",
    "Bottom": "auxiliary",
    "Left": "auxiliary",
    "Selected": "selection_detail",
    "SelectedProxy": "selection_detail_verified_proxy",
}
VIEW_CONTRACT = {
    "coordinate_frame": "spaceclaim_global",
    "direction_reference": DIRECTION_REFERENCE_VIEW,
    "auxiliary_views": list(AUXILIARY_VIEWS),
    "selection_detail": "Selected",
    "selected_projection": "face_normal_or_curve_frame_with_explicit_extent_or_isometric",
}
LIVE_OBJECTS = {}
try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


def vector3(value):
    return [float(value.X), float(value.Y), float(value.Z)]


def bbox(shape):
    box = shape.GetBoundingBox(Matrix.Identity)
    return {"min": vector3(box.MinCorner), "max": vector3(box.MaxCorner),
            "center": vector3(box.Center)}


def number(value):
    try:
        return float(value)
    except Exception:
        return None


def moniker_of(obj):
    return obj.Moniker.ToString()


def master_of(obj):
    master = getattr(obj, "Master", None)
    return obj if master is None else master


def parent_path(obj):
    result = []
    current = getattr(obj, "Parent", None)
    while current is not None:
        name = getattr(current, "Name", None)
        if name:
            result.insert(0, name)
        current = getattr(current, "Parent", None)
    return result


def primitive_data(geometry):
    result = {}
    frame = getattr(geometry, "Frame", None)
    if frame is not None:
        result["axis_origin_m"] = vector3(frame.Origin)
        result["axis_direction"] = vector3(frame.DirZ)
    radius = getattr(geometry, "Radius", None)
    if radius is not None:
        result["radius_m"] = number(radius)
    half_angle = getattr(geometry, "HalfAngle", None)
    if half_angle is not None:
        result["half_angle_rad"] = number(half_angle)
    return result


def plane_normal(face):
    geometry = face.Shape.Geometry
    if not isinstance(geometry, Plane):
        return None
    value = vector3(geometry.Frame.DirZ)
    if face.Shape.IsReversed:
        value = [-item for item in value]
    return value


def centroid(face):
    try:
        return vector3(MeasureHelper.GetCentroid(Selection.Create(face)))
    except Exception:
        return None


def sorted_objects(objects):
    return sorted(list(objects), key=lambda obj: moniker_of(obj))


def scalar_key(value):
    measured = number(value)
    return "" if measured is None else "%.12g" % measured


def vector_key(value):
    return tuple([scalar_key(item) for item in value])


def bbox_key(shape):
    box = bbox(shape)
    return (vector_key(box["min"]), vector_key(box["max"]),
            vector_key(box["center"]))


def body_sort_key(body):
    # SpaceClaim hosts Python 2: str(.NET String) uses a byte encoding and can
    # fail on imported Unicode names. Keep the native name as Unicode.
    return (tuple(parent_path(body)), unicode(body.Name), scalar_key(body.Shape.Volume),
            bbox_key(body.Shape), int(body.Faces.Count), int(body.Edges.Count))


def face_sort_key(face, body_ids):
    geometry = face.Shape.Geometry
    primitives = primitive_data(geometry)
    return (body_ids.get(moniker_of(face.Parent), ""), geometry.GetType().Name,
            scalar_key(face.Area), scalar_key(face.Perimeter), bbox_key(face.Shape),
            vector_key(centroid(face) or []), vector_key(plane_normal(face) or []),
            scalar_key(primitives.get("radius_m")), int(face.Edges.Count))


def edge_sort_key(edge, body_ids):
    geometry = edge.Shape.Geometry
    ends = sorted([vector_key(vector3(edge.Shape.StartPoint)),
                   vector_key(vector3(edge.Shape.EndPoint))])
    primitives = primitive_data(geometry)
    return (body_ids.get(moniker_of(edge.Parent), ""), geometry.GetType().Name,
            scalar_key(edge.Shape.Length), bbox_key(edge.Shape), tuple(ends),
            scalar_key(primitives.get("radius_m")), int(edge.Faces.Count))


def assign_ids(prefix, objects, sort_key):
    by_id = {}
    id_by_moniker = {}
    for index, obj in enumerate(sorted(list(objects), key=sort_key)):
        candidate_id = "%s%04d" % (prefix, index + 1)
        by_id[candidate_id] = obj
        id_by_moniker[moniker_of(obj)] = candidate_id
    return by_id, id_by_moniker


def candidate_ids(objects, id_by_moniker):
    result = []
    for obj in objects:
        candidate_id = id_by_moniker.get(moniker_of(obj))
        if candidate_id is not None:
            result.append(candidate_id)
    return sorted(set(result))


def build_catalog():
    global LIVE_OBJECTS
    root = DocumentHelper.GetRootPart()
    bodies = sorted_objects(root.GetAllBodies())
    faces = []
    edges = []
    for body in bodies:
        faces.extend(list(body.Faces))
        edges.extend(list(body.Edges))

    bodies_by_id, body_ids = assign_ids("B", bodies, body_sort_key)
    faces_by_id, face_ids = assign_ids(
        "F", faces, lambda face: face_sort_key(face, body_ids))
    edges_by_id, edge_ids = assign_ids(
        "E", edges, lambda edge: edge_sort_key(edge, body_ids))
    LIVE_OBJECTS = {}
    LIVE_OBJECTS.update(bodies_by_id)
    LIVE_OBJECTS.update(faces_by_id)
    LIVE_OBJECTS.update(edges_by_id)

    all_ids = {}
    all_ids.update(body_ids)
    all_ids.update(face_ids)
    all_ids.update(edge_ids)
    refs = {}
    raw_names = {}
    public_bodies = []
    public_faces = []
    public_edges = []
    face_rows = {}

    for candidate_id in sorted(bodies_by_id.keys()):
        body = bodies_by_id[candidate_id]
        refs[candidate_id] = {"kind": "body", "moniker": moniker_of(body)}
        raw_names[candidate_id] = {
            "raw_name": body.Name, "raw_parent_path": parent_path(body)}
        volume = number(body.Shape.Volume)
        public_bodies.append({
            "id": candidate_id,
            "kind": "solid" if volume is not None and volume > 0.0 else "sheet",
            "volume_m3": volume,
            "bbox_m": bbox(body.Shape),
            "face_ids": candidate_ids(body.Faces, face_ids),
            "edge_ids": candidate_ids(body.Edges, edge_ids),
        })

    for candidate_id in sorted(faces_by_id.keys()):
        face = faces_by_id[candidate_id]
        refs[candidate_id] = {"kind": "face", "moniker": moniker_of(face)}
        raw_names[candidate_id] = {
            "raw_parent_body_name": face.Parent.Name,
            "raw_parent_path": parent_path(face)}
        geometry = face.Shape.Geometry
        row = {
            "id": candidate_id,
            "body_id": body_ids.get(moniker_of(face.Parent)),
            "surface_type": geometry.GetType().Name,
            "area_m2": number(face.Area),
            "perimeter_m": number(face.Perimeter),
            "bbox_m": bbox(face.Shape),
            "centroid_m": centroid(face),
            "edge_ids": candidate_ids(face.Edges, edge_ids),
            "adjacent_face_ids": candidate_ids(face.AdjacentFaces, face_ids),
            "loop_ids": [],
        }
        normal = plane_normal(face)
        if normal is not None:
            row["normal"] = normal
        row.update(primitive_data(geometry))
        face_rows[candidate_id] = row
        public_faces.append(row)

    for candidate_id in sorted(edges_by_id.keys()):
        edge = edges_by_id[candidate_id]
        refs[candidate_id] = {"kind": "edge", "moniker": moniker_of(edge)}
        raw_names[candidate_id] = {
            "raw_parent_body_name": edge.Parent.Name,
            "raw_parent_path": parent_path(edge)}
        geometry = edge.Shape.Geometry
        row = {
            "id": candidate_id,
            "body_id": body_ids.get(moniker_of(edge.Parent)),
            "curve_type": geometry.GetType().Name,
            "length_m": number(edge.Shape.Length),
            "bbox_m": bbox(edge.Shape),
            "start_m": vector3(edge.Shape.StartPoint),
            "end_m": vector3(edge.Shape.EndPoint),
            "face_ids": candidate_ids(edge.Faces, face_ids),
        }
        if isinstance(geometry, Circle):
            row.update(primitive_data(geometry))
        public_edges.append(row)

    pending_loops = []
    degenerate_loops = []
    for face_id in sorted(faces_by_id.keys()):
        face = faces_by_id[face_id]
        current_edges_by_master = dict([
            (moniker_of(master_of(edge)), edge) for edge in face.Edges])
        # GetAllBodies returns occurrence wrappers (DesignFaceGeneral), whose
        # Shape is only ITrimmedSurface.  Native topology lives on the master
        # DesignFace's Modeler.Face.
        master_face = master_of(face)
        master_body = master_face.Parent
        for modeler_loop in master_face.Shape.Loops:
            design_edges = []
            for modeler_edge in modeler_loop.Edges:
                master_edge = master_body.GetDesignEdge(modeler_edge)
                design_edge = (None if master_edge is None else
                               current_edges_by_master.get(moniker_of(master_edge)))
                if design_edge is None:
                    raise ValueError(
                        "Native loop edge has no occurrence DesignEdge in its parent body")
                design_edges.append(design_edge)
            if not design_edges:
                degenerate_loops.append({
                    "face_id": face_id,
                    "reason": "native_zero_edge_loop",
                })
                continue
            loop_edge_ids = candidate_ids(design_edges, edge_ids)
            if len(loop_edge_ids) != len(design_edges):
                raise ValueError("Native loop contains an unmapped DesignEdge")
            pending_loops.append({
                "face_id": face_id,
                "body_id": face_rows[face_id]["body_id"],
                "is_outer": bool(modeler_loop.IsOuter),
                "outer_classification": "spaceclaim_native",
                "closed": True,
                "length_m": sum([
                    number(edge.Shape.Length) or 0.0 for edge in design_edges]),
                "edge_ids": loop_edge_ids,
            })
    pending_loops.sort(key=lambda row: (
        row["face_id"], not row["is_outer"], ",".join(row["edge_ids"])))
    public_loops = []
    loop_refs = {}
    for index, row in enumerate(pending_loops):
        loop_id = "L%04d" % (index + 1)
        row["id"] = loop_id
        public_loops.append(row)
        face_rows[row["face_id"]]["loop_ids"].append(loop_id)
        loop_refs[loop_id] = {
            "kind": "loop", "face_id": row["face_id"],
            "body_id": row["body_id"],
            "edge_ids": list(row["edge_ids"])}

    public_groups = []
    raw_groups = []
    groups = sorted(list(Window.ActiveWindow.Groups), key=lambda item: moniker_of(item))
    for index, group in enumerate(groups):
        group_id = "G%04d" % (index + 1)
        members = candidate_ids(group.Members, all_ids)
        public_groups.append({"id": group_id, "member_ids": members})
        raw_groups.append({
            "id": group_id, "raw_name": group.Name,
            "moniker": moniker_of(group), "member_ids": members})

    return {
        "schema_version": SCHEMA_VERSION,
        "public": {
            "coordinate_unit": "m",
            "bodies": public_bodies,
            "faces": public_faces,
            "edges": public_edges,
            "loops": public_loops,
            "groups": public_groups,
        },
        "internal": {
            "document_path": Window.ActiveWindow.Document.Path,
            "refs": refs,
            "loop_refs": loop_refs,
            "raw_names": raw_names,
            "raw_groups": raw_groups,
            "degenerate_loops": degenerate_loops,
        },
    }


def load_catalog(request):
    if request.get("catalog") is not None:
        return request["catalog"]
    path = request.get("catalog_path")
    if not path:
        raise ValueError("select requires catalog or catalog_path")
    with open(path, "r") as stream:
        value = json.load(stream)
    return value.get("catalog", value)


def stable_value(value):
    if isinstance(value, float):
        return scalar_key(value)
    if isinstance(value, dict):
        return dict((key, stable_value(value[key])) for key in sorted(value.keys()))
    if isinstance(value, list):
        return [stable_value(item) for item in value]
    return value


def geometry_signature(catalog):
    public = catalog.get("public", {})
    payload = dict((collection, public.get(collection, []))
                   for collection in ("bodies", "faces", "edges", "loops"))
    return json.dumps(stable_value(payload), sort_keys=True, separators=(",", ":"))


def validate_catalog_identity(supplied_catalog, current_catalog, requested_ids):
    if geometry_signature(supplied_catalog) != geometry_signature(current_catalog):
        raise ValueError("Geometry or topology changed since the candidate catalog was built")
    supplied_expanded = expand_candidate_ids(requested_ids, supplied_catalog)
    current_expanded = expand_candidate_ids(requested_ids, current_catalog)
    if supplied_expanded != current_expanded:
        raise ValueError("Candidate topology changed since the catalog was built")
    supplied_refs = supplied_catalog["internal"]["refs"]
    current_refs = current_catalog["internal"]["refs"]
    for candidate_id in current_expanded:
        if supplied_refs[candidate_id]["moniker"] != current_refs[candidate_id]["moniker"]:
            raise ValueError(
                "Candidate identity changed since the catalog was built: " + candidate_id)


def expand_candidate_ids(requested_ids, catalog):
    refs = catalog["internal"]["refs"]
    loops = catalog["internal"].get("loop_refs", {})
    expanded = []
    for candidate_id in requested_ids:
        if candidate_id in refs:
            expanded.append(candidate_id)
        elif candidate_id in loops:
            expanded.extend(loops[candidate_id]["edge_ids"])
        else:
            raise ValueError("Unknown candidate id: " + str(candidate_id))
    return sorted(set(expanded))


def public_rows(catalog):
    result = {}
    for collection in ("bodies", "faces", "edges", "loops", "groups"):
        for row in catalog["public"].get(collection, []):
            result[row["id"]] = row
    return result


def verify_active_selection(resolved, catalog):
    """Set and verify the exact current-document objects by native Moniker."""
    refs = catalog["internal"]["refs"]
    expected_monikers = sorted([moniker_of(obj) for obj in resolved])
    Selection.Create(resolved).SetActive()
    actual_monikers = sorted([
        moniker_of(obj) for obj in Selection.GetActive().Items])
    if actual_monikers != expected_monikers:
        raise ValueError("SpaceClaim active selection differs from requested objects")

    id_by_moniker = dict([
        (refs[candidate_id]["moniker"], candidate_id)
        for candidate_id in refs])
    try:
        active_ids = sorted([id_by_moniker[value] for value in actual_monikers])
    except KeyError:
        raise ValueError("SpaceClaim active selection contains an unmapped native object")
    return active_ids, actual_monikers


def select_candidates(request, catalog):
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported catalog schema version")
    # The host rebuilds deterministic IDs from geometry/topology before using
    # current objects.  The caller additionally checks the saved/current
    # native Moniker for every expanded requested object, preventing an
    # unnoticed ID swap between geometrically identical candidates.

    requested_ids = request.get("candidate_ids")
    if not isinstance(requested_ids, list) or not requested_ids:
        raise ValueError("candidate_ids must be a non-empty list")
    if len(set(requested_ids)) != len(requested_ids):
        raise ValueError("candidate_ids must not contain duplicates")

    expanded_ids = expand_candidate_ids(requested_ids, catalog)
    resolved = []
    for candidate_id in expanded_ids:
        obj = LIVE_OBJECTS.get(candidate_id)
        if obj is None:
            raise ValueError("Current document is missing candidate: " + candidate_id)
        resolved.append(obj)

    active_ids, actual_monikers = verify_active_selection(resolved, catalog)
    if active_ids != sorted(expanded_ids):
        raise ValueError("SpaceClaim active candidate IDs differ from expanded request")
    rows = public_rows(catalog)
    return resolved, {
        "requested_candidate_ids": list(requested_ids),
        "expanded_candidate_ids": expanded_ids,
        "active_candidate_ids": active_ids,
        "active_monikers": actual_monikers,
        "selected_objects": [rows[candidate_id] for candidate_id in requested_ids],
        "selection_verified": True,
    }


def ensure_folder(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def safe_stem(value):
    value = value or "cfd-agent-selection"
    text = value if isinstance(value, STRING_TYPES) else str(value)
    ascii_safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    text = "".join([
        char if char in ascii_safe else "_" for char in text])
    return text or "cfd-agent-selection"


def export_picture(path):
    Window.ActiveWindow.Export(WindowExportFormat.Png, path)
    bitmap = Bitmap(path)
    try:
        colors = set()
        step_x = max(1, bitmap.Width // 32)
        step_y = max(1, bitmap.Height // 32)
        for x in range(0, bitmap.Width, step_x):
            for y in range(0, bitmap.Height, step_y):
                colors.add(bitmap.GetPixel(x, y).ToArgb())
        if len(colors) < 2:
            raise ValueError("SpaceClaim exported a blank/uniform image: " + path)
    finally:
        bitmap.Dispose()


def refresh_for_export(ui_mode):
    """Allow both visible and headless renderers to finish a camera change."""
    Window.ActiveWindow.RefreshRendering()
    FormsApplication.DoEvents()
    # The visible WPF viewport animates/refills its back buffer after a camera
    # change; 250 ms was sufficient off-screen but produced uniform PNGs from
    # the second export onward in a real GUI batch.
    Thread.Sleep(1000 if ui_mode == "gui" else 250)
    FormsApplication.DoEvents()


def colour_objects(resolved):
    all_bodies = list(DocumentHelper.GetRootPart().GetAllBodies())
    all_faces = [face for body in all_bodies for face in body.Faces]
    all_edges = [edge for body in all_bodies for edge in body.Edges]
    options = SetColorOptions()
    options.FaceColorTarget = FaceColorTarget.Face
    options.Exact = True
    selected_faces = []
    selected_edges = []
    overlay_curves = []
    for obj in resolved:
        type_name = obj.GetType().Name
        if type_name.startswith("DesignBody"):
            selected_faces.extend(list(obj.Faces))
        elif type_name.startswith("DesignFace"):
            selected_faces.append(obj)
        elif type_name.startswith("DesignEdge"):
            selected_edges.append(obj)
    if selected_faces:
        ColorHelper.SetColor(Selection.Create(selected_faces), options, Color.Orange)
    if selected_edges:
        ColorHelper.SetColor(Selection.Create(selected_edges), Color.Orange)
    # Direct appearance-context colors are needed by some native off-screen
    # renderers that ignore the scripting helper's transient face override.
    context = Window.ActiveWindow.ActiveContext
    try:
        for face in all_faces:
            face.SetColor(context, Color.LightGray)
        for face in selected_faces:
            face.SetColor(context, Color.Orange)
    except Exception:
        pass
    # Design-edge colors are not reliably present in an off-screen PNG.
    # Unsaved thick curve copies make the selected edge/loop visibly orange in
    # the staged document only; they are deleted after rendering.
    for edge in selected_edges:
        try:
            overlay = DesignCurve.Create(DocumentHelper.GetRootPart(), edge.Shape)
            # Register immediately: if styling fails the temporary curve must
            # still be deleted by render_views.
            overlay_curves.append(overlay)
            # A null context applies an object-level override that is retained
            # by SpaceClaim's off-screen exporter; a window-context override
            # is only guaranteed for the live viewport.
            overlay.SetColor(None, Color.Orange)
            overlay.SetLineWeight(None, LineWeight(LineWeightType.Thick))
        except Exception:
            pass
    return all_bodies, selected_faces, selected_edges, overlay_curves


def resolved_for_candidate(candidate_id, catalog):
    expanded = expand_candidate_ids([candidate_id], catalog)
    result = []
    for current_id in expanded:
        obj = LIVE_OBJECTS.get(current_id)
        if obj is None:
            raise ValueError("Current document is missing candidate: " + current_id)
        result.append(obj)
    return result


def set_selected_projection(selected_faces, selected_edges, active, all_bodies):
    if len(selected_faces) == 1:
        face = selected_faces[0]
        center = MeasureHelper.GetCentroid(Selection.Create(face))
        bounds = bbox(face.Shape)
        spans = [bounds["max"][axis] - bounds["min"][axis]
                 for axis in range(3)]
        if isinstance(face.Shape.Geometry, Plane):
            direction = face.Shape.Geometry.Frame.DirZ
            if face.Shape.IsReversed:
                direction = -direction
        else:
            # Look normal to a point on the curved surface.  An arbitrary
            # isometric direction can leave a valid bore or fillet hidden
            # behind its own solid even when the face is actively selected.
            evaluation = face.Shape.Geometry.ProjectPoint(center)
            center = evaluation.Point
            direction = evaluation.Normal
            if face.Shape.IsReversed:
                direction = -direction
        ViewHelper.SetProjection(
            Frame.Create(center, direction), max(max(spans), 1e-6) * 2.2)
    elif len(selected_edges) == 1:
        # ZoomToEntity can derive a zero-height or zero-width camera box for a
        # straight edge.  Use an explicit non-zero view extent instead.  A
        # circle supplies its own curve frame; for a line or other curve, an
        # adjacent planar face supplies a stable normal when available.
        edge = selected_edges[0]
        center = edge.Shape.GetBoundingBox(Matrix.Identity).Center
        direction = None
        curve_frame = getattr(edge.Shape.Geometry, "Frame", None)
        if curve_frame is not None:
            direction = curve_frame.DirZ
        if direction is None:
            for face in edge.Faces:
                if isinstance(face.Shape.Geometry, Plane):
                    direction = face.Shape.Geometry.Frame.DirZ
                    break
        if direction is not None:
            ViewHelper.SetProjection(
                Frame.Create(center, direction),
                max(number(edge.Shape.Length) or 0.0, 1e-6) * 2.2)
        else:
            ViewHelper.SetProjection(
                ViewHelper.ViewProjection.Isometric, True, False)
            ViewHelper.ZoomToEntity(active)
    else:
        ViewHelper.SetProjection(
            ViewHelper.ViewProjection.Isometric, True, False)
        target = active if active.Count else Selection.Create(all_bodies)
        ViewHelper.ZoomToEntity(target)


def view_result(view, path):
    result = {
        "view": view,
        "role": VIEW_ROLES.get(view, "auxiliary"),
        "path": path,
    }
    if view == "Selected":
        result["projection_basis"] = VIEW_CONTRACT["selected_projection"]
    else:
        result["coordinate_frame"] = VIEW_CONTRACT["coordinate_frame"]
    return result


def require_rendered_views(results, expected_views):
    by_view = {}
    for item in results:
        by_view.setdefault(item.get("view"), []).append(item)
    for view in expected_views:
        rows = by_view.get(view, [])
        if len(rows) != 1:
            raise ValueError("Expected exactly one rendered view: " + str(view))
        row = rows[0]
        if row.get("error") or not row.get("path") or not os.path.isfile(row["path"]):
            raise ValueError("Required SpaceClaim view was not rendered: " + str(view))


def render_views(folder, stem, resolved, views, catalog=None):
    ensure_folder(folder)
    all_bodies = list(DocumentHelper.GetRootPart().GetAllBodies())
    selected_faces = []
    selected_edges = []
    overlay_curves = []
    try:
        active = Selection.Empty()
        if resolved:
            all_bodies, selected_faces, selected_edges, overlay_curves = colour_objects(resolved)
            active = Selection.Create(resolved)
            if catalog is None:
                raise ValueError("catalog is required when rendering selected objects")
            verify_active_selection(resolved, catalog)

        results = []
        for view in (views if views is not None else DEFAULT_VIEWS):
            try:
                if view not in ALLOWED_VIEWS:
                    raise ValueError("Unsupported view: " + str(view))
                if resolved:
                    verify_active_selection(resolved, catalog)
                ViewHelper.SetProjection(
                    getattr(ViewHelper.ViewProjection, view), True, False)
                ViewHelper.ZoomToEntity(Selection.Create(all_bodies))
                refresh_for_export(request.get("ui_mode"))
                path = os.path.join(folder, "%s-%s.png" % (stem, view))
                export_picture(path)
                if resolved:
                    verify_active_selection(resolved, catalog)
                results.append(view_result(view, path))
            except Exception:
                results.append({"view": view, "error": traceback.format_exc()})

        if resolved:
            try:
                verify_active_selection(resolved, catalog)
                set_selected_projection(
                    selected_faces, selected_edges, active, all_bodies)
                refresh_for_export(request.get("ui_mode"))
                path = os.path.join(folder, "%s-Selected.png" % stem)
                export_picture(path)
                verify_active_selection(resolved, catalog)
                results.append(view_result("Selected", path))
            except Exception:
                results.append({"view": "Selected", "error": traceback.format_exc()})
            # Show the selected face in the context of its owning component.
            # Non-owner bodies are hidden uniformly; no case or face-specific
            # decision is involved.  Internal faces may remain occluded here,
            # which is why the exact one-face proxy is exported separately.
            owner_visibility = []
            try:
                if not selected_faces:
                    raise ValueError(
                        "OwnerContext is only applicable to selected faces")
                verify_active_selection(resolved, catalog)
                owner_monikers = set()
                for face in selected_faces:
                    owner_monikers.add(moniker_of(face.Parent))
                for obj in resolved:
                    if obj.GetType().Name.startswith("DesignBody"):
                        owner_monikers.add(moniker_of(obj))
                owner_bodies = []
                for body in all_bodies:
                    owner_visibility.append((body, body.IsVisible(None)))
                    is_owner = moniker_of(body) in owner_monikers
                    body.SetVisibility(None, is_owner)
                    if is_owner:
                        owner_bodies.append(body)
                if not owner_bodies:
                    raise ValueError("No owner body was found for selected face")
                ViewHelper.SetProjection(
                    ViewHelper.ViewProjection.Isometric, True, False)
                ViewHelper.ZoomToEntity(Selection.Create(owner_bodies))
                refresh_for_export(request.get("ui_mode"))
                path = os.path.join(folder, "%s-OwnerContext.png" % stem)
                export_picture(path)
                verify_active_selection(resolved, catalog)
                results.append(view_result("OwnerContext", path))
            except Exception:
                if selected_faces:
                    results.append({
                        "view": "OwnerContext", "error": traceback.format_exc()})
            finally:
                for body, was_visible in owner_visibility:
                    try:
                        body.SetVisibility(None, was_visible)
                    except Exception:
                        pass
            # Fully internal faces cannot be seen through the native off-screen
            # renderer even with the owner body isolated.  Build an unsaved,
            # one-face display proxy from the exact selected master face, place
            # it in occurrence coordinates, render it, and delete it.  The
            # original face remains the verified active selection throughout.
            proxy_bodies = []
            proxy_visibility = []
            try:
                if not selected_faces:
                    raise ValueError(
                        "SelectedProxy is only applicable to selected faces")
                verify_active_selection(resolved, catalog)
                for body in all_bodies:
                    proxy_visibility.append((body, body.IsVisible(None)))
                    body.SetVisibility(None, False)
                for face in selected_faces:
                    master_face = getattr(face, "Master", face)
                    modeler_body = ModelerBody.CreateNurbsBody(master_face.Shape)
                    proxy = DesignBody.Create(
                        DocumentHelper.GetRootPart(), "__grounding_display_proxy__",
                        modeler_body)
                    to_master = getattr(face, "TransformToMaster", Matrix.Identity)
                    transform = to_master.Inverse
                    if not transform.IsIdentity:
                        proxy.Transform(transform)
                    proxy.SetColor(None, Color.Orange)
                    proxy_bodies.append(proxy)
                if not proxy_bodies:
                    raise ValueError("No face display proxy was created")
                ViewHelper.SetProjection(
                    ViewHelper.ViewProjection.Isometric, True, False)
                ViewHelper.ZoomToEntity(Selection.Create(proxy_bodies))
                refresh_for_export(request.get("ui_mode"))
                path = os.path.join(folder, "%s-SelectedProxy.png" % stem)
                export_picture(path)
                verify_active_selection(resolved, catalog)
                results.append(view_result("SelectedProxy", path))
            except Exception:
                if selected_faces:
                    results.append({
                        "view": "SelectedProxy", "error": traceback.format_exc()})
            finally:
                for proxy in reversed(proxy_bodies):
                    try:
                        proxy.Delete()
                    except Exception:
                        pass
                for body, was_visible in proxy_visibility:
                    try:
                        body.SetVisibility(None, was_visible)
                    except Exception:
                        pass
        if resolved:
            verify_active_selection(resolved, catalog)
        return results
    finally:
        cleanup_errors = []
        for overlay in reversed(overlay_curves):
            try:
                overlay.Delete()
            except Exception:
                cleanup_errors.append(traceback.format_exc())
        if cleanup_errors:
            raise RuntimeError(
                "Failed to delete temporary display curves:\n" +
                "\n".join(cleanup_errors))


def get_request_path():
    path = os.environ.get("SPACECLAIM_GROUNDING_REQUEST")
    if not path:
        raise ValueError("SPACECLAIM_GROUNDING_REQUEST is not set")
    return path
