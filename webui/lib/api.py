"""
Transport-agnostic request dispatch for lab-builder.

Both the Apache CGI shim (cgi-bin/labbuilder.py) and the local dev server
(run-local.py) call dispatch() so the routing logic lives in exactly one place.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery  # noqa: E402


def _one(v, default=""):
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def dispatch(action, method, params, body):
    """
    action  : str            e.g. "components", "schema", "validate", "save"
    method  : "GET"|"POST"
    params  : dict[str, list] parsed query string
    body    : bytes          request body (POST)
    returns : (http_status:int, obj:dict|list)
    """
    try:
        if action == "components" and method == "GET":
            comps = discovery.discover()
            return 200, {
                "count": len(comps),
                "components": comps,
                "scripts_dir": discovery.scripts_dir(),
                "libs_dir": discovery.libs_dir(),
            }

        if action == "schema" and method == "GET":
            return 200, discovery.schema(_one(params.get("name")))

        if action == "base" and method == "GET":
            return 200, discovery.base_schema()

        if action == "status" and method == "GET":
            return 200, discovery.status()

        if method == "POST":
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            if action == "validate":
                return 200, discovery.validate_lab(data.get("config", {}))
            if action == "save":
                path = discovery.save_lab(data.get("filename", "lab"), data.get("config", {}))
                return 200, {"saved": os.path.basename(path), "path": path}

        return 400, {"error": "unknown action %r (%s)" % (action, method)}

    except (ValueError, FileNotFoundError) as e:
        return 400, {"error": str(e)}
    except Exception as e:  # never leak a stack trace as an HTTP 500 body
        return 500, {"error": "%s: %s" % (type(e).__name__, e)}
