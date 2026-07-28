# Ship-gate verification — the one branch I could not test
**Status: BLOCKED on an Anthropic API key. ~3 minutes of your time.**

## What is unverified

`runEngine` -> `wfStartRun` onView -> `SideGate2.open({onApprove})` -> approve -> `sideAfterShip()`.

Everything around it is verified. `SideGate2.open` was proven in isolation (Wave 2),
deny-aborts-downstream was proven against the real wf lifecycle, and the budget cap
and ledger were proven through `runApiNode`. **What has never run is the real
end-to-end path with a live engine call.**

## Why I could not do it

1. No Anthropic key. The key provided on 2026-07-28 was **OpenRouter**
   (`sk-or-v1-`). `SideEngine` calls `api.anthropic.com` directly with
   `anthropic-dangerous-direct-browser-access`; an OpenRouter key 401s there.
   Supporting OpenRouter means a base-URL + auth-header change in `SideEngine`
   — a real feature (BYO provider), not a config toggle.
2. `GRAPHS` and `curSlug` are host-internal, so a graph cannot be injected from
   outside the IIFE.
3. Driving the app headlessly from its landing overlay into a runnable canvas
   needs a UI flow I could not complete.

## How to close it

1. Open the app: `side` (or `open ~/Documents/Code/conductor/agent-build.html`).
2. Settings -> set your Anthropic API key (stored in `localStorage.side_api_key`,
   your machine only).
3. Load any template that contains a **gate** node — `/divorce-practice|skill`
   has one. Press **Run**.
4. When the run finishes, the ship gate should open as a **Gate v2 card** —
   artifacts and a verdict, not a bare text blob.

### What "pass" looks like

- The gate that appears is `.gt2-` (Gate v2), not the legacy `#gt-scrim` modal.
- It lists the run's artifacts under "What the agent handed back".
- If nothing set `done_when`, the verdict band reads **"Nothing was verified"**
  and Deny is the filled primary button. It must never render green.
- **Approve** -> the run completes and `sideAfterShip()` fires.
- **Deny** -> nothing ships, downstream steps are skipped.
- The budget ledger shows real spend for the run, and it is under the cap
  (default **$5.00** — change it in the pre-flight card if you want a lower ceiling
  for a first live test; $1 is plenty).

### If it fails

The most likely failure is the legacy `openGateFlow` modal appearing instead of
Gate v2. That would mean the `hasGate && window.SideGate2` branch at the
`runEngine` `onView` splice is not being reached. Tell me what appeared and I
can fix it in one edit.

## Cheaper alternative

If you would rather not spend on a live run, `SIDE_ACP_FS=rw` plus a local ACP
agent would exercise a comparable end-to-end path through the daemon at no API
cost — but that path is Wave 4's `SideACP` driver, and no ACP adapter is
installed on this machine yet.
