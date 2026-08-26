#!/usr/bin/env python3
"""
Portable local dev server for the lab-builder web UI — no Apache required.

    python3 webui/run-local.py [port]      # default 8677
    then open http://localhost:8677/

Serves htdocs/ statically and routes /api to the same dispatch the CGI uses.
Stdlib only; works anywhere Python 3 runs.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Python 3.6-compatible stand-in for http.server.ThreadingHTTPServer."""
    daemon_threads = True

HERE = os.path.dirname(os.path.abspath(__file__))
HTDOCS = os.path.join(HERE, "htdocs")
sys.path.insert(0, os.path.join(HERE, "lib"))
import api  # noqa: E402

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def _api(self, method):
        u = urlparse(self.path)
        params = parse_qs(u.query)
        action = (params.get("action", [""]) or [""])[0]
        body = b""
        if method == "POST":
            n = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(n) if n else b""
        status, obj = api.dispatch(action, method, params, body)
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if urlparse(self.path).path == "/api":
            return self._api("GET")
        self._static(urlparse(self.path).path)

    def do_POST(self):
        if urlparse(self.path).path == "/api":
            return self._api("POST")
        self.send_error(404)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(HTDOCS, path.lstrip("/")))
        if not full.startswith(HTDOCS) or not os.path.isfile(full):
            return self.send_error(404)
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8677
    print("lab-builder dev server → http://localhost:%d/  (Ctrl-C to stop)" % port)
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
