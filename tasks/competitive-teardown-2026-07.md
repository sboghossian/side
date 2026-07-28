# Side — Competitive Functionality Teardown
**Date:** 2026-07-27 · **Side @** `477aef5` (master: `~/Documents/Code/conductor/agent-build.html`, 8.2MB)
**Scope:** Devin (Cloud + Desktop), Harness, Google Antigravity, Cursor 2.0, Factory.ai, GitHub Agent HQ,
OpenAI Codex app, Zed/ACP, the Claude-Code orchestrator ecosystem (Conductor / Superset / Emdash / Vibe Kanban /
Claude Squad / Parallel Code), the no-code camp (n8n / Gumloop / Lindy), and the agent-observability camp.

---

## Part 1 — The field, in one table

| Player | Shape | Surface | Isolation | Agent-neutral | Audience |
|---|---|---|---|---|---|
| **Devin Cloud** | Autonomous SWE + platform | Web app, Slack, API | VM per session | No (own agent) | Eng teams / enterprise |
| **Devin Desktop** | IDE + command center | VS Code fork (ex-Windsurf) | Spaces + worktrees | **Yes (ACP)** | Developers |
| **Google Antigravity** | Agent-first IDE | Editor + **Manager** surface | Workspaces | Partial | Developers |
| **Cursor 2.0** | IDE | Editor + parallel agent pane | **Git worktrees** (≤8) | No | Developers |
| **GitHub Agent HQ** | Fleet governance | "Mission control" in GitHub/VS Code/mobile/CLI | Branches | **Yes (any vendor)** | Eng orgs |
| **Factory.ai** | Droid fleet across SDLC | CLI, Web, Slack/Teams, Linear/Jira, **mobile** | Cloud env | No | Eng orgs |
| **Harness** | Agent *delivery lifecycle* | Pipelines + Knowledge Graph + marketplace | Pipeline sandbox | Yes | Platform/DevOps |
| **Codex app** | Long-run agent supervision | Desktop + CLI + IDE + ChatGPT | Cloud env | No | Developers |
| **Conductor / Superset / Emdash** | Parallel-agent desktop apps | Native dashboard | Git worktrees | Some | Developers |
| **n8n / Gumloop / Lindy** | Visual automation canvas | Node canvas | None | N/A | **Non-developers** |
| **Side** | Visual agent-fleet control room | Canvas (Flow + Map) + Brain + Gate | One sandbox dir | No (`claude -p`) | **Non-developers** |

**The single most important observation:** every serious player converged on the same surface within
12 months — Devin "Agent Command Center", GitHub "mission control", Antigravity "Manager surface",
Codex "supervise concurrent agents", Conductor/Superset/Emdash dashboards.

> The command center is now **commodity**. Side cannot win by being a better command center.

The two places differentiation actually moved:
1. **Below** — protocol + isolation (ACP, worktrees, VMs). Being standardized right now → also commoditizing.
2. **Above** — **verification and trust**. Antigravity Artifacts, Devin Review + Lifeguard + Session Insights,
   Harness eval gates + governance-as-pipeline. The bottleneck stopped being *"can the agent do it"*
   and became *"can I trust and verify what it did, at scale, without reading everything."*

---

## Part 2 — Functionality atlas

For each domain: what the leaders actually ship → what Side has (verified) → the gap → verdict.

---

### 1. Agent connectivity & neutrality

**Leaders.** ACP (Agent Client Protocol, Zed, Aug 2025, Apache-2.0) is now the de-facto standard.
JSON-RPC 2.0 over stdin/stdout, editor spawns agent as subprocess. Method surface:

- `initialize` — version + capability negotiation · `authenticate` · `logout`
- `session/new` · `session/load` (optional) · `session/prompt` · `session/cancel`
- `session/update` — streamed notifications: message chunks, **tool calls**, **plans**, available commands, mode changes
- `session/request_permission` — **agent asks the client to approve an operation**
- `session/set_mode`
- `fs/read_text_file` · `fs/write_text_file` (client-provided, absolute paths, 1-based lines)
- `terminal/create` · `terminal/output` · `terminal/wait_for_exit` · `terminal/kill` · `terminal/release`
- `elicitation/create` / `elicitation/complete` — agent requests structured info from the user
- Extensibility via `_meta` + underscore-prefixed methods

Adopters: Claude Agent, Codex CLI, GitHub Copilot, OpenHands, Cursor, Devin, Gemini CLI (reference impl),
Goose — 50+. Devin Desktop shipped ACP as its headline architectural move. There is an ACP Registry.

**Side today.** `claude -p` spawned by `bin/side-serve.py`, `--allowedTools Read,Grep,Glob`, `--output-format text`.
One agent, one vendor, no live streaming. `worktree`: **0 hits** in master. `Agent Client Protocol`: **0 hits**.

**Gap.** Total. Side is a Claude Code GUI, not a surface.

**The thing worth noticing:** `session/request_permission` **is Side's approval gate.**
`session/update` carrying *plans* and *tool calls* is Side's node state stream. `terminal/*` is Side's terminal panel.
Side is already shaped like an ACP client — it just implemented one bespoke agent instead of the protocol.

**Verdict: STEAL — but as substrate, not as the headline.** See Part 5.

---

### 2. Isolation & parallelism

**Leaders.**
- **Cursor 2.0** — git worktrees, one per agent, separate branch + separate compilation target. Up to 8 parallel.
  Community guidance: start at 2–3, merge-conflict complexity scales non-linearly.
  Enabled by a `worktree.json` in project root; `/multitask` spins async subagents.
- **Devin Cloud** — full **isolated VM per session**, including per *child* session.
- **Devin Desktop** — **Spaces**: "a new way to share context between agents while grouping sessions, PRs,
  files, and context." Worktree management inside the Space.
- **Conductor / Superset / Emdash / Claude Squad** — worktree-per-task is the universal pattern of the whole camp.
- **Known ceiling (from the orchestrator ecosystem):** worktrees stop merge *collisions* but not *drift* —
  two agents solving overlapping problems in parallel produce divergent-but-valid solutions.
  **Merge review becomes the bottleneck**, and success depends on task-decomposition skill.

**Side today.** One sandbox: `~/Side/runs/<slug>/workspace`, traversal-proof. 3-job cap, 300s timeout.
No worktrees, no branches per node, no isolation between concurrently-running nodes.

**Gap.** Real. And Side has *already been bitten by exactly this*: v0.7.6 shipped a double-integrated
fork-compare module because two Claude sessions edited the same master concurrently (20.9KB of dead code
shipped in 0.7.5). That is the drift failure mode, experienced first-hand.

**Verdict: ADAPT.** Side needs an isolation primitive, but "worktree" is a developer word. Side's version is a
**Space** = one folder + one branch + one Brain slice + the nodes allowed to touch it. Ship the concept, hide the git.

---

### 3. Orchestration topology

**Leaders.**
- **Devin manages Devins** — a main session delegates to managed child Devins in parallel, each with its own VM;
  children return **JSON via a defined structured-output schema**; the parent coordinates, monitors, resolves conflicts.
  Child sessions are first-class in the sidebar (parent-child grouping, sub-Devin filtering, independent pinning).
- **Antigravity** — "orchestrate multiple subagents across all your environments"; Manager view runs ~5 parallel agents;
  2.0 adds **Projects** (grouping conversations), multi-workspace operation, **scheduled messages**.
- **Claude Code (native)** — subagents with isolated context windows + own tool permissions + own model;
  **Agent Teams** for parallel teammates on one machine; **Dynamic Workflows** (June 2026) fan out tens-to-hundreds
  of subagents in one session. Teammates auto-load CLAUDE.md; `TaskCompleted` hook gates quality.
- **Devin Local** — rewritten in Rust, ~30% more token-efficient, supports subagents.

**Side today.** `sideSpawnSubfleet` — visual sub-fleet spawning with lineage on canvas. Decision-tree pipeline
(`SidePipeline`): pre → classify → branch lanes (code/content/research/skill/outbound/default) → post (budget, report).
64 graph templates (`ABTPL`).

**Gap.** Small — and Side is arguably **ahead on legibility**. Nobody else shows fan-out as a *visual lineage graph*.
Missing: structured-output schemas on children (Devin's mechanism for making a child's result machine-checkable).

**Verdict: HOLD — this is a strength. Add structured output (see §8).**

---

### 4. Work surfaces / projections

**Leaders.**
- **Devin Desktop Command Center** — Kanban by status: **Running / Waiting for Review / Done**. Plus list view
  with filters by status, Space, PR, agent type. Status indicators, PR tracking, timestamps.
- **Antigravity** — two surfaces: **Editor** (synchronous, tab completion, inline commands) and **Manager**
  (asynchronous, spawn/orchestrate/observe across workspaces). Explicit thesis:
  *"flips the paradigm of agents being embedded within surfaces to one where the surfaces are embedded into the agent."*
- **GitHub Agent HQ** — mission control replicated across GitHub web, VS Code, **mobile**, and CLI.
- **Devin session management** — folders, pinning, archive/unarchive (+ "archive all" with undo), rename,
  categories/subcategories, read/unread orange dot, message permalinks, session origin badge (webapp/Slack/API/CLI),
  command palette (Cmd+K), focus mode (Cmd+Shift+F).

**Side today.** Two projections over one graph — **Flow** (default, vertical document, lanes) and **Map** (2D canvas
with zoom/minimap), toggled via `#tb-flowmap`, persisted in `side_canvas_mode`. Plus isometric agents city,
Brain view, Pipeline Studio, Inbox, component market (77 blocks). `renderSessionTabs` / `tabSessionSave|Restore|Forget`.
cmdk exists. Kanban: 12 hits, but only as a *stage* toggle in the agents view — not an operate-mode board.

**Gap.** Side has **create** views and **explore** views but no **operate** view. When 20 nodes are live across
5 canvases, Flow and Map both fail: they show structure, not state. There is no single "what needs me right now."

**Verdict: STEAL — cheap and high-value.** A **Board** projection is the third lens over the same graph
(nodes already carry `status`: idle/run/queue/done/review). Flow = create · Board = operate · Map = comprehend.
This also completes the existing three-planes thesis (canvas / inbox / brain).

---

### 5. Review & approval

**Leaders.**
- **Devin Review** — a full product: interactive PR review on GitHub / GitLab / GHES, intelligent diff grouping,
  inline comments + threading, AI chat agent inside the review, security findings section, **Auto-Fix** for detected
  bugs, apply-changes-as-commits from chat, batch comments, per-PR spend limit, per-PR auto-review toggle,
  code-owner blocking indicator, required-approval count, checks tab with CI job logs, diff-line permalinks,
  comment language selector (EN/JA/ES), split-view auto-disable on narrow panels, **mobile** (tap-to-open chat,
  pull-to-refresh), merge-time-reduction % display.
- **Antigravity** — feedback via **Google-Docs-style comments on text artifacts** and **select-and-comment on
  visual artifacts** (screenshots). Feedback integrates into the running agent without interrupting it.
- **Lifeguard** (Devin static analysis) — bug detection, security scan, repo-rule files, chained attack paths,
  "Repo rule" badge on findings, ignore-comment explanations.
- **GitHub Agent HQ** — branch controls for agent-created code, identity management, one-click merge-conflict resolution,
  and **provenance**: which agent generated which block of code.

**Side today.** Approval gate that genuinely blocks the run (fixed in `2ded46b` — Deny now aborts downstream;
it used to be decorative). Inbox (90 hits). `sideLintGate` pre-run check with LINTSKIP signature.
`diff`: 98 hits, but as component/template content, not a review surface. `Waiting for review`: **0 hits**.

**Gap.** Side's gate is a **yes/no button attached to a text blob.** Every leader made review a *workspace*.

**But — the critical divergence.** Devin, Cursor, Conductor, GitHub all assume **the reviewer is an engineer
who can read a diff**. Side's stated audience cannot. A diff viewer would be the wrong feature for Side's user
even though it is the right feature for everyone else's.

**Verdict: STEAL THE SHAPE, INVERT THE CONTENT.** Review becomes a workspace, but the reviewable object is
an **artifact**, not a diff. See §6 — this is Side's actual wedge.

---

### 6. Verification & artifacts ⭐ **the biggest missing idea**

**Leaders.**
- **Antigravity Artifacts** — agents emit tangible deliverables instead of raw tool calls:
  **task lists · implementation plans · walkthroughs · screenshots · browser recordings.**
  Stated purpose: *"These Artifacts allow you to verify the agent's logic at a glance."*
  Agents *"thoroughly think through verification of the work, not just the work itself"*, and verification results
  render alongside task context. Comment directly on any artifact.
- **Devin** — desktop app E2E testing on Linux with computer-use, **edited recording of the test run**,
  pass/fail summary cards, playback speed + loop, QA approval workflow, downloadable session video.
  **Session Insights** (on-demand, API, auto for large sessions).
- **Harness** — **eval gates as pipeline stages**. Agents get evaluated the same way code gets tested, with
  deployment approvals and security checks as stages, from agent creation through everything it does after.
- **Observability camp** (LangSmith / Langfuse / Braintrust / AgentOps) — 2026 mature pattern is
  *runtime tracing + eval gates*: automated scorers grade outputs and **block a regression from shipping**.
  Step-level cost attribution; time-travel debugging; run replay.

**Side today.** `artifact`: 44 hits — an Artifacts panel exists in the Node Designer right rail, and gated results
land at `~/Side/runs/<slug>/result.md`. `run-legib` module (`.rlg-`) gives run peek + preview. `eval`: **6 hits** — no
eval layer. No screenshots, no recordings, no walkthroughs, no pass/fail, no definition-of-done.
`2ded46b` explicitly killed the *fabricated* "SHA-256 verified" badge — correct call, but it left a hole:
**there is now no verification claim at all.**

**Gap.** The largest one, and the most valuable.

**Why it matters more for Side than for anyone else:** Side's user cannot audit an agent by reading code.
The only way a non-developer can safely approve autonomous work is if the agent hands back
*something a human can look at* — a plan, a checklist with ticks, a before/after screenshot, a 20-second recording,
a plain-language summary of what changed. That is exactly Antigravity's Artifacts, aimed at an audience
Antigravity does not serve.

**Verdict: STEAL — and make it the product.**

---

### 7. Context & memory

**Leaders.**
- **Devin Knowledge** — knowledge notes, folder hierarchy, knowledge *suggestions* surfaced inside sessions,
  standalone worklog events for suggestions, search with folder auto-expand, enterprise limit 300 items,
  enable/disable per session via API.
- **DeepWiki** — repo indexing → generated wiki; v2 with subagents and agentic page writers; selectable **effort level
  with ACU ranges** and a cost-breakdown modal before generating.
- **Harness Software Delivery Knowledge Graph** — connected map of services, pipelines, deployments, infra,
  incidents, security findings; the agents *reason over the graph*.
- **Factory** — unifies GitHub + Notion + Linear + Slack + Sentry into retrievable "enterprise memory."
- **Antigravity** — agents both retrieve from *and contribute to* a knowledge base: code snippets, architecture
  patterns, successful procedural steps for recurring subtasks.

**Side today.** The **Brain** — force-graph, clusters, Discover stream, nurture schedule, sources, learnings;
`ctx-core` (edges carry context, `@` menu, merge); `brain-pick` (fact → prompt chip) + `.lint-` pre-run gate.
Brain-seed choice at onboarding (code / notes / empty). Fabricated "127 facts" was killed in `2ded46b`.

> **CORRECTION 2026-07-28 (verified in code, supersedes the original assessment below).**
> The Brain **does not persist anything.** There are **zero `localStorage` calls** anywhere in the Brain
> region (lines ~17400-18700). `BRAINFACTS` (5757), `NODES_DEF` (17571), `SOURCES` (17623) and `LEARN`
> (17627) are hardcoded arrays rendered fresh each session. The only Brain-related storage in the whole
> app is `side_brain_seed` (3346), which records the onboarding *choice* (code/notes/empty) — not a fact.
> Found by W3-D while building write-back; confirmed independently.
>
> So the gap is not "write vs read". **The Brain is a stage set.** It renders convincingly and stores
> nothing. The live parts are real — `sideBrainChipAdd` genuinely feeds grounding chips into the next
> prompt, and the `.lint-` pre-run gate works — but the graph, the clusters and the "learnings" feed
> are fiction.
>
> This also corrects **Part 3, asset #3** below: "the Brain as a first-class plane" was true about the
> *presentation* and false about the substance. Do not build further strategy on it until it stores.

**Gap.** Larger than first assessed. Antigravity's agents **write back** ("contributing to a knowledge
base"), Devin generates knowledge *suggestions* from sessions, Factory ingests from five tools
continuously. Side neither writes nor reads — it draws.

**Verdict: ADAPT.** Close the loop: every completed run proposes 0–3 Brain facts, gated by the same approval
mechanism. This makes the Brain compound, which is Side's stated third plane, and it's cheap.

---

### 8. Reusable procedure (playbooks / skills / templates)

**Leaders.**
- **Devin Playbooks** — *"a template prompt that encodes your best instructions, the steps Devin should follow,
  and **the definition of 'done'** — all in one reusable package."* Plus: per-playbook Devin mode (Fast/Normal/Ultra),
  **structured output schema**, version history, session-count + user analytics, weekly activity charts,
  promotion to enterprise level, preview-on-hover in the message box, launch-session-from-playbook,
  and a full CRUD API.
- **Devin Skills** — `/name` slash invocation and `@mention` syntax; enterprise skills analytics;
  plugin architecture for distribution with **required / optional / forbidden** governance.
- **Harness Agent Marketplace** — GA, agents distributed like packages.
- **n8n** — AI Agent Templates (lead qualification, content pipelines, data enrichment).

**Side today.** 193 `template` hits / 64 `ABTPL` graph templates + 77-block component market + `SideEco`
14-item ecosystem catalog with "From the ecosystem" rows on 2+ char query. `playbook`: **2 hits**.

**Gap.** Side's templates are **graph shapes**. Devin's playbooks are **procedures with a success criterion**.
Side has the reuse mechanism and is missing the two fields that make a run checkable:
**definition of done** and **structured output schema**.

**Verdict: STEAL the two fields.** They are ~1 day of work and they unlock §6 (verification) entirely —
you cannot verify without a stated definition of done.

---

### 9. Environment reproducibility

**Leaders.** Devin **Blueprints** — declarative, git-backed, version-controlled environment config with a
dedicated editor (per-section play buttons, bottom terminal drawer, deep-linking), **snapshot builds**
(on-demand, build history with status/platform filters, cancel in-progress, delete, **revert**, enterprise-wide
build schedule, max-concurrent-builds cap, **drift warnings with re-sync buttons**), platform defaults (Linux/Windows),
session-scoped env vars. Classic environment setup deprecated 2026-06-30.

**Side today.** Sandbox dir + `sandbox-exec` read-jail + secret-env scrub (from the `2ded46b` hardening).

**Gap.** Large but **mostly irrelevant** — this is enterprise-CI-shaped, aimed at reproducing a build machine.
Side's user does not have a build machine.

**Verdict: IGNORE.** The one transferable atom: **snapshot + revert**. "Undo everything that run did"
is a trust primitive a non-developer needs badly. Side has per-node versioning DAG already — extend it to a
run-level undo.

---

### 10. Triggers & automation ⭐ **second biggest gap**

**Leaders.** Devin **Automations** — this is a deep surface:
- Triggers: **RRULE (RFC 5545) recurring schedules**, one-time schedules, run-now, **webhooks with inline URL**,
  **GitHub file-change / push**, Linear issue (with project filter), Jira issue (per-project), Slack channel
  monitoring (auto-joins public channels), and `snapshot_build:completed` events.
- Identity: personal automations (run as you), org automations, **service-user** automations via API.
- Controls: **per-session ACU limits** (min lowered to 1), consumption tracking per automation,
  results posted to a Slack channel, failure-notification rate limiting, run-as-user assignment,
  email notifications, schedule list cap 200, cost column on the scheduled-sessions list.
- Long-running sessions can *receive* trigger events mid-flight.

**Antigravity 2.0** adds **scheduled messages**. **Factory Droid Action** runs automatically on every PR.

**Side today.** `schedule` 53 / `cron` 56 / `routine` 65 hits — but these are **demo template content and glossary
copy** ("Git-commit-derived standup written each morning"), not an execution layer. Runs start when you press go.

**Gap.** Severe, and it is a *product-category* gap, not a feature gap.
An orchestrator that only fires when you're watching is a demo. The whole promise of "give your agents a real job"
is that the job happens when you are not there.

**Verdict: STEAL.** This is the cheapest path from prototype to daily-use tool.

---

### 11. Integrations

**Leaders.** Devin: **48+ new MCP connectors in 2026**, 42 beta → GA. Named: Figma (official), PostHog, Datadog,
Linear, Notion, Miro, Mixpanel, Honeycomb, Postman, monday.com, Klaviyo, LaunchDarkly, Fathom, Attio, Calendly,
Google Drive, Axiom, Tavily, Granola, Mobbin, Amplitude. Plus: personal vs org MCP scoping, **read-only mode toggle**,
OAuth resource parameter (RFC 8707), one-click OAuth install, token-expiry warnings, "installation out of date" banner,
per-session MCP usage tracking, MCP error output channel, enterprise MCP registry enforcement.
Devin Desktop ships 20+ MCP servers. Deep first-party: Slack (thread sync both ways, `!fast`/`!ultra`/`!windows`
bang-commands, `/btw` `/queue` `/ask` slash commands), Linear, Jira, GitHub/GHES, GitLab.

**Side today.** MCP exists as a **glossary card** ("the plug that connects your tools"), a **component-market entry**
("MCP Server — call any tool exposed by a connected MCP server"), and demo template text. Zero MCP client code.
Roadmap already says "v0.8 = MCP client engine."

**Gap.** Large. A visual orchestrator is worth exactly as much as the tools its nodes can touch.
Side currently orchestrates one thing: Claude reading files.

**Verdict: STEAL — but sequenced after verification.** MCP is what makes Side *useful*; artifacts are what make it
*trustworthy*. Untrustworthy-and-useful is worse than trustworthy-and-narrow for a first real user.

---

### 12. Observability, cost & analytics

**Leaders.** Devin's **ACU** system is the reference implementation:
per-product consumption breakdown (Sessions / Review / Indexing), per-user and service-user tracking,
repository-level review spend, current-vs-previous cycle columns, session activity bar charts, metric
count/percentage toggle, **CSV export**, top-10 rankings, per-user Total ACUs, weekly PR-ratio chart,
repo filtering, URL-persisted time ranges, **session ACU hard caps with acknowledgement**, configurable
auto-reload threshold, ACU-visibility admin toggle, billing warnings for unconsumed ACUs, **audit trail of ACU
limit changes**, per-PR spend limit, cost column on scheduled sessions, and a cost-breakdown modal
*before* an expensive DeepWiki generation.
Observability camp: step-level cost attribution, run replay, time-travel debugging, trace sampling for drift.

**Side today.** `budget` 39 / `spend` 38 hits — the pipeline's post-step includes "budget" and templates carry
`cost`/`tok` fields (`"$0.04"`, `"12k"`). `LASTRUN[slug]` feeds real numbers into `sideRunSummary` for real runs.
No caps, no ledger, no cross-run history, no export.

**Gap.** Medium-large, and it is a **trust** gap disguised as an analytics gap.
Side is BYO-key: the user is spending their own money on an agent they cannot read. Without a hard cap
and a visible ledger, the correct user behaviour is to not press go.

**Verdict: STEAL the minimum viable core:** per-run hard cap with pre-flight estimate, a running ledger,
and a stop-on-exceed. Skip the enterprise analytics entirely.

---

### 13. Governance, permissions, audit

**Leaders.** Devin: RBAC with custom roles, named permissions (`ManageAccountServiceUsers`, `ViewAccountConsumption`,
`ViewOrgWiki`), SSO with JIT provisioning, SCIM group sync, IdP group→role mapping with conflict detection,
default member roles, **network access request approval** (agent asks to reach a domain; human approves —
approvable from Slack), **secure mode** (internet deployment disabled), secret scoping personal/org, secret masking,
audit logs for ACU changes / MCP updates / secret link-unlink / telemetry settings, enterprise commit-email enforcement,
GPC/CCPA/CPRA support. **Harness** applies existing pipeline policies, approvals and evidence to agents themselves.
**GitHub Agent HQ** — branch controls for agent code, agent identity management, agent-to-code provenance.

**Side today.** `permission`: **1 hit**. Solid *security* work exists (CSRF + Origin gate on mutating endpoints,
per-launch bearer token, Host validation against DNS rebinding, `sandbox-exec` read-jail, secret-env scrub, strict CSP)
but no *governance model*. `audit` 83 hits = the audit-lens templates, not an audit log.

**Gap.** Large on paper, **mostly irrelevant to Side's audience** — a single user does not need SCIM.

**Verdict: MOSTLY IGNORE.** Two transferable atoms:
1. **Network access request approval** — the agent asks to reach a domain, the human approves. This is the same
   gate mechanic Side already has, applied to egress. Excellent trust primitive for a solo user.
2. **Provenance** — which agent produced which change. Needed the moment Side is agent-neutral (§1).

---

### 14. Ambient presence & multi-surface

**Leaders.**
- **Devin lives in Slack.** Bidirectional thread sync, mid-session mode switching by bang-command, slash commands,
  preceding-thread context, `!channel` override, "Sent from Slack" badges, unarchive-on-mention, network-approval
  from Slack, automation results posted to channels. Plus Teams, Linear comments, Jira comments.
- **Factory** — CLI + Web + **Slack/Teams** + Linear/Jira + **mobile**.
- **GitHub Agent HQ** — mission control on GitHub web, VS Code, **mobile**, CLI.
- **Devin webapp** — mobile layouts, tap-to-open chat, pull-to-refresh, high-res Android home-screen icon,
  **voice recording button** for hands-free input (mic available *while Devin works*).

**Side today.** A Chrome `--app` window on `127.0.0.1:4600`. Mobile: 25 hits (a Brain drawer for mobile,
added in `2ded46b`). `voice` 133 hits = demo template content, not input.

**Gap.** Severe for the stated audience. Asynchronous agents are only valuable if approval is asynchronous too.
If the human must be at the laptop with the app open to unblock a run, the agents are not really autonomous —
the human is just waiting in a nicer room.

**Verdict: STEAL, narrowly.** Not "build Side for mobile." Build **the gate** for mobile: a push notification
that says *"Your research agent finished. Here's what it made. Approve?"* with the artifact rendered and two buttons.
That single flow is the difference between a prototype and something used daily.

---

### 15. Durability & handoff

**Leaders.** Devin Desktop **cloud handoff** — "close your laptop", work continues remotely, plan reviewable before
implementation. Cursor **Cloud Agents** (renamed from Background Agents) — isolated Ubuntu VMs, 99.9% reliability,
instant startup. Codex — cloud tasks on OpenAI infra so long jobs don't tie up the laptop.
Devin: 3× faster startup, sessions that sleep and resume, long-session streaming.

**Side today.** `bin/side-serve.py` on localhost. 300s timeout, 3-job cap. Machine sleeps → run dies.

**Gap.** Real, and expensive to close properly (it means infrastructure and a business model).

**Verdict: DEFER, take the cheap 20%.** Not cloud. Just: **daemon survives the app window closing**,
runs persist to disk, runs resume after wake, and the notification finds you (§14).
That delivers "close the laptop" psychologically at ~2% of the cost.

---

### 16. Distribution & business model

**Leaders.** Devin Desktop: Free / Pro $20 / **Max $200** / Teams $80 + $40 per seat / Enterprise custom.
Free tier includes **unlimited SWE-1.6** ("fastest coding model in the world"). 1M+ users, 4000+ enterprise customers.
Windsurf users auto-migrated OTA with pricing, plans, extensions and settings preserved.
Antigravity: **free public preview**. Harness: agent marketplace GA to all customers.
Conductor: free, closed, macOS/Apple-Silicon only. Superset: Apache-2.0, cross-platform, zero telemetry, 3.2k stars in 3 days.
Crystal: deprecated Feb 2026 → paid closed-source successor. Vibe Kanban: Bloop shut down April 2026, community-maintained.

**Side today.** MIT, free, BYO Anthropic key, `curl | sh` install, GitHub Pages demo.

**Read.** Two things stand out:
1. **The free-model subsidy is a moat Side cannot cross.** Cognition gives away unlimited inference.
   BYO-key is Side's only viable model — and it's genuinely better on trust ("your key, your data, your machine").
   Lean into it explicitly rather than treating it as a limitation.
2. **The orchestrator graveyard is real.** Crystal deprecated, Vibe Kanban abandoned, Bloop shut down —
   all in the first half of 2026, all developer-facing wrappers around Claude Code.
   *Being a better wrapper is a losing position.* Superset's traction (Apache-2.0, zero telemetry, cross-platform)
   suggests the surviving open-source niche is **trust and portability**, not features.

---

## Part 3 — What Side already has that nobody else does

Worth protecting deliberately, because these are the only defensible assets:

1. **Two live projections of one graph** (Flow + Map). Devin has a kanban. Cursor has a list. Nobody has a
   spatial *and* a document view of the same fleet.
2. **The isometric agent city.** Dismissible as decoration — it is not. It is the only interface in the entire
   competitive set that a non-technical person finds *legible at a glance*.
3. **The Brain as a first-class plane.** Harness has a knowledge graph but it's infrastructure telemetry.
   Devin's Knowledge is a notes folder. Side's Brain is the only one presented as a *thing the user owns and grows*.
4. **The decision-tree pipeline** (`SidePipeline`). Every prompt routed through pre → classify → lane → post,
   editable in a studio. n8n makes you build the routing. Devin hides it. Side shows it and lets you edit it.
5. **Visual sub-fleet lineage.** Fan-out you can see.
6. **The component market** (77 blocks) + ecosystem catalog. The distribution primitive already exists.
7. **Zero-dependency single file.** 8.2MB, ES5, no build step, no telemetry, MIT.
   In a graveyard of abandoned Electron wrappers, that is a durability argument.

---

## Part 4 — Strategic read

**Three honest options.**

### Option A — "Better command center for developers"
Build ACP + worktrees + kanban + diff review + PR integration. Compete with Devin Desktop / Conductor / Superset.
- **Cost:** high. **Ceiling:** low.
- Cognition acquired an IDE with 1M users to reach this position. GitHub ships it inside GitHub. Google ships it free.
  Three of the four open-source tools in this exact niche died or were abandoned within six months.
- **Verdict: no.**

### Option B — "The trust layer for people who can't read code" ⭐
Artifact-first review, definition-of-done verification, mobile approval, triggers, budget caps.
- **Cost:** medium. **Ceiling:** high. **Contested by:** nobody.
- The entire dev-tool camp assumes the reviewer is an engineer. The entire no-code camp (n8n / Gumloop / Lindy)
  assumes the *worker* is a deterministic workflow, not an autonomous agent, and offers no verification story at all.
- Side already has the three hard parts: the gate, the Brain, the canvas.
- **Verdict: this is the wedge.**

### Option C — "Neutral visual surface for any agent" (ACP-first)
Become the canvas every agent plugs into.
- **Cost:** medium. **Ceiling:** very high, but it is *infrastructure*, and infrastructure adoption is a
  developer-audience game — competing with Zed and GitHub on protocol gravity.
- **Verdict: not a strategy on its own. But it is the correct substrate for B** — ACP is how Side stops being
  a Claude Code wrapper (the losing position identified in §16) without building N bespoke integrations.

**Pick: B, built on C's substrate.**

**Positioning consequence.** Devin Desktop's headline is *"The home for every agent you run."*
Side's end-card is *"Where your AI agents come to work."* Those are the same sentence.
The promise no longer differentiates — the **audience** does. Side's line has to carry the non-developer claim
in the headline itself, not in the README.

---

## Part 5 — The plan

### v0.9 — "Trust" (the wedge; nothing here is contested by anyone)

**1. Artifact-first runs** *(from Antigravity)*
Every node emits typed artifacts instead of a text blob: `plan` · `checklist` · `walkthrough` ·
`screenshot` · `recording` · `file-change` (rendered as plain-language before/after, **not** a diff) · `summary`.
Artifacts render in the gate and in the Node Designer right rail (the panel already exists).
Comment on any artifact → the comment feeds back into the run. Reuses the existing `.rlg-` run-legibility module.

**2. Definition of done + structured output per node** *(from Devin Playbooks + Harness eval gates)*
Two new fields on every node and every template: `done_when` (plain language) and `output_schema` (optional JSON).
After a run, a cheap verifier (Haiku) checks the artifacts against `done_when` and returns pass / fail / unsure
with reasons. The gate shows that verdict. **This is what makes a non-coder able to approve safely** — and it
replaces the fabricated "SHA-256 verified" badge that was correctly killed in `2ded46b` with a real claim.

**3. Approve from your phone**
The gate becomes a mobile-first inbox: push notification → artifact rendered → Approve / Deny / Comment.
Nothing else needs to be mobile. This is the flow that makes the agents actually asynchronous.

**4. Board projection**
Third lens over the same graph, grouped by state: **Running / Needs you / Done**. Nodes already carry `status`.
Flow = create · Board = operate · Map = comprehend. Roughly a day of work.

**5. Run budget caps**
Pre-flight cost estimate, hard per-run cap, running ledger, stop-on-exceed. Non-negotiable for a BYO-key product.

### v0.9.1 — "Real work"

**6. Triggers** — RRULE schedule, webhook (inline URL), file-change watcher, inbound email.
Turns Side from a demo into the thing that runs your morning. Biggest product-category unlock per hour spent.

**7. ACP client** — replace the bespoke `claude -p` spawn in `side-serve.py` with an ACP client.
Concrete mapping: `session/request_permission` → **Side's existing gate**; `session/update` plan+tool-call
notifications → **node state stream** (finally live, not batched at the end); `terminal/*` → **terminal panel**;
`fs/*` → **sandbox**. Unlocks Codex, Copilot, OpenHands, Gemini CLI, Goose as node types.
Stops Side being a Claude Code wrapper — the position that killed Crystal and Vibe Kanban.

**8. Spaces** — folder + branch + Brain slice + the nodes allowed to touch it. Git worktrees underneath, hidden.
Closes the drift bug Side already shipped once (v0.7.6).

**9. Brain write-back** — every completed run proposes 0–3 facts, approved through the same gate.
Makes the third plane compound instead of just storing.

### v1.0 — "Reach"

**10. MCP client engine** — the roadmap item, now with a reason: artifacts are worth more when nodes can touch
real tools. Start with the 6 that matter for the actual audience (Gmail, Calendar, Notion, Linear, Drive, Slack),
not 48.
**11. Durable local runs** — daemon survives window close, runs persist and resume after wake.
**12. Run-level undo** — extend the per-node versioning DAG to "revert everything that run did."
**13. Egress approval** — agent requests a domain, human approves *(from Devin's network access requests)*.

### Explicitly NOT building

- **A code diff viewer.** Right for Devin's user, wrong for Side's.
- **The Node Designer as a real IDE.** `2ded46b` rebuilt it as a VS Code 3-zone layout — that walks into the one
  lane Side cannot win (Devin Desktop *is* a VS Code fork with 1M users and real language servers). The Node
  Designer wins as the **visual component canvas** from the original sketch ("everything is a component instead
  of a sidebar"), not as an editor. **Taste call needed from Stephane.**
- Autocomplete / Supercomplete. Not the game.
- Blueprints / snapshot builds / SCIM / RBAC / enterprise analytics. Wrong audience entirely.
- Cloud VMs. Wrong cost structure for a free MIT tool.
- Being a better Conductor.

---

## Sources

- Devin Desktop — https://devin.ai/desktop/ · https://devin.ai/blog/windsurf-is-now-devin-desktop
- Devin 2026 release notes (feature inventory) — https://docs.devin.ai/release-notes/2026
- Devin Playbooks — https://docs.devin.ai/product-guides/using-playbooks
- Agent Client Protocol — https://agentclientprotocol.com/protocol/overview · https://zed.dev/acp · https://zed.dev/blog/acp-registry
- Google Antigravity — https://antigravity.google/blog/introducing-google-antigravity · https://antigravity.google/product
- Cursor 2.0 — https://cursor.com/changelog/2-0
- Harness Autonomous Worker Agents / Agent DLC — https://thenewstack.io/harness-ai-agent-dlc/ · https://siliconangle.com/2026/07/21/harness-launches-agent-dlc-developers-deploy-ai-agents-using-familiar-processes-tools/
- GitHub Agent HQ — https://github.blog/news-insights/company-news/welcome-home-agents/
- Factory.ai — https://factory.ai/news/factory-is-ga
- Orchestrator ecosystem — https://rustman.org/wiki/conductor-parallel-agents/ · https://www.augmentcode.com/tools/open-source-agent-orchestrators
- Claude Code Agent Teams / subagents — https://www.developersdigest.tech/blog/claude-code-agent-teams-subagents-2026
- No-code camp — https://www.lindy.ai/blog/gumloop-vs-n8n
- Agent observability — https://www.digitalapplied.com/blog/agent-observability-2026-evals-traces-cost-guide
