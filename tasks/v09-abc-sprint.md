# Side v0.9 — A+B+C Sprint Plan & Frozen Contracts
**Started:** 2026-07-27 · **Base:** `477aef5` · **Master:** `~/Documents/Code/conductor/agent-build.html` (8.2MB)
**Source analysis:** [competitive-teardown-2026-07.md](./competitive-teardown-2026-07.md)

## Decisions locked (2026-07-27)

| # | Decision | Choice |
|---|---|---|
| 1 | Node Designer rebuild | **Component canvas + artifacts auto-mount.** Freeform dot-grid, everything draggable, run artifacts auto-mount as components. Code editor becomes ONE optional component, not the frame. Kills the `.nde-` 3-zone IDE. |
| 2 | A+B coexistence | **One adaptive UI, progressive disclosure.** Artifacts always default. `Show the code` expander on file-change artifacts reveals the real diff. No mode, no fork, no onboarding question. |
| 3 | ACP | **New adapter alongside `claude -p`, feature-flagged.** Proven fallback stays until ACP verified E2E. |
| 4 | Sprint shape | **3 waves, serial integration between.** Agents NEVER touch the master. |

## Strategy: A + B + C

- **B (trust layer)** is the wedge — artifact-first review for people who can't read a diff.
- **A (dev command center)** rides free on the same substrate — the diff is a *second rendering* of the same run, behind an expander.
- **C (ACP)** is the substrate that stops Side being a Claude Code wrapper.

---

## HARD RULES FOR ALL BUILD AGENTS

**Violating any of these breaks the master. They are non-negotiable.**

1. **NEVER edit `~/Documents/Code/conductor/agent-build.html`.** Not once. Not to "check". Write standalone module files only. Stephane's orchestrator integrates them serially.
2. **Output = two files per module**, written to the wave dir given in your prompt:
   - `<id>.css` — raw CSS. **NO `<style>` tags.** (Agents keep adding these. Do not.)
   - `<id>.js` — raw JS. **NO `<script>` tags.**
3. **ES5 ONLY.** No arrow functions, no `const`/`let`, no template literals, no classes, no spread, no `for...of`, no default params, no destructuring. `var` + `function` + string concat. The master is an ES5 IIFE.
4. **ASCII-ONLY source.** No em dash, no curly quotes, no accented characters, no emoji in source. Use `—` escapes inside JS strings if you need a dash glyph at runtime.
5. **Module guard, first line of every JS file:**
   ```js
   if(window.__sideXXX)return;window.__sideXXX=1;
   ```
   (wrapped in your IIFE). This is what saved v0.7.6 from a double-integration disaster.
6. **Wrap everything in an IIFE:** `(function(){ ... })();`
7. **Export to `window.`** Anything the host must call. Host-internal functions (`cmdkGroups`, `renderCmdk`, `go`, `navTo`, `openGateFlow`, `wfSetNodeState`) live INSIDE the host IIFE — wrapping `window.X` silently no-ops. Export yours to `window`, the orchestrator splices host call sites.
8. **Zero dependencies.** No npm, no CDN, no fetch to third parties. Inline everything.
9. **Parse-check in REAL script mode, not `node --check`.** `node --check` wraps the file in a
   CommonJS function, so a bare top-level `return` passes there and then throws
   `SyntaxError: Illegal return statement` in an actual `<script>` tag, taking the whole page down.
   Put the guard **inside** the IIFE, and verify with:
   ```bash
   node -e "const fs=require('fs'),vm=require('vm');new vm.Script(fs.readFileSync(process.argv[1],'utf8'))" yourfile.js
   ```
   (Found by W2-C, 2026-07-27. The integrator now enforces this; it previously did not.)
10. **No prose in the output files.** No "Here is the module:" preamble, no trailing explanation. The file is parsed programmatically; trailing prose has leaked into the master before.
11. **Do not claim anything is "verified" unless you ran it.** A fabricated verification badge was already shipped and killed once (`2ded46b`). Never again.
12. **Use your assigned CSS prefix.** Existing prefixes are taken: `.nde-` `.ndz-` `.ctx-` `.bpk-` `.lint-` `.rts-` `.rlg-` `.fkc-` `.hks-` `.sfs-` `.pgm-` `.apm-` `.ob2-` `.lps-` `.edu-` `.brn-` `.abmap`.
13. **Style vars available** (use them, do not hardcode): `--line-2`, `--shadow-lg`, and the existing warm-brand palette. Read a sample of the master's CSS to match the house style before writing.
14. **Return conclusions, not file dumps.** Your final message: what you built, the exported API, line counts, what you could NOT do, and any contract deviation. Do not paste the module contents back.
15. **LOAD ORDER (learned in Wave 1).** Your module parses AFTER `SCRIPT-MARK`, so every host module
    that arms itself *before* the mark has already run. A pre-mark module can never `if(typeof
    window.yourThing==='function')` its way to you — it runs first, fails the check, and often
    latches a legacy fallback permanently. If a pre-mark module must reach you, expose a **plain
    array or object it can seed before you exist**, and adopt it with `window.X=window.X||[]`.
    This is exactly how Fork/Compare silently died in Wave 1.
16. **Do not monkey-patch host-internal functions.** `openGateFlow`, `cmdkGroups`, `renderCmdk`, `go`,
    `navTo`, `wfSetNodeState` live inside the host IIFE. Some are also exported to `window`, but
    host-internal call sites bypass the window wrap. Expose a registry or a hook, and flag the
    splice for the orchestrator.

## Host surfaces Wave 2 builds on (verified @ 2026-07-27)

| Thing | Where | Notes |
|---|---|---|
| `openGateFlow(opts)` | defined 3960, exported 3984 | already wrapped by the ctx module at 19429; call site 3719 uses `(window.openGateFlow||openGateFlow)` |
| Inbox | `sideInboxReal()` 4808, `drawerInbox()` 5296 | opened via `openUtil('inbox')`; chrome button `#chr-gate` |
| `sideLintGate` | 18208 | pre-run gate, returns true to block |
| `SideEngine.COST` | 14087 | `{model:{in,out}}` per-Mtok: opus 15/75, fable 20/100, sonnet 3/15, haiku 1/5 |
| `LASTRUN[slug]` | persisted via `side_state_v1` | carries `totalCost`, `totalTok`, `final` |
| Node `status` values | in use | `idle` `queued` `todo` `run` `review` `done` |
| Canvas mode | `side_canvas_mode` | currently `flow` \| `map`, toggled by `#tb-flowmap` |

---

## FROZEN CONTRACT v1 — Wave 2

### `window.buildBoardView(host, opts)` — W2-A, prefix `.bd-`
Third projection over the SAME graph as Flow and Map. Not a new data model.
```js
buildBoardView(host, {graph, slug, onOpen(nodeId), onRun(nodeId)}) // -> {refresh(), destroy()}
```
Columns map from existing `status`: **Running** (`run`) · **Needs you** (`review`, or a pending gate,
or a `SideVerify` verdict of `fail`/`unsure`) · **Done** (`done`) · **Not started** (`idle`/`queued`/`todo`,
collapsed by default). Cards show verdict chip via `SideVerify.lastFor` and artifact count via
`SideArtifacts.forNode`. Guard both. `side_canvas_mode` gains a third value `board`.

### `window.SideGate2` — W2-B, prefix `.gt2-`
```js
SideGate2.open(payload)        // -> Promise<{decision:"approve"|"deny", comments:[], at}>
SideGate2.pending()            // -> [payload]
SideGate2.renderInto(hostEl, payload, opts)
SideGate2.notify(payload)      // Notification API, degrades silently
SideGate2.onDecision(fn)       // -> unsubscribe
```
`payload = {id, nodeId, runId, title, summary, artifacts:[], verdict}`.
The gate renders **artifacts and the verdict**, never a raw tool log. Comments route through
`SideArtifacts.comment`. Deny must still abort downstream, exactly as today. Mobile-first layout is
the same component at a narrow breakpoint, not a second implementation.

### `window.SideBudget` — W2-C, prefix `.bud-`
```js
SideBudget.estimate(graph)              // -> {low, high, tokens, perNode:[]}
SideBudget.cap()  / SideBudget.setCap(n)
SideBudget.spent(runId) / SideBudget.ledger()
SideBudget.wouldExceed(runId, nextCost) // -> bool
SideBudget.record(runId, nodeId, model, inTok, outTok) // -> entry
```
Reads `window.SideEngine.COST`. Persist under `side_ledger` / `side_budget_cap`.
Estimates are **ranges, never a single confident number**, and must be labelled as estimates.
Exceeding the cap stops the run and surfaces why. No silent truncation.

## Wave 1 — Foundations (3 agents, parallel)

---

## FROZEN CONTRACT v1 — `window.SideArtifacts`

**Owner: W1-A. Consumers: W1-B, W1-C, and Wave 2. This shape does not change mid-wave.**

Artifact object:
```js
{
  id:        "art_<nodeId>_<seq>",
  runId:     "run_<ts>",
  nodeId:    "n7",
  type:      "plan"|"checklist"|"walkthrough"|"screenshot"|"recording"|"file-change"|"summary",
  title:     "Research plan",
  createdAt: 1753600000000,
  data:      {},        // type-specific, below
  comments:  []         // [{id, at, author:"user"|"agent", text, anchor}]
}
```

`data` by type:
```js
plan:        { steps:[ {id, text, status:"todo"|"doing"|"done"|"skip"} ] }
checklist:   { items:[ {id, text, checked:true|false, note} ] }
walkthrough: { sections:[ {heading, body} ] }
screenshot:  { src, caption, w, h }                 // src = data: URI or http(s)
recording:   { src, poster, durationMs, caption }
"file-change": { path, plain, before, after, lang }
summary:     { text }
```

**RESOLVED 2026-07-27 (W1-A, confirmed compatible with W1-B) — `file-change.plain`:**
`plain` is an object `{before, after}` of plain-language prose strings. A bare string is accepted
as a single-paragraph fallback. Anything that *creates* a file-change artifact (run engine, Wave 2)
must emit the object form. Both shapes are already handled by `SideArtifacts.render` and by
`SideVerify`'s artifact summarizer, so neither needs changing.

**Other resolved conventions (additive, do not break the frozen shape):**
- Demo artifacts are signalled by `runId` starting with `run_demo_` (drives the visible DEMO badge).
- `render()` **appends** to `hostEl` without clearing it, and returns the element either way.
  Pass no host to place the element yourself.
- `comment()` takes an optional 4th `author` arg, default `"user"`.
- Comment "threads" = all comments sharing an `anchor` string. Anchors exist on plan steps,
  checklist items, walkthrough sections and diff lines. Screenshot / recording / summary get the
  general unanchored thread only.
- Verifier artifact summaries state honestly that images and recordings were **not inspected** —
  only captions are available to the judge. Do not let a later change quietly imply otherwise.

API:
```js
window.SideArtifacts.add(nodeId, runId, type, title, data)   // -> artifact
window.SideArtifacts.forNode(nodeId)                          // -> [artifact]
window.SideArtifacts.forRun(runId)                            // -> [artifact]
window.SideArtifacts.get(artifactId)                          // -> artifact|null
window.SideArtifacts.render(artifact, hostEl, opts)           // -> el
window.SideArtifacts.comment(artifactId, text, anchor)        // -> comment
window.SideArtifacts.onChange(fn)                             // -> unsubscribe fn
window.SideArtifacts.clear(nodeId)
window.SideArtifacts.demoSeed(nodeId)                         // -> [artifact]  (demo-mode fixtures)
```

`render` opts: `{ compact:false, showCode:false, readOnly:false }`
- `compact:true` — card summary only, for Board / gate lists.
- `showCode` — **file-change only.** `false` = plain-language before/after (decision #2 default).
  `true` = real diff. The expander that flips it is INSIDE the file-change renderer.
- Persist to `localStorage` key `side_artifacts`. Honour `?reset=1` (clears all `side_*`).

---

## FROZEN CONTRACT v1 — `window.SideVerify`

**Owner: W1-B.**

Node fields added (on every node object and every `ABTPL` template node):
```js
node.done_when     // string, plain language. "" = unverified, renders as such. NEVER fake a pass.
node.output_schema // object|null, JSON Schema for structured output
```

API:
```js
window.SideVerify.check(nodeId, runId)   // -> Promise<verdict>
window.SideVerify.lastFor(nodeId)        // -> verdict|null
window.SideVerify.badge(verdict, hostEl) // -> el
window.SideVerify.validate(obj, schema)  // -> {ok:bool, errors:[str]}   ES5 subset validator
```

Verdict object:
```js
{ verdict:"pass"|"fail"|"unsure"|"none", score:"3/3", reasons:[str], at:ts, model:"haiku" }
```
- `"none"` when `done_when` is empty. Renders as "No success criterion set" — **not** as a pass.
- Real mode: verifier call via existing `SideEngine.call` in a cheap-model role.
- Demo mode: deterministic fixture. Must be visibly labelled demo.

---

## Wave 1 — SHIPPED to master 2026-07-27 (not committed, not pushed)

Master `8.15 MB -> 8.31 MB` (+158.5 KB), 24,622 lines, 41 script blocks, 0 `node --check` failures,
0 non-ASCII, **0 new console errors** (verified against the pre-wave backup: the two
`api/agent/detect` CORS errors are pre-existing under `file://`).

Host splices applied:
- `openNodeDesigner` guard + call site now prefer `buildNodeDesigner2`, falling back to v1.
- `fkcEnsureWrap` registers on the v2 hook registry.

**Two real bugs found and fixed during integration:**
1. **Fork/Compare was silently dead.** `fkcArm()` parses at line 20298; the v2 module at 21823.
   `sideNd2AddHook` did not exist yet, so the legacy `window.buildNodeDesigner` wrap succeeded and
   `fkcArm` stopped retrying — but the opener no longer calls that function. Fixed by seeding
   `window.__sideNd2Hooks` directly (v2's init is `=window.__sideNd2Hooks||[]`, so it adopts the array).
   **Generalizes: any module that arms itself before SCRIPT-MARK cannot wait on a post-SCRIPT-MARK global.**
2. **The `.nde-` rebuild had dropped `.ndz-chat`/`.ndz-chat-head`**, silently killing
   `SideCtx.onDesignerOpen`'s context bar and `__rlgInitPreview`. W1-C restored both, scoped under
   `.nd2-root` so the module survives deletion of the old `.nde-` style block.

Verified in-browser: v2 root renders, `.nde-root` gone, 8 cards (Conversation pinned + all 7 artifact
types auto-mounted), legacy hooks `.ndz-msgs`/`.ndz-ta` live, fork-compare hook registered,
progressive disclosure confirmed (plain-language default, `Show the code` reveals a dual-gutter
LCS diff, 5 added / 3 removed).

**Still open from Wave 1:**
- Old `.nde-` block (lines ~15949-16342) + its CSS still present as fallback. Delete after a real
  end-to-end run through the opener, not before.
- `ndSeedPage` (line ~4974) still emits terminal + settings components. Harmless; migration handles it.
- Drag disabled below 760px (touch-drag fights page scroll). Mobile is add/collapse/remove only.
- Terminal / files / preview / browser components are still host-fed, not live. W3-A (ACP) makes them real.
- 3 of 9 artifact cards scroll internally at default height. Resize persists.

## Wave 1 — Foundations (3 agents, parallel)

| id | Module | Prefix / export | Model |
|---|---|---|---|
| **W1-A** | Artifact system: model, store, 7 renderers, comment threads, progressive-disclosure diff | `.art-` / `window.SideArtifacts` | sonnet |
| **W1-B** | done_when + output_schema fields, verifier, verdict badge, ES5 schema validator | `.dwn-` / `window.SideVerify` | sonnet |
| **W1-C** | Node Designer v2: component canvas, artifacts auto-mount, code editor demoted to a component | `.nd2-` / `window.buildNodeDesigner2` | opus |

Integration gate between waves: `node --check` per block, 0 console errors, no non-ASCII, `/browse` QA
from `/private/tmp` with `?v=$(date +%s)` cache-buster.

## Wave 2 — SHIPPED to master 2026-07-28 (not committed, not pushed)

Master `8.31 MB -> 8.42 MB` (+108.6 KB), 27,409 lines, 44 script blocks, **0 script-mode parse
failures**, 0 non-ASCII, 0 new console errors. 12 host splices applied, all 15 assertions pass.

Modules: `w2a-board` (470 JS / 94 CSS) · `w2b-gate` (979 / 418) · `w2c-budget` (668 / 66).

**Scope change:** original W2-B (gate) and W2-C (mobile approval) merged into one agent. Same surface;
two agents would have produced two incompatible implementations of the sprint's most important screen.
Mobile is a breakpoint on the same component. Budget moved up into the third slot.

**Integrator hardened — `node --check` was not enough.** It wraps files in a CommonJS function, so a
bare top-level `return` passes there and throws `Illegal return statement` in a real `<script>` tag,
taking the whole page down. The integrator now parses via `vm.Script` (real script semantics).
Proven both ways: the bad guard passes `node --check` and fails the new gate. All Wave 1 modules
re-validated clean, so nothing broken had shipped. See hard rule 9.

**Verified in-browser (not asserted):**
- Board columns with precedence: a node with `status:'done'` whose verdict is `fail` surfaces under
  **Needs you**, and Done reads 0. All three reasons fire (review status / failed verdict / pending
  gate). Unverified nodes show "Not checked yet", never an implied pass.
- Gate v2 on an `unsure` verdict: **Deny & stop is the filled primary, Approve is demoted to
  "Approve anyway"**, band reads "Treat it as unverified", reasons admit the image was not inspected,
  and a `DEMO VERDICT - NOT A REAL CHECK` chip is present. Refusing is the easy path by design.
- 7 artifacts render as compact rows under "What the agent handed back", not a tool log.
- Budget estimate returns a **range** ($2.20-$5.14 / 128k tok) with non-dispatched node types marked
  `chargeable:false`. Default cap $5.00.

**Bugs found by the agents during build (all fixed):**
1. **W2-B, most dangerous of the sprint:** `wf:clear` fires at the *start* of every run; the first
   version resolved any open gate as `deny` on that event, so an unrelated run starting would have
   **fabricated a human decision** on a pending inbox gate. Now only `fromBlock` gates follow the wf
   lifecycle, and non-human resolutions carry `aborted:true, delivered:false`.
2. **W2-A:** at 60+ nodes, `.bd-card{overflow:hidden}` in a flex column made auto-min-height resolve
   to 0, silently crushing every card to ~37px instead of scrolling. Only a real browser catches this.
3. **W2-C:** the guard-outside-the-IIFE bug that hardened the integrator (above).
4. **W2-B:** duplicate `#gt2-scrim` nodes during fade-out made every `querySelector` ambiguous.

**Still open from Wave 2:**
- The end-of-run ship gate (line ~3719) is **unverified against a live `SideEngine.runGraph`** — needs
  real mode with an API key. The mechanism was proven in isolation; the full path was not.
- Budget cap stops *new* dispatch but cannot abort an XHR already in flight, so the ledger can
  overshoot by one call. Fixing it means editing `SideEngine.call`'s XHR handling.
- Gate queue is deliberately **not persisted** — a restored gate could not resume anything, so
  persisting it would be a lie. Comments do persist via `SideArtifacts`.
- `#chr-gate` has a click handler at ~5680 but **no matching element exists** in the markup. Dead code,
  pre-existing, not from this sprint.
- Board keeps column order on mobile (amber styling instead of promoting "Needs you" to top).
- Taste call made, reversible: added a dedicated `#tb-board` toolbar button rather than cycling
  `#tb-flowmap` through three states.

## Wave 2 — Surfaces (after W1 integrated)
- **W2-A** Board projection (`.bd-`) — third lens over the same graph: Running / Needs you / Done.
- **W2-B** Gate v2 (`.gt2-`) — artifacts + verdict render in the approval gate; comment-to-agent feedback.
- **W2-C** Mobile approval + push — the gate as a phone-first inbox.
- **W2-D** Run budget caps — pre-flight estimate, hard cap, ledger, stop-on-exceed.

## FROZEN CONTRACT v1 — Wave 3 (daemon HTTP + UI modules)

**Daemon** `bin/side-serve.py` (904 lines today; routes are allowlisted in `API_ROUTES`, all mutating
routes already behind CSRF + Origin + bearer + Host checks — new routes MUST join that same gate).
W3-A owns this file exclusively. Every route below is `application/json`.

```
GET  /api/acp/agents                 -> {agents:[{id,name,cmd,available,version}]}
POST /api/acp/session {agent,cwd,spaceId}   -> {sessionId, modes:[], commands:[]}
POST /api/acp/prompt  {sessionId,text}      -> {ok:true}
GET  /api/acp/poll?sessionId=        -> {updates:[...], stopReason:null|string}
POST /api/acp/permission {sessionId,requestId,outcome:"allow"|"deny"} -> {ok:true}
POST /api/acp/stop {sessionId}       -> {ok:true}

GET  /api/space/list                 -> {spaces:[{id,name,repo,branch,path,dirty,nodes:[]}]}
POST /api/space/create {name,repo,branch}   -> {space}
POST /api/space/remove {id,force}    -> {ok:true, removed:bool}
```

`poll` update shapes mirror ACP `session/update` verbatim so nothing is re-invented:
`{type:"message_chunk",text}` · `{type:"plan",entries:[{content,status}]}` ·
`{type:"tool_call",id,title,status,kind}` · `{type:"mode",mode}` ·
`{type:"permission_request",requestId,title,options:[]}` ·
`{type:"terminal",terminalId,output}`.

**`session/request_permission` maps onto `SideGate2`.** That is the whole point: ACP's permission
model and Side's approval gate are the same shape. Route a `permission_request` update into
`SideGate2.push(...)` and send the decision back to `/api/acp/permission`.

### `window.SideSpaces` — W3-B, prefix `.spc-`
```js
SideSpaces.list() / .create(name,repo,branch) / .remove(id,force)
SideSpaces.current() / .setCurrent(id)
SideSpaces.renderPicker(hostEl, opts)
SideSpaces.onChange(fn)
```
A Space = folder + branch + Brain slice + the nodes allowed to touch it. **Git worktrees underneath,
never the word "worktree" in the UI.** Degrade to a single implicit "Default" space with no daemon.

### `window.SideTriggers` — W3-C, prefix `.trg-`
```js
SideTriggers.list() / .add(t) / .remove(id) / .toggle(id,on)
SideTriggers.due(nowMs)        // -> [trigger]   pure, testable
SideTriggers.nextFire(t,nowMs) // -> ms|null
SideTriggers.renderStudio(hostEl, opts)
```
`t = {id, kind:"schedule"|"webhook"|"filechange", slug, enabled, rrule, url, path, lastFired}`.
Implement a **RFC 5545 RRULE subset**: `FREQ` (DAILY/WEEKLY/HOURLY), `INTERVAL`, `BYDAY`, `BYHOUR`,
`BYMINUTE`, `COUNT`, `UNTIL`. Persist `side_triggers`. A trigger that fires must respect the budget
cap and the gate exactly as a manual run does — **autonomy never bypasses the approval path.**

### `window.SideBrainWrite` — W3-D, prefix `.bwb-`
```js
SideBrainWrite.propose(runId, nodeId)  // -> Promise<[{text,source,confidence}]>  max 3
SideBrainWrite.pending() / .accept(id) / .reject(id)
SideBrainWrite.renderReview(hostEl, opts)
```
Closes the Brain loop: completed runs propose 0-3 facts, **approved through the same gate**, so the
Brain compounds instead of only storing. Never write a fact without human acceptance. Cite the
artifact each fact came from; a fact with no traceable source must not be proposed.

## FROZEN CONTRACT v1 — Wave 4 (close the loop)

### `window.SideACP` — W4-A, prefix `.acp-`
The **browser-side driver**. The daemon speaks ACP; nothing calls it yet. This is the connective tissue.
```js
SideACP.agents()                       // -> Promise<[{id,name,available,reason,install}]>
SideACP.start(nodeId, opts)            // opts {agent,cwd,spaceId,prompt} -> Promise<{sessionId}>
SideACP.send(sessionId, text, mode)    // -> Promise
SideACP.stop(sessionId)                // -> Promise
SideACP.sessions()                     // -> [{sessionId,nodeId,agent,state}]
SideACP.onUpdate(fn)                   // -> unsubscribe
SideACP.renderPicker(hostEl, opts)     // agent chooser
```
Poll `/api/acp/poll` and route each update shape:
- `plan` -> a `plan` artifact via `SideArtifacts.add` (update in place, do not append duplicates)
- `tool_call` -> node state + a `walkthrough` section
- `message_chunk` -> the node's live transcript (`window.ndOnNodeState` / `.ndz-msgs`)
- `terminal` -> the terminal component
- **`permission_request` -> `SideGate2.push(...)`, and the human's decision POSTs back to
  `/api/acp/permission`.** Nothing may auto-answer. This is the whole point of the wave.
Degrade silently with no daemon. Respect `SideBudget` where token counts are reported.

### Brain reads real facts — W4-B, prefix `.brf-`
`buildBrainView` currently renders `BRAINFACTS` (5757), `NODES_DEF` (17571), `SOURCES` (17623),
`LEARN` (17627) — hardcoded, zero `localStorage`. Make it read `side_brain_facts` (written by
`SideBrainWrite.accept`) **merged with** the existing demo set, with demo clearly labelled and an
honest empty state when there are no real facts. Do not delete the demo data: it is what sells Side
to someone who has not run anything yet. Additive module, no rewrite of the Brain view.

## Wave 3 — Engine (after W2 integrated)
- **W3-A** ACP client adapter (Python, feature-flagged beside `claude -p`).
  Maps `session/request_permission` -> Side's gate, `session/update` -> node state stream (live, finally),
  `terminal/*` -> terminal component, `fs/*` -> sandbox.
- **W3-B** Spaces — folder + branch + Brain slice + allowed nodes. Git worktrees underneath, hidden from UI.
- **W3-C** Triggers — RRULE schedule, webhook with inline URL, file-change watcher.
- **W3-D** Brain write-back — completed runs propose 0-3 facts through the same gate.

## Deferred to v1.0
MCP client engine (6 tools, not 48) · durable local runs · run-level undo · egress approval.

## Explicitly not building
Code diff viewer as a primary surface · autocomplete · Blueprints/snapshots · SCIM/RBAC ·
cloud VMs · being a better Conductor.
