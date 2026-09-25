# Python Script, API Version = V241
"""SpaceClaim query and exact-selection entrypoint (IronPython 2.7)."""

import os

# Trusted packaged helpers execute in SpaceClaim's provided scripting namespace.
execfile(os.environ["CFD_AGENT_SC_COMMON"], globals())

request_path = get_request_path()
with open(request_path, "r") as request_stream:
    request = json.load(request_stream)

response = {
    "ok": False,
    "run_id": request.get("run_id"),
    "operation": request.get("operation"),
    "ui_mode": request.get("ui_mode"),
    "view_contract": VIEW_CONTRACT,
}
try:
    operation = request.get("operation")
    if operation not in ("catalog", "select", "batch_select"):
        raise ValueError("operation must be catalog, select, or batch_select")
    if not request.get("input"):
        raise ValueError("input is required")
    if not request.get("response"):
        raise ValueError("response is required")

    DocumentOpen.Execute(request["input"])
    folder = request.get("output_dir") or request.get("folder")
    if not folder:
        folder = os.path.dirname(request["response"])
    stem = safe_stem(request.get("image_stem") or operation)

    if operation == "catalog":
        catalog = build_catalog()
        response["catalog"] = catalog
        response["images"] = render_views(
            folder, stem, None, request.get("views"))
        require_rendered_views(response["images"],
                               request.get("views") or DEFAULT_VIEWS)
        if request.get("render_candidates"):
            collections = request.get("candidate_collections") or ["bodies", "faces", "edges", "loops"]
            if any(item not in ("bodies", "faces", "edges", "loops") for item in collections):
                raise ValueError("Unknown candidate collection")
            response["candidate_render_results"] = []
            for collection in collections:
                for row in catalog["public"].get(collection, []):
                    candidate_id = row["id"]
                    candidate_kind = {
                        "bodies": "body", "faces": "face",
                        "edges": "edge", "loops": "loop"}[collection]
                    candidate_result = {
                        "candidate_id": candidate_id,
                        "candidate_kind": candidate_kind,
                        "status": "pending",
                        "images": [],
                    }

                    # Direct edge selection is executable only for a closed
                    # single-face open-edge representation.  Keep every
                    # edge in the topology catalog, but do not render unrelated
                    # seam/straight edges as visual opening candidates.
                    if (collection == "edges" and
                            (row.get("closed") is not True or
                             len(row.get("face_ids", [])) != 1)):
                        candidate_result["status"] = "skipped"
                        candidate_result["reason"] = "not_a_supported_opening_edge"
                        response["candidate_render_results"].append(candidate_result)
                        continue

                    try:
                        rendered = render_views(
                            folder, stem + "-candidate-" + candidate_id,
                            resolved_for_candidate(candidate_id, catalog), [], catalog)
                        for item in rendered:
                            item["candidate_id"] = candidate_id
                            item["candidate_kind"] = candidate_kind
                        candidate_result["images"] = rendered
                        response["images"].extend(rendered)
                        try:
                            require_rendered_views(rendered, ["Selected"])
                            candidate_result["status"] = "rendered"
                        except Exception:
                            candidate_result["status"] = "failed"
                            candidate_result["validation_error"] = traceback.format_exc()
                            image_errors = [
                                item.get("error") for item in rendered
                                if item.get("error")]
                            candidate_result["error"] = (
                                "\n\n".join(image_errors) if image_errors else
                                candidate_result["validation_error"])
                    except Exception:
                        candidate_result["status"] = "failed"
                        candidate_result["error"] = traceback.format_exc()
                    response["candidate_render_results"].append(candidate_result)
            response["candidate_render_summary"] = {
                "rendered": len([
                    item for item in response["candidate_render_results"]
                    if item["status"] == "rendered"]),
                "failed": len([
                    item for item in response["candidate_render_results"]
                    if item["status"] == "failed"]),
                "skipped": len([
                    item for item in response["candidate_render_results"]
                    if item["status"] == "skipped"]),
            }
    elif operation == "select":
        supplied_catalog = load_catalog(request)
        current_catalog = build_catalog()
        requested_ids = request.get("candidate_ids") or []
        validate_catalog_identity(supplied_catalog, current_catalog, requested_ids)
        resolved, selected = select_candidates(request, current_catalog)
        response.update(selected)
        response["images"] = render_views(
            folder, stem, resolved, request.get("views"), current_catalog,
            request.get("detail_views"))
        requested_views = (request.get("views") if request.get("views") is not None
                           else DEFAULT_VIEWS)
        detail_views = request.get("detail_views") or ["Selected"]
        if any(view not in DETAIL_VIEWS for view in detail_views):
            raise ValueError("select contains an unsupported detail view")
        require_rendered_views(response["images"], list(requested_views) + list(detail_views))

        # Rendering changes camera and display colors but must not change the
        # requested active selection.  Verify the exact moniker set again.
        final_ids, final_monikers = verify_active_selection(resolved, current_catalog)
        if final_ids != sorted(response["expanded_candidate_ids"]):
            raise ValueError("SpaceClaim selection changed while rendering")
        response["active_candidate_ids"] = final_ids
        response["active_monikers"] = final_monikers
    else:
        supplied_catalog = load_catalog(request)
        current_catalog = build_catalog()
        if geometry_signature(supplied_catalog) != geometry_signature(current_catalog):
            raise ValueError("Geometry or topology changed since the candidate catalog was built")
        selections = request.get("selections")
        if not isinstance(selections, list) or not selections:
            raise ValueError("batch_select requires a non-empty selections list")
        task_ids = [item.get("task_id") for item in selections
                    if isinstance(item, dict)]
        if (len(task_ids) != len(selections) or
                any([not isinstance(value, STRING_TYPES) or not value
                     for value in task_ids])):
            raise ValueError("Every batch selection requires a non-empty string task_id")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Batch task_id values must be unique")

        response["results"] = []
        for index, item in enumerate(selections):
            task_id = item["task_id"]
            task_result = {"task_id": task_id, "ok": False}
            try:
                requested_ids = item.get("candidate_ids") or []
                validate_catalog_identity(
                    supplied_catalog, current_catalog, requested_ids)
                selection_request = {
                    "candidate_ids": requested_ids,
                }
                resolved, selected = select_candidates(
                    selection_request, current_catalog)
                task_result.update(selected)
                task_views = item.get("views")
                if task_views is None:
                    task_views = []
                task_detail_views = item.get("detail_views") or ["Selected"]
                if any(view not in DETAIL_VIEWS for view in task_detail_views):
                    raise ValueError("batch_select contains an unsupported detail view")
                task_stem = "%s-task-%04d-%s" % (
                    stem, index + 1, safe_stem(task_id))
                task_result["images"] = render_views(
                    folder, task_stem, resolved, task_views, current_catalog,
                    task_detail_views)
                require_rendered_views(
                    task_result["images"], list(task_views) + list(task_detail_views))
                final_ids, final_monikers = verify_active_selection(
                    resolved, current_catalog)
                if final_ids != sorted(task_result["expanded_candidate_ids"]):
                    raise ValueError("SpaceClaim selection changed while rendering")
                task_result["active_candidate_ids"] = final_ids
                task_result["active_monikers"] = final_monikers
                task_result["ok"] = True
            except Exception:
                task_result["error"] = traceback.format_exc()
            response["results"].append(task_result)

    response["ok"] = True
except Exception:
    response["error"] = traceback.format_exc()

response_path = request.get("response")
if not response_path:
    raise ValueError("response is required")
temporary = response_path + ".tmp"
with open(temporary, "w") as response_stream:
    json.dump(response, response_stream, indent=2)
os.rename(temporary, response_path)
