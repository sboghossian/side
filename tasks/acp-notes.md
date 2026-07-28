# W3-A — ACP client + Spaces (`bin/side-serve.py`)

**Date:** 2026-07-28 · **Base:** `c18de9f` · **File:** 904 -> ~3,000 lines, stdlib only, no pip
**Spec:** [agentclientprotocol.com](https://agentclientprotocol.com) protocol **v1**

Side is the **client**. The agent is a subprocess we spawn; we speak JSON-RPC 2.0 over its
stdin/stdout, one message per line. Two tiers now exist side by side:

| Tier | Routes | Flag | Status |
|---|---|---|---|
| 1 — `claude -p`, read-only | `/api/agent/*` | always on | untouched (one bugfix, below) |
| 2 — ACP | `/api/acp/*` | `SIDE_ACP` (default on) | new |
| Spaces | `/api/space/*` | always on | new |

`SIDE_ACP=0` disables **only** tier 2. Tier 1 keeps running exactly as before.

---

## The protocol subset actually implemented

### Agent methods we call
| Method | Status |
|---|---|
| `initialize` | Yes. `protocolVersion: 1`, client capabilities, `clientInfo`. A mismatched version closes the connection rather than guessing. |
| `session/new` | Yes. `cwd` + `mcpServers: []`. Captures `modes` and `sessionId`. |
| `session/prompt` | Yes, text content blocks. Runs on a worker so the HTTP call returns `{ok:true}` immediately, per the frozen contract. |
| `session/cancel` | Yes (notification), on `/api/acp/stop`. |
| `session/set_mode` | Yes, via the optional `mode` field on `/api/acp/prompt`. |

### Client methods we serve
| Method | Status |
|---|---|
| `session/update` | Yes — mapped onto the frozen poll shapes. |
| `session/request_permission` | Yes. **The approval gate.** Blocks the agent until a human answers. |
| `fs/read_text_file` | Yes, jailed. Honours 1-based `line` + `limit`. |
| `fs/write_text_file` | Yes, jailed. Creates parents. |
| `terminal/create\|output\|wait_for_exit\|kill\|release` | Implemented, **advertised only when `SIDE_ACP_TERMINAL=1`** (default off). |
| `elicitation/create` | **Not implemented.** We do not advertise the capability, so per spec the agent must not call it; if it does we answer `-32601`. |

### `session/update` mapping
Wire discriminator -> the shape the browser polls (frozen contract):

```
agent_message_chunk / user_message_chunk / agent_thought_chunk
                             -> {type:"message_chunk", text, role, messageId?}
plan                         -> {type:"plan", entries:[{content,status,priority}]}
tool_call                    -> {type:"tool_call", id, title, status, kind, update:false}
tool_call_update             -> {type:"tool_call", ..., update:true}   // merge by id
current_mode_update          -> {type:"mode", mode}
available_commands_update    -> {type:"commands", commands, total, truncated}
usage_update                 -> {type:"usage", used, size, cost}
(anything else)              -> {type:"raw", sessionUpdate, raw}       // never dropped
```

Side-generated updates on the same stream: `permission_request`, `permission_resolved`,
`terminal`, `fs`, `stop`, `cancelled`, `error`, `closed`. Every update carries `seq` and `at`.

**Poll is drain-once.** The frozen route has no cursor, so `/api/acp/poll` returns pending
updates and clears them. The caller owns what it is handed; a dropped response loses those
updates. Fine over loopback, worth knowing.

---

## Deliberately left out

- **`session/load` / `session/resume` / `session/list` / `session/delete` / `session/close`** —
  no persistence layer to restore into yet. Restoring a session we cannot actually resume would
  be a lie, the same reason Wave 2 refused to persist the gate queue.
- **`authenticate` / `logout`** — we surface an agent's `authMethods` in the `session/new`
  failure message but do not drive an auth flow. Agents that need an explicit login must be
  logged in through their own CLI first.
- **`elicitation/create`** — needs a UI that does not exist. Refused honestly.
- **MCP servers** — `session/new` always sends `mcpServers: []`. MCP is the v1.0 roadmap item.
- **Image / audio / embedded-context prompt blocks** — text only for now.
- **Streamable-HTTP transport** — stdio only, which is the recommended transport.
- **`_meta` extensions** — passed through inside `raw`, not interpreted.
- **Non-text `ContentBlock`s in message chunks** — surfaced with `contentType` + `raw` rather
  than rendered as an empty bubble.

---

## The approval gate

`session/request_permission` maps onto Side's gate. This is the whole reason ACP fits.

- The request blocks on an `Event` until `POST /api/acp/permission` answers it.
- **There is no auto-approve path.** No allowlist, no "remember", no config, no default-yes.
- `allow`/`deny` map onto the agent's own `optionId`s by `kind`
  (`allow_once` -> `allow_always`, `reject_once` -> `reject_always`).
- If the agent offered **no option of the requested polarity**, we do not substitute the
  opposite one — the request is answered `cancelled` and the user is told why.
- An `optionId` the agent never offered is rejected.
- Timeout (30 min), session cancel, and connection death all resolve as `cancelled`.
  **Timing out is not consent.**

---

## Security decisions

1. **Every new mutating route joins the existing gate.** `_mutation_guard_ok` (Content-Type +
   Origin/bearer) then `_host_ok`. No parallel auth path was created.
2. **GETs stay unauthenticated**, matching the existing `/api/agent/*` posture. Cross-origin
   reads are blocked by CORS since we only echo `Access-Control-Allow-Origin` for allowed origins.
3. **`sandbox-exec` extended to tier 2**, and made two-sided: tier 1 denies reads under `$HOME`;
   the ACP profile denies **reads and writes** under `$HOME`, re-permitting the session root,
   the Space's backing `.git`, and agent essentials. Best-effort like tier 1 — and the resulting
   `sandboxed` boolean is reported on every session so an unsandboxed agent is never displayed
   as a sandboxed one.
4. **`terminal/*` is opt-in.** Granting arbitrary command execution to any ACP agent by default
   would have weakened the daemon's headline property. `SIDE_ACP_TERMINAL=1` to enable.
5. **fs is jailed twice** — under the session root *and* under a Side root, after `realpath`, so
   symlinks and `..` cannot escape. Verified against `/etc/passwd`, `~/.ssh`, `..` traversal and
   a write to `~/PWNED.txt`; all refused.
6. **Session `cwd` cannot be arbitrary.** A `spaceId` resolves to that Space's worktree; a raw
   `cwd` must be under `~/Side`. Handing an agent any directory it names was not on the table.
7. **Repos must live under `$HOME`** (override with `SIDE_SPACE_REPO_ROOTS`) and cannot be inside
   the spaces root. Branch names pass a strict regex *and* `git check-ref-format`. All git calls
   use list argv with `--`, never a shell string.
8. **Secret scrub reused, not reimplemented** — ACP children go through the same
   `build_child_env` as tier 1, so the two tiers cannot drift.

---

## Spaces

A Space is one git worktree + one branch under `~/Side/spaces/<id>/tree`, recorded in
`spaces.json` (atomic write). The word "worktree" never reaches the UI.

- `dirty` is real: `git status --porcelain` including untracked files. If git *fails*, the space
  is reported dirty — failing safe is the difference between refusing a removal and destroying work.
- `remove` without `force` refuses a dirty space and returns `{ok:true, removed:false, reason}`.
- `remove` also refuses while an agent session is live in that space.
- **`remove` never deletes the branch.** Even a forced removal only discards uncommitted changes;
  every commit stays reachable in the source repo. Verified.

---

## Concurrency and reaping

- One reader thread per connection (owns stdout), one stderr drain thread (an undrained stderr
  pipe deadlocks the child), and **one worker per inbound request** — `request_permission` and
  `wait_for_exit` block for minutes and must not stall the reader.
- Outbound requests wait on an `Event` with a timeout **and** are woken by connection close, so a
  dead child fails callers in milliseconds instead of hanging. Verified with an agent that exits
  before saying anything: `initialize failed` in 0.1s.
- Reader EOF -> fail all pending, cancel all permissions, release terminals, terminate, `wait()`.
  **No zombies** — verified across repeated 4-way concurrent runs.
- `MAX_ACP_SESSIONS = 4`; a janitor reaps idle sessions after 30 min; `ACP.shutdown()` on exit.
- Non-JSON lines on stdout are logged and skipped, not fatal — real agents print banners
  (`openclaw` prints a doctor warning before any protocol traffic).

---

## Environment flags

| Var | Default | Effect |
|---|---|---|
| `SIDE_ACP` | on | Master switch for `/api/acp/*`. Off = 503, tier 1 unaffected. |
| `SIDE_ACP_FS` | `rw` | `rw` \| `read` \| `off`. Which fs capabilities we advertise. |
| `SIDE_ACP_TERMINAL` | **off** | Advertise and serve `terminal/*`. |
| `SIDE_ACP_EXTRA_AGENTS` | — | JSON array of custom agent specs; the catalog will go stale. |
| `SIDE_SPACE_REPO_ROOTS` | `$HOME` | Colon-separated roots a Space repo may live under. |
| `SIDE_SANDBOX` | on (darwin) | Shared with tier 1. |

---

## Pre-existing bug found and fixed

**Tier 1 was completely dead on this machine and nobody noticed.** Every `/api/agent/analyze` run
returned `Not logged in - Please run /login`.

Cause: `build_agent_argv`'s sandbox profile denies reads under `$HOME` and never re-permitted
`~/Library/Keychains`. Claude Code stores subscription credentials in the login keychain, not in
`~/.claude/.credentials.json` (which does not exist here). Shipped in `2ded46b`.

Verified against the unmodified file at `c18de9f`: sandboxed -> `Not logged in`;
`SIDE_SANDBOX=0` -> works. Fixed by adding `~/Library/Keychains` to the readable list in **both**
profiles. Keychain *items* stay protected by securityd ACLs. Re-verified after the fix: tier 1
returns `OK` sandboxed, and an adversarial read of `~/.zshrc` still fails with `EPERM`.
