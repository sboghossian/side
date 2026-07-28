#!/usr/bin/env python3
# Side local server -- stdlib only. Serves the app UI, a small save API,
# a SAFE read-only agent-analysis API, an ACP client, and Spaces (git worktrees).
#
# TIER 1 (`/api/agent/*`) -- UNCHANGED, PROVEN.
# Spawns headless Claude Code (`claude -p`) with a READ-ONLY tool set so a Side
# canvas node can READ the user's code/context and PROPOSE work. Zero file edits;
# the child runs confined to a throwaway sandbox. This daemon never executes those
# proposals -- approval and execution live elsewhere in the product.
#
# TIER 2 (`/api/acp/*`) -- ADDITIVE, FEATURE-FLAGGED (SIDE_ACP, default on).
# A client for the Agent Client Protocol (https://agentclientprotocol.com):
# JSON-RPC 2.0 over a spawned agent's stdin/stdout, newline-delimited. Side is the
# CLIENT; the agent is the subprocess. This is what stops Side being a wrapper
# around one vendor's CLI. Turning SIDE_ACP off disables ONLY these routes -- tier 1
# is untouched either way, by design (sprint decision #3: proven fallback stays).
#
# `session/request_permission` is deliberately NOT auto-answered anywhere in this
# file. It surfaces to the browser as a `permission_request` poll update and blocks
# the agent until a human answers via POST /api/acp/permission. There is no
# auto-approve setting. Read AcpConnection._on_request_permission before changing
# anything here.
#
# SPACES (`/api/space/*`) -- one git worktree + branch per space, all working trees
# under ~/Side/spaces. `remove` refuses to run on a dirty worktree without `force`,
# and never deletes the branch, so committed work survives removal either way.
import argparse
import hmac
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.9.0-alpha.1"
MAX_SAVE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_BODY_BYTES = 8 * 1024 * 1024  # hard cap on raw request body /api/save will read
MAX_AGENT_BODY = 64 * 1024  # 64KB JSON cap on agent endpoints

# SEC-03: per-launch bearer token. Generated fresh on every process start and
# served ONLY to same-origin callers via /api/health (cross-origin reads of the
# health body are blocked by CORS, since we only echo Access-Control-Allow-Origin
# for allowed origins). Mutating POSTs must prove they are same-origin by EITHER a
# matching Origin header OR presenting this token in the X-Side-Token header. The
# Origin path lets the real app work immediately (before it has fetched the
# token); the token path covers same-origin clients that omit Origin. See
# Handler._mutation_guard_ok for the full contract.
LAUNCH_TOKEN = uuid.uuid4().hex

# SEC-04: only these Host header hosts are accepted (blocks DNS-rebinding, where a
# malicious page resolves an attacker domain to 127.0.0.1 and talks to us under
# that Host). The port, if present, must match the one we bound.
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

# SEC-05: Content-Security-Policy for served pages. Deliberately tight but
# compatible with the single-file app: inline <script>/<style> ('unsafe-inline',
# but NOT 'unsafe-eval' -- the app uses neither eval nor new Function), data:/blob:
# images, fonts as data:, and connections limited to same-origin, the Anthropic
# Messages API (browser tier), and the local Ollama/daemon loopback ports.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' https://api.anthropic.com http://127.0.0.1:* http://localhost:*; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

WORKSPACE_ROOT = Path(os.path.expanduser("~/Side")).resolve()
RUNS_ROOT = (WORKSPACE_ROOT / "runs").resolve()  # throwaway agent sandboxes live here
SPACES_ROOT = (WORKSPACE_ROOT / "spaces").resolve()  # git worktrees, one dir per space
SPACES_STATE = SPACES_ROOT / "spaces.json"
HOME_ROOT = Path(os.path.expanduser("~")).resolve()

# Every path an ACP session may be rooted at must live under one of these. A
# caller-supplied `cwd` that resolves outside them is a 400 -- the daemon does not
# hand an agent an arbitrary directory just because the request asked for one.
SESSION_ROOTS = (SPACES_ROOT, RUNS_ROOT, WORKSPACE_ROOT)

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
    # --- ACP client (tier 2). Every POST below goes through _mutation_guard_ok,
    # the same CSRF + Origin + bearer gate as /api/save. No parallel auth path.
    "/api/acp/agents": ("GET",),
    "/api/acp/session": ("POST",),
    "/api/acp/prompt": ("POST",),
    "/api/acp/poll": ("GET",),
    "/api/acp/permission": ("POST",),
    "/api/acp/stop": ("POST",),
    # --- Spaces (git worktrees).
    "/api/space/list": ("GET",),
    "/api/space/create": ("POST",),
    "/api/space/remove": ("POST",),
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


# SEC-02(a): env vars stripped from the child claude process. Even in read-only
# mode a hostile prompt could try to have claude echo back these credentials, so
# unrelated third-party secrets never enter the child env in the first place. We
# KEEP the child's own claude auth (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN and
# the ~/.claude login on disk) -- without it the node cannot read/analyze anything.
SECRET_ENV_PREFIXES = (
    "AWS_", "GOOGLE_", "GCP_", "GCLOUD_", "AZURE_", "GITHUB_", "GH_", "OPENAI_",
    "GROQ_", "MISTRAL_", "COHERE_", "REPLICATE_", "HUGGINGFACE_", "SLACK_",
    "STRIPE_", "TWILIO_", "SENDGRID_", "MAILGUN_", "VERCEL_", "NETLIFY_",
    "CLOUDFLARE_", "SUPABASE_", "FIREBASE_", "DIGITALOCEAN_", "HEROKU_",
    "DOCKERHUB_", "NGROK_", "SENTRY_", "DATADOG_", "PAGERDUTY_", "NPM_TOKEN",
)
SECRET_ENV_EXACT = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "COHERE_API_KEY",
    "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY", "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY", "HF_TOKEN", "REPLICATE_API_TOKEN", "ANTHROPIC_ADMIN_KEY",
    "DATABASE_URL", "REDIS_URL", "MONGODB_URI", "PGPASSWORD", "SSH_AUTH_SOCK",
})
# Never strip these -- they ARE the child's claude credentials.
KEEP_ENV_EXACT = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
})


def build_child_env(claude_path):
    """Env for the child claude process: inherit the daemon env, guarantee the
    claude bin dir is on PATH, and scrub unrelated third-party secrets (SEC-02a).
    We deliberately KEEP the machine's own claude auth (subscription login in
    ~/.claude, or ANTHROPIC_API_KEY if that's how the user authenticates the CLI)
    -- the node cannot read/analyze anything otherwise. If claude authenticates via
    Bedrock/Vertex we also keep the matching cloud creds so the tier never breaks.
    Safety here comes from the read-only tool set (zero edits) + the throwaway
    sandbox cwd + this scrub, not from starving claude of its own credentials.
    Note: the Side browser key (localStorage side_api_key) never touches this
    process -- it lives in the browser and is only used for the Messages-API tier."""
    env = os.environ.copy()

    def _truthy(name):
        return env.get(name, "").strip().lower() not in ("", "0", "false", "no")

    keep_aws = _truthy("CLAUDE_CODE_USE_BEDROCK")
    keep_gcp = _truthy("CLAUDE_CODE_USE_VERTEX")
    for key in list(env.keys()):
        if key in KEEP_ENV_EXACT:
            continue
        if keep_aws and key.startswith("AWS_"):
            continue  # claude authenticates via Bedrock -> its AWS creds must stay
        if keep_gcp and (key.startswith("GOOGLE_") or key.startswith("GCP_")
                         or key.startswith("GCLOUD_")):
            continue  # claude authenticates via Vertex -> its GCP creds must stay
        if key in SECRET_ENV_EXACT or key.startswith(SECRET_ENV_PREFIXES):
            del env[key]

    bin_dir = os.path.dirname(claude_path)
    if bin_dir:
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        if bin_dir not in parts:
            env["PATH"] = os.pathsep.join([bin_dir] + parts) if parts else bin_dir
    env["CLAUDE_NO_ANALYTICS"] = "1"
    return env


def _sbpl_quote(path):
    """Escape a filesystem path for an SBPL (sandbox profile) string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_agent_argv(claude_argv, sandbox_path):
    """SEC-02(b): defense-in-depth on top of the read-only tool restriction. The
    tool set already blocks writes/bash, but a read-only run could still READ any
    absolute path (e.g. ~/.ssh, ~/.aws, other repos) and surface it in its output.
    On macOS we therefore wrap claude in `sandbox-exec` with a profile that DENIES
    file reads under $HOME -- re-permitting only the throwaway sandbox, the agent
    workspace roots, and the minimal paths claude needs to run (its own install +
    ~/.claude auth + node/npm caches). System paths (/usr, /System, ...) stay
    readable so node/claude can start; they hold no user secrets. Reads of the rest
    of $HOME are blocked, which is the real exfiltration surface.

    BEST-EFFORT -- the tier must never break: if SIDE_SANDBOX is off, or we're not
    on darwin, or `sandbox-exec` is missing, or the generated profile fails a
    synchronous pre-flight compile, we return the plain (un-wrapped) argv."""
    default_on = "1" if sys.platform == "darwin" else "0"
    flag = os.environ.get("SIDE_SANDBOX", default_on).strip().lower()
    if flag in ("", "0", "false", "no", "off"):
        return claude_argv
    if sys.platform != "darwin":
        return claude_argv  # the profile below is macOS-specific SBPL
    sbx = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not (os.path.isfile(sbx) and os.access(sbx, os.X_OK)):
        return claude_argv

    home = os.path.expanduser("~")
    readable = [
        sandbox_path, str(RUNS_ROOT), str(WORKSPACE_ROOT),
        os.path.join(home, ".claude"),
        os.path.join(home, ".config"), os.path.join(home, ".cache"),
        os.path.join(home, ".npm"), os.path.join(home, ".local"),
        os.path.join(home, ".nvm"), os.path.join(home, ".n"),
        os.path.join(home, ".asdf"), os.path.join(home, ".volta"),
        os.path.join(home, ".fnm"), os.path.join(home, ".bun"),
        os.path.join(home, "Library", "Caches"),
        os.path.join(home, "Library", "Application Support", "claude"),
        # BUGFIX (W3-A, 2026-07-28): without this the ENTIRE tier-1 agent was dead
        # on any machine where Claude Code authenticates by subscription login --
        # every run returned "Not logged in - Please run /login". Claude Code keeps
        # those credentials in the login keychain, NOT in ~/.claude/.credentials.json
        # (which does not exist on such machines), and this profile denied it.
        # Verified before/after against the unmodified file at commit c18de9f:
        # sandboxed = "Not logged in", SIDE_SANDBOX=0 = works. Keychain ITEMS remain
        # protected by securityd ACLs; this only unblocks the container file, and
        # the rest of $HOME (~/.ssh, ~/.aws, other repos) stays denied.
        os.path.join(home, "Library", "Keychains"),
    ]
    claude_file = os.path.join(home, ".claude.json")
    # A path containing a quote/newline would corrupt the profile -> bail (unwrapped).
    for p in [home, claude_file] + readable:
        if '"' in p or "\n" in p:
            return claude_argv
    allow_block = "\n".join('  (subpath "%s")' % _sbpl_quote(p) for p in readable)
    profile = (
        "(version 1)\n"
        "(allow default)\n"                        # baseline: don't fight node's needs
        '(deny file-read* (subpath "%s"))\n'       # ...but wall off all of $HOME reads
        "(allow file-read-metadata)\n"             # keep stat/traverse so paths resolve
        "(allow file-read*\n%s\n"                  # re-permit sandbox + claude essentials
        '  (literal "%s"))\n'                       # ~/.claude.json is a file, not a dir
    ) % (_sbpl_quote(home), allow_block, _sbpl_quote(claude_file))

    # Pre-flight: verify sandbox-exec accepts the profile before we depend on it.
    try:
        chk = subprocess.run(
            [sbx, "-p", profile, "/usr/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if chk.returncode != 0:
            return claude_argv
    except (OSError, subprocess.SubprocessError):
        return claude_argv
    return [sbx, "-p", profile] + claude_argv


# =====================================================================
# ACP -- Agent Client Protocol client
# https://agentclientprotocol.com/protocol/v1/overview
#
# Side is the CLIENT. The agent is a subprocess we spawn; we speak JSON-RPC 2.0
# to its stdin and read its stdout, one message per line, no embedded newlines
# (see /protocol/v1/transports). We implement protocolVersion 1.
# =====================================================================

ACP_PROTOCOL_VERSION = 1
MAX_ACP_SESSIONS = 4           # concurrent live agent subprocesses -> 429 beyond
ACP_RPC_TIMEOUT = 120          # seconds to wait on a normal agent->us RPC reply
ACP_TURN_TIMEOUT = 60 * 60     # hard ceiling on one session/prompt turn
ACP_INIT_TIMEOUT = 45          # initialize / session/new can be slow on first run
ACP_SETTLE_MS = 1200           # drain-window after session/new for modes+commands
ACP_UPDATE_CAP = 4000          # per-session ring buffer of undelivered updates
ACP_COMMANDS_CAP = 150         # slash commands surfaced per session (agents list 180+)
ACP_IDLE_TIMEOUT = 30 * 60     # idle session reaped after 30 min
ACP_STDERR_CAP = 64 * 1024     # keep last 64KB of the agent's stderr, for errors
ACP_FS_MAX_BYTES = 4 * 1024 * 1024   # cap on fs/read_text_file + fs/write_text_file
TERMINAL_OUTPUT_CAP = 1024 * 1024    # default outputByteLimit if agent omits one
TERMINAL_WAIT_MAX = 900        # hard cap on a terminal/wait_for_exit hold
# How long a session/request_permission may sit unanswered before we answer
# `cancelled` on the user's behalf. Timing out is NOT approval -- see
# AcpConnection._on_request_permission.
PERMISSION_TIMEOUT = 30 * 60

# JSON-RPC error codes we return to the agent.
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL = -32603


def _env_flag(name, default):
    """Read a boolean-ish env var. `default` is the value used when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def acp_enabled():
    """SIDE_ACP feature flag (sprint decision #3). Off -> /api/acp/* is 503 and the
    proven `claude -p` tier at /api/agent/* keeps working exactly as before."""
    return _env_flag("SIDE_ACP", True)


def acp_fs_mode():
    """SIDE_ACP_FS: 'read' (DEFAULT) | 'rw' | off.

    Controls which fs/* client capabilities we advertise. Reads and writes are
    jailed to the session root regardless -- this only decides what we tell the
    agent it may call.

    Default changed rw -> read on 2026-07-28. Tier 1 (`claude -p`) is read-only
    by tool restriction and was proven so adversarially. ACP agents getting
    write access is a real escalation, and an escalation should be a decision
    the operator makes explicitly, not one they inherit from a default. Opt in
    with SIDE_ACP_FS=rw.
    """
    raw = (os.environ.get("SIDE_ACP_FS") or "").strip().lower()
    if raw in ("0", "false", "no", "off", "none"):
        return "off"
    if raw in ("rw", "w", "write", "readwrite", "read-write"):
        return "rw"
    return "read"


def acp_terminal_enabled():
    """SIDE_ACP_TERMINAL, default OFF.

    terminal/* lets an agent run arbitrary shell commands through us. The whole
    security posture of this daemon's tier-1 agent is 'the child physically cannot
    run commands', and quietly granting that to every ACP agent would weaken an
    existing control. So the capability is implemented but not advertised unless
    the user opts in, and when it is off every terminal/* call is answered with
    METHOD_NOT_FOUND rather than silently executed."""
    return _env_flag("SIDE_ACP_TERMINAL", False)


# ---- ACP agent catalog -------------------------------------------------------
# `argv` is the documented ACP-mode invocation. `bins` are the binary names we
# look for ON DISK, in order. We never report an agent as available unless we
# actually resolved an executable file for it -- no guessing, no npx-on-demand
# (that would be a silent network install). `install` is shown to the user
# instead. Entries whose ACP flag we could not confirm from the vendor's own docs
# carry confirmed=False and are reported that way.
ACP_AGENTS = (
    {
        "id": "claude-code",
        "name": "Claude Code",
        "bins": ("claude-agent-acp", "claude-code-acp"),
        "args": (),
        "install": "npm i -g @agentclientprotocol/claude-agent-acp",
        "note": "Claude Code through the official ACP adapter (was zed-industries/claude-code-acp)",
        "confirmed": True,
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "bins": ("gemini",),
        "args": ("--acp",),
        "install": "npm i -g @google/gemini-cli",
        "note": "Google's reference ACP implementation (flag was --experimental-acp before)",
        "confirmed": True,
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "bins": ("codex-acp",),
        "args": (),
        "install": "npm i -g @agentclientprotocol/codex-acp",
        "note": "OpenAI Codex via the ACP adapter; the codex binary has no native acp mode",
        "confirmed": True,
    },
    {
        "id": "goose",
        "name": "Goose",
        "bins": ("goose",),
        "args": ("acp",),
        "install": "https://goose-docs.ai/docs/getting-started/installation",
        "note": "Block's Goose",
        "confirmed": True,
    },
    {
        "id": "openhands",
        "name": "OpenHands",
        "bins": ("openhands",),
        "args": ("acp",),
        "install": "pip install openhands-ai",
        "note": "OpenHands CLI",
        "confirmed": True,
    },
    {
        "id": "copilot",
        "name": "GitHub Copilot CLI",
        "bins": ("copilot",),
        "args": ("--acp",),
        "install": "npm i -g @github/copilot",
        "note": "Copilot CLI ACP server over stdio",
        "confirmed": True,
    },
    {
        "id": "cursor",
        "name": "Cursor CLI",
        "bins": ("cursor-agent", "agent"),
        "args": ("acp",),
        # The current Cursor binary is plainly named `agent`, which is far too
        # generic to trust on PATH, so a hit on that name only counts when the
        # resolved path or version string actually says cursor.
        "match": "cursor",
        "install": "curl https://cursor.com/install | bash",
        "note": "Cursor Agent CLI",
        "confirmed": True,
    },
    {
        "id": "qwen",
        "name": "Qwen Code",
        "bins": ("qwen",),
        "args": ("--acp",),
        "install": "npm i -g @qwen-code/qwen-code",
        "note": "Qwen Code CLI",
        "confirmed": True,
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "bins": ("opencode",),
        "args": ("acp",),
        "install": "npm i -g opencode-ai",
        "note": "sst/opencode",
        "confirmed": True,
    },
    {
        "id": "auggie",
        "name": "Augment Code",
        "bins": ("auggie",),
        "args": ("--acp",),
        "install": "npm i -g @augmentcode/auggie",
        "note": "Augment's Auggie CLI",
        "confirmed": True,
    },
    {
        "id": "amp",
        "name": "Amp",
        "bins": ("amp-acp",),
        "args": (),
        "install": "npm i -g amp-acp",
        "note": "ACP wrapper for Amp; needs the amp CLI on PATH",
        "confirmed": True,
    },
    {
        "id": "kimi",
        "name": "Kimi Code",
        "bins": ("kimi",),
        "args": ("acp",),
        "install": "npm i -g @moonshot-ai/kimi-code",
        "note": "Moonshot Kimi Code CLI",
        "confirmed": True,
    },
    {
        "id": "kiro",
        "name": "Kiro CLI",
        "bins": ("kiro-cli",),
        "args": ("acp",),
        "install": "brew install --cask kiro-cli",
        "note": "AWS Kiro CLI",
        "confirmed": True,
    },
    {
        "id": "vibe",
        "name": "Mistral Vibe",
        "bins": ("vibe-acp",),
        "args": (),
        "install": "pip install mistral-vibe",
        "note": "Mistral Vibe's dedicated ACP entrypoint",
        "confirmed": True,
    },
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "bins": ("openclaw",),
        "args": ("acp",),
        "install": "npm i -g openclaw",
        "note": "ACP bridge backed by a running OpenClaw gateway (gateway must be up)",
        "confirmed": True,
    },
)

def acp_extra_agents():
    """SIDE_ACP_EXTRA_AGENTS: JSON array of custom agent specs, so a user can point
    Side at an ACP agent this catalog has never heard of (the ecosystem is 40+
    agents and moving weekly) without editing this file.

    [{"id":"my-agent","name":"My Agent","bin":"/abs/path","args":["acp"]}]

    Only `bin` is trusted as a name-or-path; it still has to resolve to a real
    executable before the agent is ever reported as available."""
    raw = os.environ.get("SIDE_ACP_EXTRA_AGENTS", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        agent_id = item.get("id")
        binary = item.get("bin")
        if not isinstance(agent_id, str) or not SPACE_ID_RE.match(agent_id):
            continue
        if not isinstance(binary, str) or not binary:
            continue
        args = item.get("args") or []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            continue
        out.append({
            "id": agent_id,
            "name": item.get("name") if isinstance(item.get("name"), str) else agent_id,
            "bins": (binary,),
            "args": tuple(args),
            "install": "configured via SIDE_ACP_EXTRA_AGENTS",
            "note": "custom agent",
            "confirmed": False,
        })
    return tuple(out)


def acp_catalog():
    return ACP_AGENTS + acp_extra_agents()


ACP_BIN_CANDIDATE_DIRS = (
    "~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin", "~/.bun/bin",
    "~/.cargo/bin", "~/.npm-global/bin", "/usr/local/lib/node_modules/.bin",
    "/opt/homebrew/lib/node_modules/.bin",
)


def _resolve_executable(name):
    """Find `name` as a real executable file. PATH first, then a short list of
    well-known install dirs, then npm/npx caches already materialised on disk.
    Returns an absolute path or None. Never installs anything."""
    if os.path.isabs(name):
        return name if (os.path.isfile(name) and os.access(name, os.X_OK)) else None
    found = shutil.which(name)
    if found:
        return found
    for cand in ACP_BIN_CANDIDATE_DIRS:
        p = os.path.join(os.path.expanduser(cand), name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # npx keeps previously-run packages unpacked under ~/.npm/_npx/<hash>/node_modules/.bin.
    npx_root = os.path.expanduser("~/.npm/_npx")
    try:
        entries = os.listdir(npx_root)
    except OSError:
        entries = []
    for entry in entries:
        p = os.path.join(npx_root, entry, "node_modules", ".bin", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _probe_version(path, args):
    """Best-effort `--version`. Returns a short string or None. Never raises, and
    an unreadable version never downgrades availability -- we already proved the
    file exists and is executable."""
    for flag in (("--version",), ("-V",)):
        try:
            proc = subprocess.run(
                [path] + list(args) + list(flag),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=6, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            line = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
            if line:
                return line[0].strip()[:120] or None
    return None


def _resolve_agent_path(spec):
    """(path, reason). A spec carrying `match` must additionally prove the binary
    it found really is that vendor's -- the substring has to appear in the resolved
    path or in its --version output. Under-reporting beats claiming an agent is
    installed when all we found was a generically-named executable."""
    needle = spec.get("match")
    generic_hit = None
    for name in spec["bins"]:
        path = _resolve_executable(name)
        if not path:
            continue
        if not needle:
            return path, None
        blob = (path + " " + (_probe_version(path, ()) or "")).lower()
        if needle.lower() in blob:
            return path, None
        generic_hit = path
    if generic_hit:
        return None, ("found %s but could not confirm it is %s"
                      % (generic_hit, spec["name"]))
    return None, "not installed (looked for: %s)" % ", ".join(spec["bins"])


_ACP_DETECT_LOCK = threading.Lock()
_ACP_DETECT_CACHE = {"at": 0.0, "agents": None}
ACP_DETECT_TTL = 30.0


def acp_detect_agents(force=False):
    """Resolve every catalog entry against this machine. Cached for 30s so the
    UI can poll cheaply. An entry is available ONLY when we resolved a real
    executable; otherwise it reports available:false plus a reason."""
    with _ACP_DETECT_LOCK:
        now = time.time()
        cached = _ACP_DETECT_CACHE["agents"]
        if cached is not None and not force and (now - _ACP_DETECT_CACHE["at"]) < ACP_DETECT_TTL:
            return cached
        out = []
        for spec in acp_catalog():
            path, err = _resolve_agent_path(spec)
            entry = {
                "id": spec["id"],
                "name": spec["name"],
                "cmd": None,
                "available": False,
                "version": None,
                "reason": None,
                "install": spec["install"],
                "note": spec["note"],
                # False = we could not confirm this agent's exact ACP invocation
                # from its own docs. It is still offered, but flagged, so nothing
                # here silently pretends to more certainty than it has.
                "invocationConfirmed": bool(spec.get("confirmed")),
            }
            if not path:
                entry["reason"] = err
            else:
                entry["available"] = True
                entry["cmd"] = " ".join([path] + list(spec["args"]))
                entry["version"] = _probe_version(path, ())
            out.append(entry)
        _ACP_DETECT_CACHE["agents"] = out
        _ACP_DETECT_CACHE["at"] = now
        return out


def acp_agent_argv(agent_id):
    """(argv, error). Re-resolves at spawn time rather than trusting the cache."""
    spec = None
    for cand in acp_catalog():
        if cand["id"] == agent_id:
            spec = cand
            break
    if spec is None:
        return None, "unknown agent"
    path, err = _resolve_agent_path(spec)
    if path:
        return [path] + list(spec["args"]), None
    return None, "agent '%s' unavailable: %s" % (agent_id, err)


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


# =====================================================================
# ACP runtime: sandbox profile, terminals, JSON-RPC connection, sessions
# =====================================================================

def build_acp_env(agent_path):
    """Child env for an ACP agent. Same third-party-secret scrub as the tier-1
    child (SEC-02a) -- we reuse build_child_env so the two tiers can never drift
    apart -- plus ACP-specific markers."""
    env = build_child_env(agent_path)
    env["ACP_CLIENT"] = "side"
    env["SIDE_ACP_CLIENT_VERSION"] = VERSION
    return env


def build_acp_sandbox_argv(argv, session_root, extra_read=(), extra_write=()):
    """SEC-02(b), extended to tier 2. Returns (argv, sandboxed_bool).

    An ACP agent is a full agent with its own write and exec tools, so unlike the
    read-only tier-1 child we cannot rely on tool restriction alone. On macOS we
    wrap it in sandbox-exec with a profile that:
      * denies file READS under $HOME, re-permitting the session root, the agent's
        own install/config/caches, and any caller-supplied extra read paths;
      * denies file WRITES under $HOME, re-permitting only the session root and
        caller-supplied extra write paths (a Space's backing .git/worktrees dir).

    BEST EFFORT, exactly like tier 1: if SIDE_SANDBOX is off, we are not on
    darwin, sandbox-exec is missing, or the profile fails its pre-flight compile,
    we return the plain argv and report sandboxed:false. The caller surfaces that
    boolean to the UI -- an unsandboxed agent is never presented as a sandboxed
    one."""
    default_on = "1" if sys.platform == "darwin" else "0"
    flag = os.environ.get("SIDE_SANDBOX", default_on).strip().lower()
    if flag in ("", "0", "false", "no", "off"):
        return argv, False
    if sys.platform != "darwin":
        return argv, False
    sbx = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not (os.path.isfile(sbx) and os.access(sbx, os.X_OK)):
        return argv, False

    home = os.path.expanduser("~")
    writable = [str(session_root), str(RUNS_ROOT), str(SPACES_ROOT)] + [str(p) for p in extra_write]
    readable = writable + [
        str(WORKSPACE_ROOT),
        os.path.join(home, ".claude"), os.path.join(home, ".codex"),
        os.path.join(home, ".gemini"), os.path.join(home, ".config"),
        os.path.join(home, ".cache"), os.path.join(home, ".npm"),
        os.path.join(home, ".local"), os.path.join(home, ".nvm"),
        os.path.join(home, ".n"), os.path.join(home, ".asdf"),
        os.path.join(home, ".volta"), os.path.join(home, ".fnm"),
        os.path.join(home, ".bun"), os.path.join(home, ".cargo"),
        os.path.join(home, "Library", "Caches"),
        os.path.join(home, "Library", "Application Support"),
        os.path.join(home, "Library", "Preferences"),
        # Claude Code (and several other agents) keep their OAuth credentials in
        # the login keychain, not in a dotfile -- verified on this machine, where
        # ~/.claude/.credentials.json does not exist. Without this the agent
        # initializes and creates a session fine and then fails every prompt with
        # "Authentication required". Keychain ITEMS stay protected by securityd
        # ACLs; this only makes the container file readable, as it is for every
        # normal app. Everything else under $HOME stays denied.
        os.path.join(home, "Library", "Keychains"),
    ] + [str(p) for p in extra_read]
    # Agents keep mutable state (session logs, auth refresh) in these.
    writable = writable + [
        os.path.join(home, ".claude"), os.path.join(home, ".codex"),
        os.path.join(home, ".gemini"), os.path.join(home, ".cache"),
        os.path.join(home, ".npm"),
        os.path.join(home, "Library", "Caches"),
        os.path.join(home, "Library", "Application Support"),
    ]
    claude_file = os.path.join(home, ".claude.json")
    for p in [home, claude_file] + readable + writable:
        if '"' in p or "\n" in p:
            return argv, False  # a quote/newline would corrupt the profile
    read_block = "\n".join('  (subpath "%s")' % _sbpl_quote(p) for p in readable)
    write_block = "\n".join('  (subpath "%s")' % _sbpl_quote(p) for p in writable)
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        '(deny file-read* (subpath "%s"))\n'
        '(deny file-write* (subpath "%s"))\n'
        "(allow file-read-metadata)\n"
        "(allow file-read*\n%s\n"
        '  (literal "%s"))\n'
        "(allow file-write*\n%s\n"
        '  (literal "%s"))\n'
    ) % (
        _sbpl_quote(home), _sbpl_quote(home), read_block, _sbpl_quote(claude_file),
        write_block, _sbpl_quote(claude_file),
    )
    try:
        chk = subprocess.run(
            [sbx, "-p", profile, "/usr/bin/true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6,
        )
        if chk.returncode != 0:
            return argv, False
    except (OSError, subprocess.SubprocessError):
        return argv, False
    return [sbx, "-p", profile] + argv, True


class AcpTerminal:
    """One `terminal/create` process. Output is drained by a dedicated thread into
    a byte-capped buffer, so the child can never block on a full pipe and we never
    do a blocking read against a dead process."""

    def __init__(self, terminal_id, argv, cwd, env, byte_limit, sandboxed):
        self.id = terminal_id
        self.byte_limit = byte_limit
        self.sandboxed = sandboxed
        self.lock = threading.Lock()
        self.buf = ""
        self.truncated = False
        self.exit_code = None
        self.signal_name = None
        self.exited = threading.Event()
        self.released = False
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                with self.lock:
                    self.buf += line
                    if len(self.buf) > self.byte_limit:
                        # Spec: truncate from the START, on a character boundary.
                        self.buf = self.buf[-self.byte_limit:]
                        self.truncated = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.proc.stdout.close()
            except OSError:
                pass
        code = self.proc.wait()  # reap
        with self.lock:
            if code is not None and code < 0:
                self.exit_code = None
                try:
                    self.signal_name = signal.Signals(-code).name
                except (ValueError, AttributeError):
                    self.signal_name = str(-code)
            else:
                self.exit_code = code
        self.exited.set()

    def snapshot(self):
        with self.lock:
            out = {"output": self.buf, "truncated": self.truncated}
            if self.exited.is_set():
                out["exitStatus"] = {"exitCode": self.exit_code, "signal": self.signal_name}
        return out

    def wait_for_exit(self, closed_event, timeout=TERMINAL_WAIT_MAX):
        """Wait for exit, but never forever and never past connection teardown."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.exited.wait(0.25):
                break
            if closed_event is not None and closed_event.is_set():
                break
        with self.lock:
            return {"exitCode": self.exit_code, "signal": self.signal_name}

    def kill(self):
        proc = self.proc
        if proc.poll() is not None:
            return
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

    def release(self):
        self.released = True
        self.kill()
        self.exited.wait(2)


class PendingPermission:
    """One in-flight session/request_permission. The agent's request thread blocks
    on `answered` until a human decides. Nothing in this file sets `outcome` to an
    allow value on its own."""

    def __init__(self, request_id, title, options, tool_call):
        self.id = request_id
        self.title = title
        self.options = options
        self.tool_call = tool_call
        self.answered = threading.Event()
        self.outcome = None       # {"outcome":"selected","optionId":...} | {"outcome":"cancelled"}
        self.decided_by = None    # "user" | "cancelled" | "timeout" | "closed"
        self.at = time.time()


def _pick_permission_option(options, outcome, explicit_id):
    """Map Side's allow/deny onto one of the agent's offered optionIds.

    Returns (optionId, error). If the agent offered no option of the requested
    polarity we return an error and the caller answers `cancelled`. We NEVER fall
    back to an option of the opposite polarity -- answering 'allow' to a denial
    would be fabricating consent."""
    opts = [o for o in options if isinstance(o, dict)]
    if explicit_id:
        for o in opts:
            if o.get("optionId") == explicit_id:
                return explicit_id, None
        return None, "optionId was not offered by the agent"
    wanted = ("allow_once", "allow_always") if outcome == "allow" else ("reject_once", "reject_always")
    for kind in wanted:
        for o in opts:
            if o.get("kind") == kind and o.get("optionId"):
                return o.get("optionId"), None
    return None, "agent offered no '%s' option" % outcome


class AcpConnection:
    """JSON-RPC 2.0 peer over a spawned agent's stdio.

    Threading model
      * one reader thread owns stdout and never blocks on anything else;
      * one stderr drain thread (an undrained stderr pipe deadlocks the child);
      * every INBOUND request is dispatched to its own short-lived worker, because
        session/request_permission and terminal/wait_for_exit block for minutes and
        must not stall the reader;
      * outbound requests wait on a per-request Event with a timeout AND are woken
        by `closed`, so a dead child fails them immediately instead of hanging.
    """

    def __init__(self, session):
        self.session = session
        self.proc = None
        self.closed = threading.Event()
        self.close_reason = None
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending = {}          # our outbound id -> {"event","result","error"}
        self._pending_lock = threading.Lock()
        self._stderr = deque(maxlen=400)
        self._stderr_bytes = 0
        self.terminals = {}
        self._term_lock = threading.Lock()
        self._term_seq = 0

    # -- lifecycle -------------------------------------------------------
    def spawn(self, argv, cwd, env):
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _stderr_loop(self):
        try:
            for line in self.proc.stderr:
                if self._stderr_bytes < ACP_STDERR_CAP:
                    self._stderr.append(line)
                    self._stderr_bytes += len(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.proc.stderr.close()
            except OSError:
                pass

    def stderr_tail(self, lines=20):
        return "".join(list(self._stderr)[-lines:]).strip()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    # Real agents print banners, doctor warnings and progress bars
                    # on stdout before/around the protocol stream. The transport
                    # says messages are newline-delimited JSON, so a line that is
                    # not JSON is not a message -- keep it for diagnostics and
                    # carry on rather than tearing the session down.
                    self._stderr.append("[non-json stdout] " + line[:500] + "\n")
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception as exc:  # a malformed frame must not kill the loop
                    self._stderr.append("[dispatch error] %s\n" % exc)
        except (OSError, ValueError) as exc:
            self.close_reason = "stdout closed: %s" % exc
        finally:
            self._shutdown("agent stdout closed")

    def _shutdown(self, reason):
        if self.closed.is_set():
            return
        if self.close_reason is None:
            self.close_reason = reason
        self.closed.set()
        # Fail every outbound request rather than let a caller hang on a dead child.
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w["error"] = {"code": RPC_INTERNAL, "message": "agent connection closed"}
            w["event"].set()
        # Release any permission still waiting -- as `cancelled`, never as allowed.
        self.session.cancel_all_permissions("closed")
        with self._term_lock:
            terms = list(self.terminals.values())
            self.terminals.clear()
        for t in terms:
            try:
                t.release()
            except Exception:
                pass
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                self.proc.wait(timeout=5)  # reap; no zombies
            except (OSError, subprocess.SubprocessError):
                pass
            for stream in (self.proc.stdin,):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        self.session.on_closed(self.close_reason)

    def terminate(self, reason="stopped by user"):
        self.close_reason = reason
        self._shutdown(reason)

    # -- transport -------------------------------------------------------
    def _write(self, obj):
        if self.closed.is_set():
            return False
        # No embedded newlines: json.dumps escapes them inside strings already,
        # and we never pass indent=, so one dump == one line.
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError) as exc:
                self.close_reason = "agent stdin closed: %s" % exc
                threading.Thread(target=self._shutdown, args=("agent stdin closed",),
                                 daemon=True).start()
                return False

    def notify(self, method, params):
        return self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=ACP_RPC_TIMEOUT):
        """Outbound request. Returns (result, error). Never blocks past `timeout`
        and returns immediately if the child dies."""
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        waiter = {"event": threading.Event(), "result": None, "error": None}
        with self._pending_lock:
            self._pending[rid] = waiter
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None, {"code": RPC_INTERNAL, "message": "agent connection closed"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if waiter["event"].wait(0.25):
                return waiter["result"], waiter["error"]
            if self.closed.is_set():
                break
        with self._pending_lock:
            self._pending.pop(rid, None)
        if self.closed.is_set():
            return None, {"code": RPC_INTERNAL, "message": "agent connection closed"}
        return None, {"code": RPC_INTERNAL, "message": "timed out after %ss" % timeout}

    def _respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self._write(msg)

    def _dispatch(self, msg):
        if "method" in msg and "id" in msg and msg["id"] is not None:
            # Inbound REQUEST. Off the reader thread -- these block.
            threading.Thread(
                target=self._handle_request,
                args=(msg["id"], msg.get("method"), msg.get("params") or {}),
                daemon=True,
            ).start()
            return
        if "method" in msg:
            self._handle_notification(msg.get("method"), msg.get("params") or {})
            return
        rid = msg.get("id")
        with self._pending_lock:
            waiter = self._pending.pop(rid, None)
        if waiter is None:
            return
        waiter["result"] = msg.get("result")
        waiter["error"] = msg.get("error")
        waiter["event"].set()

    def _handle_notification(self, method, params):
        if method == "session/update":
            self.session.ingest_update(params.get("update") or {})
        # Any other agent-side notification is informational; ignore it rather
        # than error (notifications must not be answered).

    def _handle_request(self, rid, method, params):
        try:
            if method == "session/request_permission":
                result, error = self._on_request_permission(params)
            elif method == "fs/read_text_file":
                result, error = self._on_fs_read(params)
            elif method == "fs/write_text_file":
                result, error = self._on_fs_write(params)
            elif method and method.startswith("terminal/"):
                result, error = self._on_terminal(method, params)
            else:
                # Includes elicitation/create: we do not advertise the elicitation
                # capability, so per the spec the agent must not call it. Saying
                # METHOD_NOT_FOUND is honest; inventing an answer would not be.
                result, error = None, {"code": RPC_METHOD_NOT_FOUND,
                                       "message": "method not supported by Side: %s" % method}
        except Exception as exc:
            result, error = None, {"code": RPC_INTERNAL, "message": str(exc)}
        self._respond(rid, result=result, error=error)

    # -- client methods --------------------------------------------------
    def _on_request_permission(self, params):
        """THE APPROVAL GATE.

        This blocks the agent until a human answers through POST /api/acp/permission.
        There is no auto-approve path, no allowlist, no 'remember this' shortcut and
        no timeout-means-yes: every exit from this function other than an explicit
        human allow answers the agent with reject/cancelled."""
        options = params.get("options") or []
        tool_call = params.get("toolCall") or {}
        title = tool_call.get("title") or "The agent is asking permission to continue"
        pending = self.session.open_permission(title, options, tool_call)
        answered = pending.answered.wait(PERMISSION_TIMEOUT)
        if not answered:
            self.session.close_permission(pending.id, "timeout")
            self.session.emit({
                "type": "permission_resolved", "requestId": pending.id,
                "outcome": "cancelled",
                "reason": "no answer within %d minutes" % (PERMISSION_TIMEOUT // 60),
            })
            return {"outcome": {"outcome": "cancelled"}}, None
        outcome = pending.outcome or {"outcome": "cancelled"}
        return {"outcome": outcome}, None

    def _on_fs_read(self, params):
        mode = acp_fs_mode()
        if mode == "off":
            return None, {"code": RPC_METHOD_NOT_FOUND, "message": "fs access disabled"}
        path = params.get("path")
        if not isinstance(path, str) or not path:
            return None, {"code": RPC_INVALID_PARAMS, "message": "path is required"}
        real, err = self.session.jail_path(path, for_write=False)
        if err:
            return None, {"code": RPC_INVALID_PARAMS, "message": err}
        try:
            if real.stat().st_size > ACP_FS_MAX_BYTES:
                return None, {"code": RPC_INVALID_PARAMS, "message": "file too large"}
            text = real.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, {"code": RPC_INTERNAL, "message": "read failed: %s" % exc}
        line = params.get("line")
        limit = params.get("limit")
        if isinstance(line, int) or isinstance(limit, int):
            rows = text.splitlines(True)
            start = (line - 1) if isinstance(line, int) and line > 0 else 0  # 1-based
            end = (start + limit) if isinstance(limit, int) and limit >= 0 else len(rows)
            text = "".join(rows[start:end])
        self.session.emit({"type": "fs", "op": "read", "path": str(real)})
        return {"content": text}, None

    def _on_fs_write(self, params):
        if acp_fs_mode() != "rw":
            return None, {"code": RPC_METHOD_NOT_FOUND, "message": "fs writes disabled"}
        path = params.get("path")
        content = params.get("content")
        if not isinstance(path, str) or not path:
            return None, {"code": RPC_INVALID_PARAMS, "message": "path is required"}
        if not isinstance(content, str):
            return None, {"code": RPC_INVALID_PARAMS, "message": "content must be a string"}
        if len(content.encode("utf-8")) > ACP_FS_MAX_BYTES:
            return None, {"code": RPC_INVALID_PARAMS, "message": "content too large"}
        real, err = self.session.jail_path(path, for_write=True)
        if err:
            return None, {"code": RPC_INVALID_PARAMS, "message": err}
        try:
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text(content, encoding="utf-8")
        except OSError as exc:
            return None, {"code": RPC_INTERNAL, "message": "write failed: %s" % exc}
        self.session.emit({"type": "fs", "op": "write", "path": str(real)})
        return None, None

    def _on_terminal(self, method, params):
        if not acp_terminal_enabled():
            return None, {"code": RPC_METHOD_NOT_FOUND,
                          "message": "terminal access is disabled (set SIDE_ACP_TERMINAL=1)"}
        if method == "terminal/create":
            return self._terminal_create(params)
        term_id = params.get("terminalId")
        with self._term_lock:
            term = self.terminals.get(term_id)
        if term is None:
            return None, {"code": RPC_INVALID_PARAMS, "message": "unknown terminalId"}
        if method == "terminal/output":
            return term.snapshot(), None
        if method == "terminal/wait_for_exit":
            res = term.wait_for_exit(self.closed)
            self.session.emit({"type": "terminal", "terminalId": term.id,
                               "output": term.snapshot().get("output", ""),
                               "exitStatus": res})
            return res, None
        if method == "terminal/kill":
            term.kill()
            return None, None
        if method == "terminal/release":
            with self._term_lock:
                self.terminals.pop(term_id, None)
            term.release()
            return None, None
        return None, {"code": RPC_METHOD_NOT_FOUND, "message": method}

    def _terminal_create(self, params):
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return None, {"code": RPC_INVALID_PARAMS, "message": "command is required"}
        args = params.get("args") or []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            return None, {"code": RPC_INVALID_PARAMS, "message": "args must be strings"}
        cwd_param = params.get("cwd")
        if cwd_param:
            cwd_real, err = self.session.jail_path(cwd_param, for_write=False, allow_dir=True)
            if err:
                return None, {"code": RPC_INVALID_PARAMS, "message": "cwd: " + err}
            cwd = str(cwd_real)
        else:
            cwd = str(self.session.cwd)
        env = build_acp_env(command)
        for item in (params.get("env") or []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                env[item["name"]] = str(item.get("value", ""))
        limit = params.get("outputByteLimit")
        if not isinstance(limit, int) or limit <= 0:
            limit = TERMINAL_OUTPUT_CAP
        limit = min(limit, TERMINAL_OUTPUT_CAP)
        argv, sandboxed = build_acp_sandbox_argv(
            [command] + list(args), self.session.cwd,
            extra_read=self.session.extra_read, extra_write=self.session.extra_write,
        )
        with self._term_lock:
            self._term_seq += 1
            term_id = "term_%s_%d" % (self.session.id[-6:], self._term_seq)
        try:
            term = AcpTerminal(term_id, argv, cwd, env, limit, sandboxed)
        except OSError as exc:
            return None, {"code": RPC_INTERNAL, "message": "spawn failed: %s" % exc}
        with self._term_lock:
            self.terminals[term_id] = term
        self.session.emit({"type": "terminal", "terminalId": term_id, "output": "",
                           "command": " ".join([command] + list(args)),
                           "sandboxed": sandboxed})
        return {"terminalId": term_id}, None


class AcpSession:
    """One live agent subprocess plus the buffered stream the browser polls."""

    def __init__(self, session_id, agent_id, cwd, space_id, extra_read, extra_write):
        self.id = session_id
        self.agent_id = agent_id
        self.cwd = Path(cwd)
        self.space_id = space_id
        self.extra_read = list(extra_read)
        self.extra_write = list(extra_write)
        self.acp_session_id = None       # the id the AGENT gave us
        self.agent_info = {}
        self.agent_caps = {}
        self.conn = AcpConnection(self)
        self.sandboxed = False
        self.created = time.time()
        self.touched = time.time()
        self.state = "starting"          # starting|idle|running|closed
        self.stop_reason = None
        self.closed_reason = None
        self.modes = []
        self.current_mode = None
        self.commands = []
        self.usage = None
        self._updates = deque(maxlen=ACP_UPDATE_CAP)
        self._seq = 0
        self._lock = threading.Lock()
        self._permissions = {}
        self._perm_seq = 0
        self._turn = None

    # -- update stream ---------------------------------------------------
    def emit(self, update):
        """Queue one normalised update for the next /api/acp/poll."""
        with self._lock:
            self._seq += 1
            update["seq"] = self._seq
            update["at"] = int(time.time() * 1000)
            self._updates.append(update)
            self.touched = time.time()

    def drain(self):
        with self._lock:
            out = list(self._updates)
            self._updates.clear()
            self.touched = time.time()
            return out

    def ingest_update(self, update):
        """Map an ACP `session/update` onto the frozen Wave-3 poll shapes.

        The wire shapes are the spec's (sessionUpdate discriminators); the shapes we
        hand the browser are the ones frozen in the sprint contract. Anything we
        have no frozen shape for is passed through as {type:"raw"} with the original
        payload intact rather than dropped."""
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "user_message_chunk", "agent_thought_chunk"):
            content = update.get("content") or {}
            role = {"agent_message_chunk": "agent", "user_message_chunk": "user",
                    "agent_thought_chunk": "thought"}[kind]
            item = {"type": "message_chunk", "text": content.get("text") or "", "role": role}
            if content.get("type") and content.get("type") != "text":
                # Non-text content blocks carry no .text; say so instead of
                # rendering an empty bubble as if the agent said nothing.
                item["contentType"] = content.get("type")
                item["raw"] = content
            if update.get("messageId"):
                item["messageId"] = update.get("messageId")
            self.emit(item)
        elif kind == "plan":
            entries = []
            for e in (update.get("entries") or []):
                if isinstance(e, dict):
                    entries.append({"content": e.get("content") or "",
                                    "status": e.get("status") or "pending",
                                    "priority": e.get("priority")})
            self.emit({"type": "plan", "entries": entries})
        elif kind in ("tool_call", "tool_call_update"):
            item = {"type": "tool_call",
                    "id": update.get("toolCallId"),
                    "title": update.get("title"),
                    "status": update.get("status"),
                    "kind": update.get("kind"),
                    # tool_call_update carries only the changed fields, so the UI
                    # must merge by id rather than replace.
                    "update": kind == "tool_call_update"}
            if update.get("content"):
                item["content"] = update.get("content")
            if update.get("locations"):
                item["locations"] = update.get("locations")
            self.emit(item)
        elif kind == "current_mode_update":
            self.current_mode = update.get("modeId")
            self.emit({"type": "mode", "mode": update.get("modeId")})
        elif kind == "available_commands_update":
            # Claude Code advertised 184 commands on this machine, which made the
            # session response a 125KB JSON blob. Cap the list and clip the prose
            # so one chatty agent cannot wedge the UI.
            cmds = []
            for c in (update.get("availableCommands") or []):
                if isinstance(c, dict) and len(cmds) < ACP_COMMANDS_CAP:
                    desc = c.get("description") or ""
                    cmds.append({"name": c.get("name"),
                                 "description": desc[:200],
                                 "hint": (c.get("input") or {}).get("hint")})
            total = len(update.get("availableCommands") or [])
            self.commands = cmds
            self.emit({"type": "commands", "commands": cmds, "total": total,
                       "truncated": total > len(cmds)})
        elif kind == "usage_update":
            # `used`/`size` are CONTEXT WINDOW numbers, not spend. Forward the
            # raw token counts too when the agent reports them, so the client
            # can feed SideBudget.record. Without these the budget ledger can
            # never see an ACP session at all. Agents spell these differently,
            # so accept the common variants and pass through None otherwise --
            # the client records nothing rather than estimating.
            def _tok(*names):
                for n in names:
                    v = update.get(n)
                    if isinstance(v, (int, float)):
                        return v
                return None
            in_tok = _tok("inputTokens", "input_tokens", "promptTokens", "prompt_tokens")
            out_tok = _tok("outputTokens", "output_tokens", "completionTokens", "completion_tokens")
            self.usage = {"used": update.get("used"), "size": update.get("size"),
                          "cost": update.get("cost"),
                          "inputTokens": in_tok, "outputTokens": out_tok}
            self.emit({"type": "usage", "used": update.get("used"),
                       "size": update.get("size"), "cost": update.get("cost"),
                       "inputTokens": in_tok, "outputTokens": out_tok})
        else:
            self.emit({"type": "raw", "sessionUpdate": kind, "raw": update})

    # -- permissions -----------------------------------------------------
    def open_permission(self, title, options, tool_call):
        with self._lock:
            self._perm_seq += 1
            req_id = "perm_%s_%d" % (self.id[-6:], self._perm_seq)
        clean = []
        for o in options:
            if isinstance(o, dict) and o.get("optionId"):
                clean.append({"optionId": o.get("optionId"), "name": o.get("name") or o.get("optionId"),
                              "kind": o.get("kind")})
        pending = PendingPermission(req_id, title, clean, tool_call)
        with self._lock:
            self._permissions[req_id] = pending
        self.emit({"type": "permission_request", "requestId": req_id, "title": title,
                   "options": clean, "toolCall": tool_call})
        return pending

    def pending_permissions(self):
        with self._lock:
            return [p for p in self._permissions.values() if not p.answered.is_set()]

    def answer_permission(self, request_id, outcome, option_id=None):
        """Called from the HTTP thread when a human decides. Returns (ok, error)."""
        with self._lock:
            pending = self._permissions.get(request_id)
        if pending is None:
            return False, "unknown requestId"
        if pending.answered.is_set():
            return False, "already answered"
        if outcome not in ("allow", "deny"):
            return False, "outcome must be allow or deny"
        picked, err = _pick_permission_option(pending.options, outcome, option_id)
        if err:
            # We will not substitute a different polarity. Tell the agent the turn
            # was cancelled and tell the user why.
            pending.outcome = {"outcome": "cancelled"}
            pending.decided_by = "user"
            pending.answered.set()
            self.emit({"type": "permission_resolved", "requestId": request_id,
                       "outcome": "cancelled", "reason": err})
            return False, err
        pending.outcome = {"outcome": "selected", "optionId": picked}
        pending.decided_by = "user"
        pending.answered.set()
        self.emit({"type": "permission_resolved", "requestId": request_id,
                   "outcome": outcome, "optionId": picked})
        return True, None

    def close_permission(self, request_id, why):
        with self._lock:
            pending = self._permissions.get(request_id)
        if pending is not None and not pending.answered.is_set():
            pending.outcome = {"outcome": "cancelled"}
            pending.decided_by = why
            pending.answered.set()

    def cancel_all_permissions(self, why):
        """Spec: on cancellation the client MUST answer every outstanding
        request_permission with the `cancelled` outcome."""
        for pending in self.pending_permissions():
            pending.outcome = {"outcome": "cancelled"}
            pending.decided_by = why
            pending.answered.set()
            self.emit({"type": "permission_resolved", "requestId": pending.id,
                       "outcome": "cancelled", "reason": why})

    # -- path jail -------------------------------------------------------
    def jail_path(self, path, for_write, allow_dir=False):
        """(Path, error). ACP paths are absolute. We resolve symlinks and require
        the result to sit under this session's root AND under a Side root, so an
        agent cannot read ~/.ssh or write outside its Space by asking nicely."""
        if not isinstance(path, str) or not path:
            return None, "path is required"
        if "\x00" in path:
            return None, "null byte in path"
        if not os.path.isabs(path):
            return None, "path must be absolute"
        try:
            candidate = Path(path).resolve()
        except (OSError, RuntimeError) as exc:
            return None, "cannot resolve path: %s" % exc
        try:
            candidate.relative_to(self.cwd.resolve())
        except ValueError:
            return None, "path escapes the session root"
        under_side = False
        for root in SESSION_ROOTS:
            try:
                candidate.relative_to(root)
                under_side = True
                break
            except ValueError:
                continue
        if not under_side:
            return None, "path escapes the Side workspace"
        if for_write and candidate.is_dir():
            return None, "path is a directory"
        if not for_write and not allow_dir and candidate.exists() and candidate.is_dir():
            return None, "path is a directory"
        return candidate, None

    # -- lifecycle -------------------------------------------------------
    def on_closed(self, reason):
        self.state = "closed"
        self.closed_reason = reason
        self.emit({"type": "closed", "reason": reason})

    def snapshot(self):
        return {
            "sessionId": self.id,
            "agent": self.agent_id,
            "acpSessionId": self.acp_session_id,
            "cwd": str(self.cwd),
            "spaceId": self.space_id,
            "state": self.state,
            "sandboxed": self.sandboxed,
            "modes": self.modes,
            "currentMode": self.current_mode,
            "commands": self.commands,
            "usage": self.usage,
        }


class AcpSessionManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def live_count(self):
        with self._lock:
            return len([s for s in self._sessions.values() if s.state != "closed"])

    def sessions_for_space(self, space_id):
        with self._lock:
            return [s for s in self._sessions.values()
                    if s.space_id == space_id and s.state != "closed"]

    def create(self, agent_id, cwd, space_id, extra_read, extra_write):
        """Spawn the agent, run initialize + session/new, and return (session, error)."""
        if self.live_count() >= MAX_ACP_SESSIONS:
            return None, ("busy", "too many live agent sessions (max %d)" % MAX_ACP_SESSIONS)
        argv, err = acp_agent_argv(agent_id)
        if err:
            return None, ("unavailable", err)
        session_id = "acps_" + uuid.uuid4().hex[:16]
        session = AcpSession(session_id, agent_id, cwd, space_id, extra_read, extra_write)
        sandbox_argv, sandboxed = build_acp_sandbox_argv(
            argv, session.cwd, extra_read=extra_read, extra_write=extra_write)
        session.sandboxed = sandboxed
        env = build_acp_env(argv[0])
        try:
            session.conn.spawn(sandbox_argv, str(session.cwd), env)
        except OSError as exc:
            return None, ("spawn", "could not start %s: %s" % (agent_id, exc))
        with self._lock:
            self._sessions[session_id] = session

        fs_mode = acp_fs_mode()
        caps = {
            "fs": {"readTextFile": fs_mode in ("read", "rw"),
                   "writeTextFile": fs_mode == "rw"},
            # terminal is opt-in; we must not advertise what we will refuse to do.
            "terminal": acp_terminal_enabled(),
        }
        result, rpc_err = session.conn.request("initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": caps,
            "clientInfo": {"name": "side", "title": "Side", "version": VERSION},
        }, timeout=ACP_INIT_TIMEOUT)
        if rpc_err is not None:
            detail = session.conn.stderr_tail()
            session.conn.terminate("initialize failed")
            self._forget(session_id)
            return None, ("handshake", "initialize failed: %s%s" % (
                rpc_err.get("message"), ("\n" + detail) if detail else ""))
        version = (result or {}).get("protocolVersion")
        if version != ACP_PROTOCOL_VERSION:
            # Spec: if we cannot speak the version the agent chose, close the
            # connection and say so. We do not pretend to negotiate.
            session.conn.terminate("protocol version mismatch")
            self._forget(session_id)
            return None, ("handshake",
                          "agent speaks ACP v%s, Side speaks v%d" % (version, ACP_PROTOCOL_VERSION))
        session.agent_info = (result or {}).get("agentInfo") or {}
        session.agent_caps = (result or {}).get("agentCapabilities") or {}
        auth_methods = (result or {}).get("authMethods") or []

        new_result, rpc_err = session.conn.request("session/new", {
            "cwd": str(session.cwd),
            "mcpServers": [],
        }, timeout=ACP_INIT_TIMEOUT)
        if rpc_err is not None:
            detail = session.conn.stderr_tail()
            hint = ""
            if auth_methods:
                hint = " (agent advertises authMethods %s -- it may need `authenticate` first, " \
                       "which Side does not drive yet)" % json.dumps(
                           [m.get("id") if isinstance(m, dict) else m for m in auth_methods])
            session.conn.terminate("session/new failed")
            self._forget(session_id)
            return None, ("handshake", "session/new failed: %s%s%s" % (
                rpc_err.get("message"), hint, ("\n" + detail) if detail else ""))
        session.acp_session_id = (new_result or {}).get("sessionId")
        modes = (new_result or {}).get("modes") or {}
        session.current_mode = modes.get("currentModeId")
        session.modes = modes.get("availableModes") or []
        # Commands arrive as an available_commands_update notification just after
        # session/new, so give the agent a short beat to send it before we answer.
        deadline = time.time() + (ACP_SETTLE_MS / 1000.0)
        while time.time() < deadline and not session.commands:
            if session.conn.closed.is_set():
                break
            time.sleep(0.05)
        session.state = "idle"
        return session, None

    def _forget(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    def prompt(self, session, text):
        """Fire session/prompt on a worker so the HTTP call can return {ok:true}
        immediately, per the frozen contract."""
        if session.state == "closed":
            return False, "session is closed"
        if session.state == "running":
            return False, "a turn is already running"
        session.state = "running"
        session.stop_reason = None

        def _run():
            result, rpc_err = session.conn.request(
                "session/prompt",
                {"sessionId": session.acp_session_id,
                 "prompt": [{"type": "text", "text": text}]},
                timeout=ACP_TURN_TIMEOUT,   # a turn can legitimately run for a while
            )
            if rpc_err is not None:
                session.stop_reason = "error"
                session.emit({"type": "error", "message": rpc_err.get("message")})
            else:
                session.stop_reason = (result or {}).get("stopReason") or "end_turn"
                session.emit({"type": "stop", "stopReason": session.stop_reason})
            if session.state != "closed":
                session.state = "idle"

        session._turn = threading.Thread(target=_run, daemon=True)
        session._turn.start()
        return True, None

    def stop(self, session, cancel_only):
        """Stop means stop: cancel any in-flight turn AND tear the session down,
        so pressing stop once always ends the agent subprocess.

        `cancelOnly:true` keeps the session alive for another prompt. That used to
        be the default and it leaked -- a client that stopped a still-running turn
        left the agent resident until the 30-minute idle reap.

        Either way, cancelling resolves every outstanding permission request as
        `cancelled`, which the spec requires."""
        had_turn = session.state == "running"
        if had_turn:
            session.conn.notify("session/cancel", {"sessionId": session.acp_session_id})
            session.cancel_all_permissions("cancelled")
            session.emit({"type": "cancelled"})
            # Give the agent its chance to answer session/prompt with `cancelled`
            # before we pull the process out from under it -- but never hang on it.
            turn = session._turn
            if turn is not None:
                turn.join(2.0)
        if cancel_only:
            return {"cancelled": had_turn, "closed": False}
        session.conn.terminate("stopped by user")
        self._forget(session.id)
        return {"cancelled": had_turn, "closed": True}

    def set_mode(self, session, mode_id):
        result, err = session.conn.request(
            "session/set_mode", {"sessionId": session.acp_session_id, "modeId": mode_id})
        if err:
            return False, err.get("message")
        session.current_mode = mode_id
        return True, None

    def reap(self):
        """Janitor: close idle and already-dead sessions, and drop them from the
        table so a crashed agent cannot hold a slot forever."""
        now = time.time()
        with self._lock:
            items = list(self._sessions.items())
        for sid, session in items:
            if session.state == "closed":
                if (now - session.touched) > 300:
                    self._forget(sid)
                continue
            if session.state == "running":
                continue
            if (now - session.touched) > ACP_IDLE_TIMEOUT:
                session.conn.terminate("idle timeout")
                self._forget(sid)

    def shutdown(self):
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for session in items:
            try:
                session.conn.terminate("daemon shutting down")
            except Exception:
                pass


ACP = AcpSessionManager()


# =====================================================================
# Spaces -- a Space is one git worktree + one branch, living under
# ~/Side/spaces/<id>/tree. The UI never says "worktree".
# =====================================================================

GIT_TIMEOUT = 60
SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Deliberately strict. git's own rules allow more, but every extra character is
# another chance for a value to be read as an option or a path.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _git(args, cwd=None, timeout=GIT_TIMEOUT):
    """Run git with a list argv (never a shell string), reaping via run()."""
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def git_available():
    code, out, _ = _git(["--version"], timeout=10)
    return code == 0, (out.strip() or None)


def slugify_space(name):
    """(id, error). Same traversal posture as sanitize_sandbox: reject anything
    that could climb out BEFORE touching the filesystem, then confine."""
    if not isinstance(name, str):
        return None, "name is required"
    raw = name.strip()
    if not raw or "\x00" in raw:
        return None, "name is required"
    if "/" in raw or "\\" in raw or ".." in raw:
        return None, "invalid name"
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in raw).strip("-. ")
    safe = re.sub(r"-{2,}", "-", safe)[:64]
    if not safe or not SPACE_ID_RE.match(safe):
        return None, "invalid name"
    return safe, None


def space_dir(space_id):
    """(dir, error) confined under SPACES_ROOT."""
    if not SPACE_ID_RE.match(space_id or ""):
        return None, "invalid space id"
    d = (SPACES_ROOT / space_id).resolve()
    try:
        d.relative_to(SPACES_ROOT)
    except ValueError:
        return None, "space id escapes the spaces root"
    return d, None


def validate_repo(repo):
    """(repo_path, git_common_dir, error).

    The repo is the user's own project, so it necessarily lives outside ~/Side.
    We still refuse anything outside $HOME (so `repo:"/"` or a system path is not
    reachable), refuse a path inside SPACES_ROOT (no spaces of spaces), and require
    it to actually be a git repository."""
    if not isinstance(repo, str) or not repo.strip():
        return None, None, "repo is required"
    if "\x00" in repo:
        return None, None, "invalid repo path"
    try:
        path = Path(os.path.expanduser(repo.strip())).resolve()
    except (OSError, RuntimeError) as exc:
        return None, None, "cannot resolve repo: %s" % exc
    if not path.is_dir():
        return None, None, "repo is not a directory"
    roots_env = os.environ.get("SIDE_SPACE_REPO_ROOTS", "").strip()
    allowed = [Path(p).expanduser().resolve() for p in roots_env.split(":") if p] or [HOME_ROOT]
    ok = False
    for root in allowed:
        try:
            path.relative_to(root)
            ok = True
            break
        except ValueError:
            continue
    if not ok:
        return None, None, "repo must live under %s" % ", ".join(str(a) for a in allowed)
    try:
        path.relative_to(SPACES_ROOT)
        return None, None, "repo cannot be inside the spaces root"
    except ValueError:
        pass
    code, out, err = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(path))
    if code != 0:
        return None, None, "not a git repository: %s" % (err.strip() or out.strip() or "unknown")
    return path, Path(out.strip()), None


def validate_branch(branch):
    if branch is None or branch == "":
        return None, None
    if not isinstance(branch, str):
        return None, "branch must be a string"
    b = branch.strip()
    if not BRANCH_RE.match(b) or ".." in b or b.endswith("/") or b.endswith(".lock"):
        return None, "invalid branch name"
    code, _, err = _git(["check-ref-format", "--branch", b], timeout=10)
    if code != 0:
        return None, "git rejected the branch name: %s" % (err.strip() or b)
    return b, None


class SpaceStore:
    """spaces.json under SPACES_ROOT, written atomically."""

    def __init__(self):
        self._lock = threading.RLock()

    def _load_locked(self):
        try:
            with open(SPACES_STATE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("spaces"), dict):
            return {}
        return data["spaces"]

    def _save_locked(self, spaces):
        SPACES_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = SPACES_STATE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "spaces": spaces}, f, indent=2)
        os.replace(tmp, SPACES_STATE)

    def all(self):
        with self._lock:
            return self._load_locked()

    def get(self, space_id):
        with self._lock:
            return self._load_locked().get(space_id)

    def put(self, record):
        with self._lock:
            spaces = self._load_locked()
            spaces[record["id"]] = record
            self._save_locked(spaces)
            return record

    def delete(self, space_id):
        with self._lock:
            spaces = self._load_locked()
            existed = spaces.pop(space_id, None) is not None
            self._save_locked(spaces)
            return existed


SPACES = SpaceStore()


def space_status(record):
    """Real dirty flag from `git status --porcelain`, plus liveness of the tree.

    dirty covers modified, staged AND untracked files -- anything that would be
    lost with the directory. Committed work is never at risk here because removing
    a Space never deletes its branch."""
    tree = record.get("path")
    out = dict(record)
    out["exists"] = bool(tree) and os.path.isdir(tree)
    out["dirty"] = False
    out["dirtyCount"] = 0
    out["dirtyFiles"] = []
    out["gitError"] = None
    if not out["exists"]:
        out["gitError"] = "working tree is missing"
        return out
    code, stdout, stderr = _git(["status", "--porcelain", "--untracked-files=normal"], cwd=tree)
    if code != 0:
        # Cannot prove it is clean -> treat as dirty. Failing safe here is the
        # difference between refusing a removal and destroying someone's work.
        out["dirty"] = True
        out["gitError"] = (stderr.strip() or "git status failed")
        return out
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    out["dirty"] = bool(lines)
    out["dirtyCount"] = len(lines)
    out["dirtyFiles"] = [ln[3:] if len(ln) > 3 else ln for ln in lines[:20]]
    code, head, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=tree)
    if code == 0 and head.strip():
        out["branch"] = head.strip()
    return out


def space_public(record):
    """The shape the frozen contract promises for /api/space/list."""
    st = space_status(record)
    return {
        "id": st.get("id"),
        "name": st.get("name"),
        "repo": st.get("repo"),
        "branch": st.get("branch"),
        "path": st.get("path"),
        "dirty": bool(st.get("dirty")),
        "nodes": st.get("nodes") or [],
        "exists": st.get("exists"),
        "dirtyCount": st.get("dirtyCount"),
        "dirtyFiles": st.get("dirtyFiles"),
        "gitError": st.get("gitError"),
        "createdAt": st.get("createdAt"),
    }


def space_create(name, repo, branch, base=None):
    """(record, error). Creates the branch and the worktree, or fails cleanly."""
    ok, _ = git_available()
    if not ok:
        return None, "git is not installed"
    space_id, err = slugify_space(name)
    if err:
        return None, err
    if SPACES.get(space_id):
        return None, "a space named '%s' already exists" % space_id
    repo_path, git_common, err = validate_repo(repo)
    if err:
        return None, err
    branch, err = validate_branch(branch or ("side/" + space_id))
    if err:
        return None, err
    base, err2 = validate_branch(base) if base else (None, None)
    if err2:
        return None, "base: " + err2
    d, err = space_dir(space_id)
    if err:
        return None, err
    tree = d / "tree"
    if tree.exists():
        return None, "space directory already exists on disk"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, "could not create space directory: %s" % exc

    # Does the branch already exist? If so attach to it; otherwise create it.
    code, _, _ = _git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], cwd=str(repo_path))
    if code == 0:
        argv = ["worktree", "add", "--", str(tree), branch]
    elif base:
        argv = ["worktree", "add", "-b", branch, "--", str(tree), base]
    else:
        argv = ["worktree", "add", "-b", branch, "--", str(tree)]
    code, out, err_txt = _git(argv, cwd=str(repo_path))
    if code != 0:
        try:
            shutil.rmtree(str(d), ignore_errors=True)
        except OSError:
            pass
        return None, "git worktree add failed: %s" % (err_txt.strip() or out.strip() or "unknown")
    record = {
        "id": space_id,
        "name": name.strip(),
        "repo": str(repo_path),
        "gitCommonDir": str(git_common) if git_common else None,
        "branch": branch,
        "path": str(tree),
        "nodes": [],
        "createdAt": int(time.time() * 1000),
    }
    SPACES.put(record)
    return record, None


def space_remove(space_id, force):
    """(result, error).

    Refuses on a dirty worktree unless force. NEVER deletes the branch, so even a
    forced removal only discards uncommitted changes -- commits stay reachable in
    the repo. Directory deletion is confined to SPACES_ROOT."""
    record = SPACES.get(space_id)
    if record is None:
        return None, "unknown space"
    st = space_status(record)
    if st.get("dirty") and not force:
        return {
            "ok": True, "removed": False, "dirty": True,
            "dirtyCount": st.get("dirtyCount"),
            "dirtyFiles": st.get("dirtyFiles"),
            "reason": st.get("gitError") or (
                "%d uncommitted change(s) in this space. Commit or discard them, "
                "or remove with force." % (st.get("dirtyCount") or 0)),
        }, None
    # A Space with a live agent attached is not safe to delete underneath it.
    live = ACP.sessions_for_space(space_id)
    if live and not force:
        return {"ok": True, "removed": False, "dirty": bool(st.get("dirty")),
                "reason": "an agent session is still running in this space"}, None
    for session in live:
        session.conn.terminate("space removed")

    d, err = space_dir(space_id)
    if err:
        return None, err
    tree = record.get("path")
    if tree and os.path.isdir(tree):
        argv = ["worktree", "remove"]
        if force:
            argv.append("--force")
        argv += ["--", tree]
        code, out, err_txt = _git(argv, cwd=record.get("repo") or None)
        if code != 0 and not force:
            return None, "git worktree remove failed: %s" % (err_txt.strip() or out.strip())
    _git(["worktree", "prune"], cwd=record.get("repo") or None)
    # Belt and braces: only ever rmtree inside SPACES_ROOT.
    try:
        d.relative_to(SPACES_ROOT)
        if d.is_dir():
            shutil.rmtree(str(d), ignore_errors=True)
    except ValueError:
        return None, "refusing to delete outside the spaces root"
    SPACES.delete(space_id)
    return {"ok": True, "removed": True, "dirty": bool(st.get("dirty"))}, None


def resolve_session_cwd(space_id, cwd):
    """(Path, extra_read, extra_write, error) for a new ACP session.

    A spaceId resolves to that Space's worktree. A raw cwd must land under a Side
    root -- the daemon does not hand an agent an arbitrary directory."""
    if space_id:
        record = SPACES.get(space_id)
        if record is None:
            return None, [], [], "unknown space"
        tree = record.get("path")
        if not tree or not os.path.isdir(tree):
            return None, [], [], "this space's working tree is missing"
        # A worktree's .git is a FILE pointing into the source repo, so git needs
        # read+write there. Nothing else outside the space becomes reachable.
        extra_read = [record["repo"]] if record.get("repo") else []
        extra_write = [record["gitCommonDir"]] if record.get("gitCommonDir") else []
        return Path(tree).resolve(), extra_read, extra_write, None
    if not cwd:
        return None, [], [], "spaceId or cwd is required"
    if not isinstance(cwd, str) or "\x00" in cwd:
        return None, [], [], "invalid cwd"
    try:
        path = Path(os.path.expanduser(cwd)).resolve()
    except (OSError, RuntimeError) as exc:
        return None, [], [], "cannot resolve cwd: %s" % exc
    for root in SESSION_ROOTS:
        try:
            path.relative_to(root)
            break
        except ValueError:
            continue
    else:
        return None, [], [], "cwd must be inside %s" % str(WORKSPACE_ROOT)
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, [], [], "could not create cwd: %s" % exc
    return path, [], [], None


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

    def _host_ok(self):
        """SEC-04: accept only Host headers whose host part is 127.0.0.1/localhost
        (with our bound port or no port). Blocks DNS-rebinding. A missing Host
        (HTTP/1.0) is allowed -- the socket already binds 127.0.0.1 only and a
        rebinding attack always carries the attacker's Host."""
        host_hdr = self.headers.get("Host", "")
        if not host_hdr:
            return True
        host_part, sep, port_part = host_hdr.rpartition(":")
        if not sep:  # no colon -> whole value is the host, no port
            host_part, port_part = host_hdr, ""
        # A bracketed IPv6 literal (e.g. [::1]) is never in our allow-list; treating
        # the trailing ":x" as a port is harmless because the host still won't match.
        if host_part.strip().lower() not in ALLOWED_HOSTS:
            return False
        if port_part and port_part != str(self.port):
            return False
        return True

    def _reject_bad_host(self):
        """Send the SEC-04 rejection. Returns True when it fired (Host was bad)."""
        if self._host_ok():
            return False
        try:
            self._send_json(421, {"error": "bad host"})
        except Exception:
            pass
        return True

    def _mutation_guard_ok(self, headers):
        """CSRF + auth gate for EVERY mutating POST (/api/save, /api/agent/analyze,
        /api/agent/stop). Rejects with 403 unless BOTH hold:
          (a) Content-Type starts with 'application/json' -- blocks cross-origin
              'simple' POSTs (text/plain, form encodings) that skip the CORS
              preflight and could otherwise trigger writes/agent spawns; AND
          (b) the caller proves same-origin, by EITHER a matching Origin header
              (allow-list {http://127.0.0.1:PORT, http://localhost:PORT}) OR a
              matching X-Side-Token bearer (served same-origin via /api/health).
        Origin covers the real app before it has read the token; the token covers
        same-origin clients that omit Origin. Returns True if allowed, else emits
        the 403 and returns False. GET endpoints do not call this."""
        ctype = self.headers.get("Content-Type", "")
        if not ctype.strip().lower().startswith("application/json"):
            self._send_json(403, {"error": "content-type must be application/json"}, headers)
            return False
        origin_ok = cors_origin(self, self.port) is not None
        token = self.headers.get("X-Side-Token", "")
        token_ok = bool(token) and hmac.compare_digest(token, LAUNCH_TOKEN)
        if not (origin_ok or token_ok):
            self._send_json(403, {"error": "cross-origin request blocked"}, headers)
            return False
        return True

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
            if self._reject_bad_host():
                return
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
                elif route == "/api/acp/agents":
                    self._handle_acp_agents()
                elif route == "/api/acp/poll":
                    self._handle_acp_poll()
                elif route == "/api/space/list":
                    self._handle_space_list()
                return
            if route.startswith("/api/"):
                self._send_json(404, {"error": "not found"}, self._cors_headers())
                return
            self._handle_static()
        except Exception as exc:  # last-resort backstop -- never crash the handler
            self._safe_500(exc)

    def do_POST(self):
        try:
            if self._reject_bad_host():
                return
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
                elif route == "/api/acp/session":
                    self._handle_acp_session()
                elif route == "/api/acp/prompt":
                    self._handle_acp_prompt()
                elif route == "/api/acp/permission":
                    self._handle_acp_permission()
                elif route == "/api/acp/stop":
                    self._handle_acp_stop()
                elif route == "/api/space/create":
                    self._handle_space_create()
                elif route == "/api/space/remove":
                    self._handle_space_remove()
                return
            self._send_json(404, {"error": "not found"}, self._cors_headers())
        except Exception as exc:
            self._safe_500(exc)

    def do_OPTIONS(self):
        if self._reject_bad_host():
            return
        if self.path.startswith("/api/"):
            headers = self._cors_headers()
            headers["Access-Control-Allow-Headers"] = "content-type, x-side-token"
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
            # SEC-03: per-launch token. CORS keeps this readable same-origin only,
            # so the app can fetch it here and attach it as X-Side-Token on writes.
            "token": LAUNCH_TOKEN,
            "agent": {"available": bool(detect.get("available")), "mode": MODE_LABEL},
            # Additive. Tier 1 above is unchanged; these describe tier 2 and Spaces
            # so the UI can degrade honestly instead of guessing.
            "acp": {
                "enabled": acp_enabled(),
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "fs": acp_fs_mode(),
                "terminal": acp_terminal_enabled(),
                "sessions": ACP.live_count(),
                "maxSessions": MAX_ACP_SESSIONS,
            },
            "spaces": {"root": str(SPACES_ROOT), "git": git_available()[0]},
        }
        self._send_json(200, obj, self._cors_headers())

    def _handle_save(self):
        headers = self._cors_headers()
        if not self._mutation_guard_ok(headers):  # SEC-01/03: CSRF + auth gate
            return
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
        if not self._mutation_guard_ok(headers):  # SEC-01/03: CSRF + auth gate
            return
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
        # SEC-02(b): best-effort sandbox-exec read-confinement on top of the
        # read-only tool restriction (falls back to the plain argv if unavailable).
        argv = build_agent_argv(argv, str(sandbox))

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
        if not self._mutation_guard_ok(headers):  # SEC-01/03: CSRF + auth gate
            return
        data = self._read_json(headers, MAX_AGENT_BODY)
        if data is None:
            return
        job_id = data.get("job")
        if not isinstance(job_id, str) or not job_id:
            self._send_json(400, {"error": "job is required"}, headers)
            return
        JOBS.stop(job_id)
        self._send_json(200, {"ok": True}, headers)

    # ---- ACP handlers ----
    # Every POST below calls _mutation_guard_ok FIRST -- the same CSRF + Origin +
    # bearer + Host gate as /api/save. There is no second auth path in this file.
    def _acp_gate(self, headers, mutating):
        """Shared preamble: feature flag, then (for POSTs) the existing CSRF gate.
        Returns the parsed body for mutating routes, or True/None."""
        # CSRF gate BEFORE the feature flag, so an unauthorized caller cannot probe
        # which features are enabled.
        if mutating and not self._mutation_guard_ok(headers):
            return None
        if not acp_enabled():
            self._send_json(503, {"error": "acp disabled (SIDE_ACP=0)"}, headers)
            return None
        if not mutating:
            return True
        return self._read_json(headers, MAX_AGENT_BODY)

    def _handle_acp_agents(self):
        headers = self._cors_headers()
        if self._acp_gate(headers, False) is None:
            return
        agents = acp_detect_agents()
        self._send_json(200, {
            "ok": True,
            "agents": agents,
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "capabilities": {"fs": acp_fs_mode(), "terminal": acp_terminal_enabled()},
            "sessions": ACP.live_count(),
            "maxSessions": MAX_ACP_SESSIONS,
        }, headers)

    def _handle_acp_session(self):
        headers = self._cors_headers()
        data = self._acp_gate(headers, True)
        if data is None:
            return
        agent_id = data.get("agent")
        if not isinstance(agent_id, str) or not agent_id.strip():
            self._send_json(400, {"error": "agent is required"}, headers)
            return
        space_id = data.get("spaceId")
        if space_id is not None and not isinstance(space_id, str):
            self._send_json(400, {"error": "spaceId must be a string"}, headers)
            return
        cwd = data.get("cwd")
        root, extra_read, extra_write, err = resolve_session_cwd(space_id, cwd)
        if err:
            self._send_json(400, {"error": err}, headers)
            return
        session, failure = ACP.create(agent_id.strip(), root, space_id, extra_read, extra_write)
        if session is None:
            reason, message = failure
            status = {"busy": 429, "unavailable": 503, "spawn": 503, "handshake": 502}.get(reason, 500)
            self._send_json(status, {"error": message, "reason": reason}, headers)
            return
        body = session.snapshot()
        body["ok"] = True
        body["agentInfo"] = session.agent_info
        body["agentCapabilities"] = session.agent_caps
        self._send_json(200, body, headers)

    def _handle_acp_prompt(self):
        headers = self._cors_headers()
        data = self._acp_gate(headers, True)
        if data is None:
            return
        session = ACP.get(data.get("sessionId") or "")
        if session is None:
            self._send_json(404, {"error": "unknown sessionId"}, headers)
            return
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text is required"}, headers)
            return
        if len(text) > PROMPT_CAP:
            self._send_json(413, {"error": "prompt too large"}, headers)
            return
        # Optional, additive: switch mode in the same call the UI sends the prompt.
        mode = data.get("mode")
        if isinstance(mode, str) and mode and mode != session.current_mode:
            ok, err = ACP.set_mode(session, mode)
            if not ok:
                self._send_json(400, {"error": "set_mode failed: %s" % err}, headers)
                return
        ok, err = ACP.prompt(session, text)
        if not ok:
            self._send_json(409, {"error": err}, headers)
            return
        self._send_json(200, {"ok": True}, headers)

    def _handle_acp_poll(self):
        headers = self._cors_headers()
        if self._acp_gate(headers, False) is None:
            return
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        values = params.get("sessionId") or []
        session_id = values[0] if values else ""
        if not session_id:
            self._send_json(400, {"error": "sessionId is required"}, headers)
            return
        session = ACP.get(session_id)
        if session is None:
            self._send_json(404, {"error": "unknown sessionId"}, headers)
            return
        # Drain semantics: updates are delivered exactly once. The frozen contract
        # has no cursor, so the caller owns everything it is handed.
        updates = session.drain()
        self._send_json(200, {
            "ok": True,
            "updates": updates,
            "stopReason": session.stop_reason,
            "state": session.state,
            "pendingPermissions": [p.id for p in session.pending_permissions()],
            "closedReason": session.closed_reason,
        }, headers)

    def _handle_acp_permission(self):
        """The human end of the approval gate. Nothing else in this daemon can
        resolve a permission request as allowed."""
        headers = self._cors_headers()
        data = self._acp_gate(headers, True)
        if data is None:
            return
        session = ACP.get(data.get("sessionId") or "")
        if session is None:
            self._send_json(404, {"error": "unknown sessionId"}, headers)
            return
        request_id = data.get("requestId")
        outcome = data.get("outcome")
        if not isinstance(request_id, str) or not request_id:
            self._send_json(400, {"error": "requestId is required"}, headers)
            return
        if outcome not in ("allow", "deny"):
            self._send_json(400, {"error": "outcome must be 'allow' or 'deny'"}, headers)
            return
        option_id = data.get("optionId")
        if option_id is not None and not isinstance(option_id, str):
            self._send_json(400, {"error": "optionId must be a string"}, headers)
            return
        ok, err = session.answer_permission(request_id, outcome, option_id)
        if not ok:
            self._send_json(409, {"error": err, "ok": False}, headers)
            return
        self._send_json(200, {"ok": True}, headers)

    def _handle_acp_stop(self):
        headers = self._cors_headers()
        data = self._acp_gate(headers, True)
        if data is None:
            return
        session = ACP.get(data.get("sessionId") or "")
        if session is None:
            self._send_json(404, {"error": "unknown sessionId"}, headers)
            return
        result = ACP.stop(session, bool(data.get("cancelOnly")))
        body = {"ok": True}
        body.update(result)
        self._send_json(200, body, headers)

    # ---- Spaces handlers ----
    def _handle_space_list(self):
        headers = self._cors_headers()
        records = SPACES.all()
        spaces = [space_public(r) for r in records.values()]
        spaces.sort(key=lambda s: s.get("createdAt") or 0)
        ok, version = git_available()
        self._send_json(200, {"ok": True, "spaces": spaces,
                              "git": {"available": ok, "version": version},
                              "root": str(SPACES_ROOT)}, headers)

    def _handle_space_create(self):
        headers = self._cors_headers()
        if not self._mutation_guard_ok(headers):
            return
        data = self._read_json(headers, MAX_AGENT_BODY)
        if data is None:
            return
        record, err = space_create(data.get("name"), data.get("repo"),
                                   data.get("branch"), data.get("base"))
        if err:
            self._send_json(400, {"error": err}, headers)
            return
        self._send_json(200, {"ok": True, "space": space_public(record)}, headers)

    def _handle_space_remove(self):
        headers = self._cors_headers()
        if not self._mutation_guard_ok(headers):
            return
        data = self._read_json(headers, MAX_AGENT_BODY)
        if data is None:
            return
        space_id = data.get("id")
        if not isinstance(space_id, str) or not space_id:
            self._send_json(400, {"error": "id is required"}, headers)
            return
        result, err = space_remove(space_id, bool(data.get("force")))
        if err:
            self._send_json(400, {"error": err}, headers)
            return
        self._send_json(200, result, headers)

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
        # SEC-05: lock down what the served page may load/connect to.
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
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
    SPACES_ROOT.mkdir(parents=True, exist_ok=True)

    def janitor():
        """Reap idle/dead ACP sessions so a crashed agent never holds a slot."""
        while True:
            time.sleep(30)
            try:
                ACP.reap()
            except Exception:
                pass

    threading.Thread(target=janitor, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("side-serve %s on http://127.0.0.1:%d (dir=%s)" % (VERSION, args.port, app_dir))
    print("  acp=%s fs=%s terminal=%s spaces=%s" % (
        "on" if acp_enabled() else "off", acp_fs_mode(),
        "on" if acp_terminal_enabled() else "off", SPACES_ROOT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Never leave agent subprocesses behind when the daemon exits.
        ACP.shutdown()


if __name__ == "__main__":
    main()
