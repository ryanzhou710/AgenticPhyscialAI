# Python Script, API Version = V241
"""Save-only bridge bound to the open task document, not a reopened disk copy."""

import json
import os
import traceback

import clr
clr.AddReference("System.Windows.Forms")
from System import Guid
from System.Windows.Forms import Timer
from SpaceClaim.Api.V241 import Application, Task, Window, WriteBlock


def install_save_bridge(document, working_path, folder):
    working_path = os.path.normcase(os.path.abspath(working_path))
    request_path = os.path.join(folder, "save-current-document-" + Guid.NewGuid().ToString("N") + "-request.json")
    holder = {"last_request": None, "timer": None}

    def tick(sender, args):
        if not os.path.isfile(request_path):
            return
        with open(request_path, "r") as stream:
            request = json.load(stream)
        if request["id"] == holder["last_request"]:
            return
        holder["last_request"] = request["id"]
        response = {"id": request["id"], "ok": False, "path": working_path}
        try:
            if request["operation"] != "save_if_modified":
                raise ValueError("Unsupported document request")
            expected = os.path.normcase(os.path.abspath(request["document_path"]))
            if expected != working_path:
                raise ValueError("Save request does not match the task working document")
            if Window.ActiveWindow is None or Window.ActiveWindow.Document != document:
                raise ValueError("The task editing document is not the current SpaceClaim document")
            if os.path.normcase(os.path.abspath(document.Path)) != working_path:
                raise ValueError("The editing document path differs from the task working copy")
            modified = bool(document.IsModified)
            response["modified_before"] = modified
            if modified:
                WriteBlock.ExecuteTask("Save CFD agent working CAD", Task(document.Save))
            response["saved"] = modified
            response["modified_after"] = bool(document.IsModified)
            if response["modified_after"]:
                raise ValueError("SpaceClaim still reports unsaved changes after saving")
            response["ok"] = True
        except Exception:
            response["error"] = traceback.format_exc()
        temporary = request["response"] + ".tmp"
        with open(temporary, "w") as stream:
            json.dump(response, stream, indent=2)
        os.rename(temporary, request["response"])

    def start_timer():
        timer = Timer()
        timer.Interval = 250
        timer.Tick += tick
        holder["timer"] = timer
        timer.Start()

    Application.ExecuteOnMainThread(Task(start_timer))
    return {"request_path": request_path, "document_path": working_path}
