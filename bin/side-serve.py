#!/usr/bin/env python3
# Side local server -- stdlib only. Serves the app UI and a small save API.
import argparse
import json
import mimetypes
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0"
MAX_SAVE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_BODY_BYTES = 8 * 1024 * 1024  # hard cap on raw request body we will read

WORKSPACE_ROOT = Path(os.path.expanduser("~/Side")).resolve()


def cors_origin(handler, port):
    origin = handler.headers.get("Origin", "")
    allowed = ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)
    if origin in allowed:
        return origin
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "SideServe/" + VERSION
    app_dir = None  # set by main()
    port = 4600

    def log_message(self, fmt, *args):
        sys.stdout.write(
            "%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args)
        )
        sys.stdout.flush()

    # ---- helpers ----
    def _send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        headers = {}
        origin = cors_origin(self, self.port)
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
        return headers

    def _safe_static_path(self, url_path):
        path = urllib.parse.urlsplit(url_path).path
        path = urllib.parse.unquote(path)
        if "\x00" in path:
            return None
        path = path.lstrip("/")
        if path == "":
            path = "index.html"
        candidate = (self.app_dir / path).resolve()
        try:
            candidate.relative_to(self.app_dir)
        except ValueError:
            return None
        return candidate

    # ---- routing ----
    def do_GET(self):
        if self.path == "/api/health" or self.path.startswith("/api/health?"):
            self._handle_health()
            return
        if self.path.startswith("/api/"):
            self._send_json(404, {"error": "not found"}, self._cors_headers())
            return
        self._handle_static()

    def do_POST(self):
        if self.path == "/api/save" or self.path.startswith("/api/save?"):
            self._handle_save()
            return
        self._send_json(404, {"error": "not found"}, self._cors_headers())

    def do_OPTIONS(self):
        if self.path.startswith("/api/"):
            headers = self._cors_headers()
            headers["Access-Control-Allow-Headers"] = "content-type"
            headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
            self.send_response(204)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(404, {"error": "not found"})

    # ---- handlers ----
    def _handle_health(self):
        obj = {"ok": True, "version": VERSION, "workspace": str(WORKSPACE_ROOT)}
        self._send_json(200, obj, self._cors_headers())

    def _handle_save(self):
        headers = self._cors_headers()
        length_hdr = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_json(400, {"error": "invalid content-length"}, headers)
            return
        if length <= 0:
            self._send_json(400, {"error": "missing body"}, headers)
            return
        if length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "request too large"}, headers)
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"}, headers)
            return
        if not isinstance(data, dict):
            self._send_json(400, {"error": "invalid json body"}, headers)
            return
        rel = data.get("path")
        content = data.get("content")
        if not isinstance(rel, str) or not isinstance(content, str):
            self._send_json(400, {"error": "path and content are required strings"}, headers)
            return
        if "\x00" in rel or "\x00" in content:
            self._send_json(400, {"error": "null byte in input"}, headers)
            return
        if not rel or rel.endswith("/") or os.path.isabs(rel):
            self._send_json(400, {"error": "invalid path"}, headers)
            return
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_SAVE_BYTES:
            self._send_json(400, {"error": "content too large"}, headers)
            return
        target = (WORKSPACE_ROOT / rel).resolve()
        try:
            target.relative_to(WORKSPACE_ROOT)
        except ValueError:
            self._send_json(400, {"error": "path escapes workspace"}, headers)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(content_bytes)
        except OSError as exc:
            self._send_json(500, {"error": "write failed: %s" % exc}, headers)
            return
        self._send_json(200, {"ok": True, "saved": str(target)}, headers)

    def _handle_static(self):
        candidate = self._safe_static_path(self.path)
        if candidate is None:
            self._send_json(404, {"error": "not found"})
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            self._send_json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        try:
            data = candidate.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Side local server")
    parser.add_argument("--port", type=int, default=4600)
    parser.add_argument("--dir", default=os.path.expanduser("~/.side/app"))
    args = parser.parse_args()

    app_dir = Path(args.dir).resolve()
    Handler.app_dir = app_dir
    Handler.port = args.port

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("side-serve %s on http://127.0.0.1:%d (dir=%s)" % (VERSION, args.port, app_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
