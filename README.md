<div align="center">

# 🟠 Side

**Your AI sidekick. One prompt in, a shipped result out — and you watch every move.**

[![License: MIT](https://img.shields.io/badge/license-MIT-orange.svg)](LICENSE)
[![Single file](https://img.shields.io/badge/app-one%20HTML%20file-blueviolet.svg)](app/index.html)
[![Local first](https://img.shields.io/badge/runs-100%25%20on%20your%20machine-1C9E6B.svg)](#under-the-hood)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-FF5CA8.svg)](CONTRIBUTING.md)

```sh
curl -fsSL sboghossian.github.io/side/install.sh | sh
```

*One command. Zero dependencies. Nothing leaves your computer.*

**[▶ Try it in your browser right now](https://sboghossian.github.io/side/app/)** — no install needed.

<img src="assets/02-canvas.png" alt="A fleet of AI agents working on the Side canvas" width="920">

</div>

---

## Why

99% of people don't code. And most people who *use* AI every day still can't see how it works. You can chat with a model, but you never see how agents think, how they plan, or how they work together. So people either trust AI blindly or stay a little scared of it.

Side is the missing picture: hand it one plain-English prompt and **watch** a fleet of agents plan, split the work, route each task to the right model, stop at your approval gates, and ship the result. Think **n8n for the AI age** — built so you learn by watching, not reading.

## What you get

### 🏠 Start with a sentence

Type what you want — a website migration, an outbound campaign, a deck. Or start from a template, your voice, a trigger, or the CLI. Side shows you the plan first: the goal, the steps, the agents, **and the cost — before anything runs.**

<img src="assets/01-home.png" alt="Side home — describe what to ship" width="920">

### 🎨 A canvas, not a black box

Every agent is a node you can open, reorder, pause, or re-run — colour-coded by what it does. Prompt, brain, tools, reasoning, orchestrator, gates. A live minimap tracks the whole run.

<img src="assets/02-canvas.png" alt="The Side canvas" width="920">

### 🧠 Route every task to the right model

Open any node. The orchestrator sends server-side rendering to one model, i18n to another, transcription to the cheapest one that clears the bar — and anything private to a local model. You can override every row.

<img src="assets/03-routing.png" alt="Per-node routing table" width="920">

### 📊 A fleet board you own

ARR, spend, live agents, human gates, heartbeat — every widget draggable, resizable, closable. It's your mission control, 1000% customizable.

<img src="assets/04-fleet-board.png" alt="The fleet dashboard" width="920">

### 🛡️ Nothing irreversible without your yes

Sends, merges, deploys, payments — everything risky stops at a human gate and lands in one inbox. Approve, modify, or reject, with one-click revert armed for 24 hours.

<img src="assets/05-inbox-gates.png" alt="Inbox with human approval gates" width="920">

### 💸 Every token, live — and capped

It runs on **your** API keys, so you see real spend as it happens: model split, cache hits, budget caps, auto-downgrade before you blow the budget.

<img src="assets/06-cost.png" alt="Live cost panel" width="920">

### ⚡ 64 templates, or save your own

Never start from a blank page. Run a template, love the result, save the whole fleet as a one-click Skill.

<img src="assets/07-templates.png" alt="Template library" width="920">

### 🚢 Every agent is a canvas

Your whole fleet at a glance — running, gated, done — each with a live thumbnail of its graph. Some public on your profile, most private on your machine.

<img src="assets/08-agents.png" alt="My agents" width="920">

## How it works

```
you describe it → Side plans the fleet → the fleet runs → it stops at your gate → shipped
```

1. **You describe it** — plain words in one box. No setup.
2. **Side plans the fleet** — the goal becomes real steps, each step an agent on the canvas.
3. **The fleet runs** — every agent in parallel, on your machine, with your keys.
4. **It stops at your gate** — nothing irreversible without your yes.
5. **Shipped — and saved** — the real thing, done. Save it as a Skill and run it again in one click.

## Install

```sh
curl -fsSL sboghossian.github.io/side/install.sh | sh
```

Then:

| command | what it does |
|---|---|
| `side` | launch Side (serves `localhost:4600`, opens a desktop window) |
| `side update` | pull the latest app |
| `side reset` | relaunch with a fresh profile (replays onboarding) |
| `side --version` | print version |

Configuration takes about 30 seconds and asks exactly three things: your name, a few sentences of context your agents read before every run, and your Anthropic API key. Paste the key and Side is real; skip it and you get the safe demo sandbox.

**Uninstall:** `rm -rf ~/.side ~/.local/bin/side` — that's everything.

## Under the hood

- **One self-contained HTML file.** The entire product — canvas, fleet board, inbox, cost, templates, onboarding, command palette — is a single zero-dependency file. No build step, no node_modules, no framework.
- **Local-first by design.** The launcher is ~80 lines of POSIX sh plus a stdlib-only Python daemon on `127.0.0.1` — it serves the app and writes your shipped artifacts under `~/Side/`, and nothing else. Your keys, your files, your machine.
- **A desktop app without the desktop-app tax.** `side` opens a chromeless app window if you have any Chromium browser, and falls back to your default browser. No Electron, no 200MB runtime.
- Press `⌘K` anywhere. Everything is reachable from the keyboard.

## The real engine (v0.2)

Bring your own Anthropic API key — paste it in onboarding, or press `⌘K → Set API key` anytime — and Side stops simulating:

- **Real plans.** Your prompt goes to Claude; the fleet on the canvas is the plan it returns.
- **Real runs.** Every node is a real Claude call — brain, tools, orchestrator, then the sub-agents in parallel, then a converge pass that writes the final deliverable.
- **Real numbers.** Tokens and cost in the shipped report come from actual API usage.
- **Real artifacts.** After you approve the gate, the deliverable is written to `~/Side/runs/<fleet>/result.md` on your disk by the local daemon.

Your key lives in your browser's local storage and is sent to exactly one place: `api.anthropic.com`. No key? Everything runs as the safe simulation — same product, demo fuel.

## Status & roadmap

- [x] The whole experience: canvas, routing, fleet board, gates, cost, templates, onboarding, ⌘K
- [x] One-command install + desktop launcher
- [x] **Real runs: bring-your-own-key engine** — browser-direct Anthropic calls, per-node model routing, parallel sub-agents
- [x] **Real artifacts on disk** — the `side` daemon writes shipped results under `~/Side/`
- [x] **Real mode** — with a key, Side is *your* workspace: your name, your agents, your runs and spend, persisted across launches. The demo data only exists in demo mode
- [ ] Real connectors: GitHub, Vercel, Linear, Gmail, PostHog
- [ ] Skills: export/import a fleet as a shareable one-click Skill
- [ ] Side World: public profiles + leaderboard

If that roadmap excites you — [come build it](CONTRIBUTING.md).

## Contributing

The whole product is one HTML file: open [`app/index.html`](app/index.html), edit, refresh. See [CONTRIBUTING.md](CONTRIBUTING.md) for the three rules that keep it that simple.

## License

[MIT](LICENSE) — do whatever you want with it.

---

<div align="center">

*Made for people who want to **control** AI instead of being rushed along by it.*

</div>
