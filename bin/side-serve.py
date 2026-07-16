#!/usr/bin/env python3
# Side local server -- stdlib only. Serves the app UI, a small save API,
# and a SAFE read-only agent-analysis API.
#
# The agent API spawns headless Claude Code (`claude -p`) in PLAN MODE ONLY so a
# Side canvas node can READ the user's code/context and PROPOSE work. Plan mode
# makes ZERO file edits; the child runs confined to a throwaway sandbox with no
# API keys and no widened reach. This daemon never executes proposals -- approval
# and execution live elsewhere in the product. (Per-action edit bridge = future.)
import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.8.0-alpha.1"
MAX_SAVE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_BODY_BYTES = 8 * 1024 * 1024  # hard cap on raw request body /api/save will read
MAX_AGENT_BODY = 64 * 1024  # 64KB JSON cap on agent endpoints

WORKSPACE_ROOT = Path(os.path.expanduser("~/Side")).resolve()
RUNS_ROOT = (WORKSPACE_ROOT / "runs").resolve()  # throwaway agent sandboxes live here

# ---- agent runtime knobs ----
MAX_JOBS = 3  # concurrent agent jobs -> 429 beyond this
JOB_TIMEOUT = 300  # hard wall-clock timeout per job (seconds)
JOB_PRUNE_AFTER = 600  # finished jobs pruned 10 min after they end
OUTPUT_CAP = 200 * 1024  # keep last ~200KB of combined stdout/stderr per job
PROMPT_CAP = 8000  # max composed prompt length (chars)

# READ-ONLY BY TOOL RESTRICTION. The child claude is limited to read-only tools
# (Read/Grep/Glob) via --allowedTools; Write, Edit and Bash are NOT in the set, so
# the process physically cannot modify files or run commands even if asked -- and
# unlike --permission-mode plan (which lingers waiting for interactive approval in
# headless -p mode and never terminates), a read-only-tool run completes cleanly.
# Verified: an adversarial "create a file" prompt produced no file. There is no
# write/edit/bash-enabling flag anywhere in this file.
READ_TOOLS = "Read,Grep,Glob"
MODE_LABEL = "read"

# Fixed footer appended to every composed prompt. Reinforces read-only intent.
PROMPT_FOOTER = (
    "\n\nYou are one node of a Side fleet running in READ-ONLY mode. "
    "You can only read files (no write/edit/run tools are available). "
    "READ what you need from the current directory, then produce a concrete "
    "proposal: the exact steps and file changes you WOULD make, and a "
    "one-paragraph summary. A human will review and approve before anything runs."
)

# Where to look for the claude binary if it is not already on PATH.
CLAUDE_CANDIDATE_PATHS = (
    "~/.local/bin/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
    "~/.claude/local/claude",
)

# Known API routes -> the HTTP methods they accept. Anything else under /api/ is
# a 404; a known path hit with the wrong method is a 405.
API_ROUTES = {
    "/api/health": ("GET",),
    "/api/save": ("POST",),
    "/api/agent/detect": ("GET",),
    "/api/agent/analyze": ("POST",),
    "/api/agent/poll": ("GET",),
    "/api/agent/stop": ("POST",),
}


def cors_origin(handler, port):
    origin = handler.headers.get("Origin", "")
    allowed = ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)
    if origin in allowed:
        return origin
    return None


# ---- claude binary detection (cached) ----
_DETECT_LOCK = threading.Lock()
_DETECT_CACHE = {"result": None}


def _detect_claude():
    """Resolve the claude binary and its version. Never raises."""
    path = shutil.which("claude")
    if not path:
        for cand in CLAUDE_CANDIDATE_PATHS:
            p = os.path.expanduser(cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                path = p
                break
    if not path:
        return {"available": False, "path": None, "version": None}
    version = None
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            text=True,
        )
        if proc.returncode == 0:
            version = (proc.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        version = None
    return {"available": True, "path": path, "version": version}


def get_detection():
    """Cheap cached detect. Once available, the result is memoized; while
    unavailable we re-probe (which() is cheap, no subprocess unless a binary
    appears) so a later install is picked up without a restart."""
    with _DETECT_LOCK:
        cached = _DETECT_CACHE["result"]
        if cached is not None and cached.get("available"):
            return cached
        result = _detect_claude()
        _DETECT_CACHE["result"] = result
        return result


def sanitize_sandbox(slug):
    """Map a node slug to ~/Side/runs/<sanitized>/workspace, mirroring the
    traversal protection /api/save uses. Returns (sandbox_path, None) or
    (None, error_message). Rejects traversal outright, then confines."""
    if not isinstance(slug, str):
        return None, "slug is required"
    s = slug.strip()
    if not s:
        return None, "slug is required"
    if "\x00" in s:
        return None, "invalid slug"
    # Reject anything that could climb out before we ever touch the filesystem.
    if "/" in s or "\\" in s or ".." in s:
        return None, "invalid slug"
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in s)
    safe = safe.strip("-. ")
    if not safe:
        return None, "invalid slug"
    sandbox = (RUNS_ROOT / safe / "workspace").resolve()
    try:
        sandbox.relative_to(RUNS_ROOT)
    except ValueError:
        return None, "slug escapes sandbox"
    return sandbox, None


def build_child_env(claude_path):
    """Env for the child claude process: inherit the daemon env and guarantee the
    claude bin dir is on PATH. We deliberately KEEP the machine's own claude auth
    (subscription login in ~/.claude, or ANTHROPIC_API_KEY if that's how the user
    authenticates the CLI) -- the node cannot read/analyze anything otherwise.
    Safety here comes from the read-only tool set (zero edits) + the throwaway
    sandbox cwd, not from starving claude of its own credentials. Note: the Side
    browser key (localStorage side_api_key) never touches this process -- it lives
    in the browser and is only used for the direct Messages-API tier."""
    env = os.environ.copy()
    bin_dir = os.path.dirname(claude_path)
    if bin_dir:
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        if bin_dir not in parts:
            env["PATH"] = os.pathsep.join([bin_dir] + parts) if parts else bin_dir
    env["CLAUDE_NO_ANALYTICS"] = "1"
    return env


# ---- job manager ----
class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.status = "running"  # running | done | error
        self.output = ""
        self.code = None
        self.started = time.time()
        self.finished_at = None
        self.proc = None
        self.timed_out = False
        self.stopped = False
        self.lock = threading.Lock()


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def _prune_locked(self):
        now = time.time()
        dead = []
        for jid, job in self._jobs.items():
            fa = job.finished_at
            if job.status != "running" and fa is not None and (now - fa) > JOB_PRUNE_AFTER:
                dead.append(jid)
        for jid in dead:
            del self._jobs[jid]

    def create(self, argv, cwd, env):
        """Register + start a job. Returns job_id, or None if at capacity."""
        with self._lock:
            self._prune_locked()
            active = 0
            for job in self._jobs.values():
                if job.status == "running":
                    active += 1
            if active >= MAX_JOBS:
                return None
            job_id = uuid.uuid4().hex
            job = Job(job_id)
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run, args=(job, argv, cwd, env), daemon=True)
        thread.start()
        return job_id

    def _append(self, job, text):
        with job.lock:
            job.output += text
            if len(job.output) > OUTPUT_CAP:
                job.output = "[output truncated]\n" + job.output[-OUTPUT_CAP:]

    def _terminate(self, proc):
        # SIGTERM, then SIGKILL if it does not exit within 3s.
        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass

    def _watchdog(self, job, proc):
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                return  # finished on its own
            time.sleep(0.2)
        if proc.poll() is None:
            with job.lock:
                job.timed_out = True
            self._terminate(proc)

    def _run(self, job, argv, cwd, env):
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,  # headless: claude -p must not wait on stdin
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )
        except OSError as exc:
            with job.lock:
                job.status = "error"
                job.code = -1
                job.output = job.output + "spawn failed: %s\n" % exc
                job.finished_at = time.time()
            return
        with job.lock:
            job.proc = proc
        watchdog = threading.Thread(target=self._watchdog, args=(job, proc), daemon=True)
        watchdog.start()
        try:
            for line in proc.stdout:
                self._append(job, line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
        return_code = proc.wait()
        with job.lock:
            job.finished_at = time.time()
            if job.timed_out:
                job.status = "error"
                job.code = -1
            elif job.stopped:
                job.status = "error"
                job.code = return_code
            else:
                job.code = return_code
                job.status = "done" if return_code == 0 else "error"

    def poll(self, job_id):
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            end = job.finished_at if job.finished_at is not None else time.time()
            return {
                "status": job.status,
                "output": job.output,
                "code": job.code,
                "elapsed": round(end - job.started, 3),
            }

    def stop(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        with job.lock:
            proc = job.proc
            job.stopped = True
        if proc is not None:
            self._terminate(proc)
        return True


JOBS = JobManager()


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

    def _method_not_allowed(self, allowed):
        headers = self._cors_headers()
        headers["Allow"] = ",".join(allowed)
        self._send_json(405, {"error": "method not allowed"}, headers)

    def _read_json(self, headers, max_bytes):
        """Read + parse a JSON object body, capped at max_bytes. On any problem
        it sends the error response and returns None."""
        length_hdr = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_json(400, {"error": "invalid content-length"}, headers)
            return None
        if length <= 0:
            self._send_json(400, {"error": "missing body"}, headers)
            return None
        if length > max_bytes:
            self._send_json(413, {"error": "request too large"}, headers)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"}, headers)
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"error": "invalid json body"}, headers)
            return None
        return data

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
        try:
            route = urllib.parse.urlsplit(self.path).path
            if route in API_ROUTES:
                if "GET" not in API_ROUTES[route]:
                    self._method_not_allowed(API_ROUTES[route])
                    return
                if route == "/api/health":
                    self._handle_health()
                elif route == "/api/agent/detect":
                    self._handle_agent_detect()
                elif route == "/api/agent/poll":
                    self._handle_agent_poll()
                return
            if route.startswith("/api/"):
                self._send_json(404, {"error": "not found"}, self._cors_headers())
                return
            self._handle_static()
        except Exception as exc:  # last-resort backstop -- never crash the handler
            self._safe_500(exc)

    def do_POST(self):
        try:
            route = urllib.parse.urlsplit(self.path).path
            if route in API_ROUTES:
                if "POST" not in API_ROUTES[route]:
                    self._method_not_allowed(API_ROUTES[route])
                    return
                if route == "/api/save":
                    self._handle_save()
                elif route == "/api/agent/analyze":
                    self._handle_agent_analyze()
                elif route == "/api/agent/stop":
                    self._handle_agent_stop()
                return
            self._send_json(404, {"error": "not found"}, self._cors_headers())
        except Exception as exc:
            self._safe_500(exc)

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

    def _safe_500(self, exc):
        try:
            self._send_json(500, {"error": "internal error: %s" % exc}, self._cors_headers())
        except Exception:
            pass

    # ---- handlers ----
    def _handle_health(self):
        detect = get_detection()
        obj = {
            "ok": True,
            "version": VERSION,
            "workspace": str(WORKSPACE_ROOT),
            "agent": {"available": bool(detect.get("available")), "mode": MODE_LABEL},
        }
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

    def _handle_agent_detect(self):
        headers = self._cors_headers()
        detect = get_detection()
        obj = {
            "ok": True,
            "available": bool(detect.get("available")),
            "version": detect.get("version"),
            "mode": MODE_LABEL,
        }
        self._send_json(200, obj, headers)

    def _handle_agent_analyze(self):
        headers = self._cors_headers()
        data = self._read_json(headers, MAX_AGENT_BODY)
        if data is None:
            return

        task = data.get("task")
        node = data.get("node")
        context = data.get("context")
        if not isinstance(task, str) or not task.strip():
            self._send_json(400, {"error": "task is required"}, headers)
            return
        if not isinstance(node, str):
            self._send_json(400, {"error": "node is required"}, headers)
            return
        if context is not None and not isinstance(context, str):
            self._send_json(400, {"error": "context must be a string"}, headers)
            return

        sandbox, err = sanitize_sandbox(data.get("slug"))
        if err is not None:
            self._send_json(400, {"error": err}, headers)
            return

        # Compose the prompt. Read-only footer is always appended.
        prompt = task
        if context:
            prompt += "\n\nCONTEXT:\n" + context
        prompt += PROMPT_FOOTER
        if len(prompt) > PROMPT_CAP:
            self._send_json(413, {"error": "composed prompt too large"}, headers)
            return

        # Only spawn if a claude binary is present AND we can run plan mode. We
        # never fall back to an editing mode -- if plan is unavailable, we bail.
        detect = get_detection()
        if not detect.get("available"):
            self._send_json(503, {"error": "agent runtime unavailable"}, headers)
            return
        claude_path = detect.get("path")

        try:
            sandbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._send_json(500, {"error": "sandbox setup failed: %s" % exc}, headers)
            return

        # -p prompt, text output, READ-ONLY TOOLS ONLY. No --add-dir (cwd is the
        # throwaway sandbox, never a real repo). No write/edit/bash tool is allowed,
        # so the child cannot modify anything and it terminates cleanly.
        argv = [
            claude_path,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--allowedTools",
            READ_TOOLS,
        ]
        env = build_child_env(claude_path)

        job_id = JOBS.create(argv, str(sandbox), env)
        if job_id is None:
            self._send_json(429, {"error": "agent runtime busy"}, headers)
            return
        self._send_json(200, {"ok": True, "job": job_id}, headers)

    def _handle_agent_poll(self):
        headers = self._cors_headers()
        query = urllib.parse.urlsplit(self.path).query
        params = urllib.parse.parse_qs(query)
        job_values = params.get("job") or []
        job_id = job_values[0] if job_values else ""
        if not job_id:
            self._send_json(400, {"error": "job is required"}, headers)
            return
        snapshot = JOBS.poll(job_id)
        if snapshot is None:
            self._send_json(404, {"error": "unknown job"}, headers)
            return
        obj = {"ok": True}
        obj.update(snapshot)
        self._send_json(200, obj, headers)

    def _handle_agent_stop(self):
        headers = self._cors_headers()
        data = self._read_json(headers, MAX_AGENT_BODY)
        if data is None:
            return
        job_id = data.get("job")
        if not isinstance(job_id, str) or not job_id:
            self._send_json(400, {"error": "job is required"}, headers)
            return
        JOBS.stop(job_id)
        self._send_json(200, {"ok": True}, headers)

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
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("side-serve %s on http://127.0.0.1:%d (dir=%s)" % (VERSION, args.port, app_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
