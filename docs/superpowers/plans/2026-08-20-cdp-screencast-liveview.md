# Per-Target CDP Screencast Live-View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan phase-by-phase with review between phases. Steps use checkbox (`- [ ]`) syntax. **This is a phased architectural rework — do NOT one-shot it. Phase 0 (spike) gates the detailed design of Phases 1-4.**

**Goal:** Replace the single-VNC-of-the-X-display live-view with a per-target CDP screencast, so each container browser tab/window renders as its own dockview panel — fixing the matchbox fullscreen "trapped popup" problem, raising resolution (viewport-driven, not a fixed 1280×800), and cutting latency/CPU (page frames instead of full-framebuffer VNC on 1 vCPU).

**Architecture:** Today: `BrowserDockPanel` (noVNC) → `browser_view.py` relay → websockify → `Xvnc :1` of the whole display; matchbox fullscreens windows; software GL. New: for each CDP page target, `Page.startScreencast` emits JPEG/WebP frames; a backend relay forwards frames over the existing `bv-{env}` CloudFront VPC-origin WS door to a frontend canvas panel; input is captured on the canvas and sent back as CDP `Input.dispatchMouseEvent`/`dispatchKeyEvent`. Each target is its own dockview panel (reusing the layout the workspace already has). Resolution follows `Page.setDeviceMetricsOverride`/the viewport, not the X display.

**Tech Stack:** CDP (`Page.startScreencast`, `Input.*`, `Target.*`), the OpenClaw fork `extensions/browser` gateway (protocol v4), Isol8 backend `routers/browser_view.py` + gateway pool, frontend dockview + a canvas panel, the `bv-{env}` CloudFront VPC-origin door (see memory `reference_apigw_no_websockets_bv_cloudfront_door`).

**Spec:** self-contained; derived from the 2026-08-20 session investigation (scoping subagent findings on the matchbox trap + the industry rendering comparison). Companion to the trapped-popup interim fix already shipped (#1928, tab-list + Close).

## Global Constraints

- **Keep the VNC path working until the screencast path is proven and flagged on.** This is a parallel build behind a flag, not a rip-and-replace. Do not delete `browser_view.py`'s VNC relay or the Xvnc/matchbox boot until Phase 4 cutover is validated in prod.
- **Reuse the `bv-{env}` CloudFront VPC-origin door for the frame WS** — it is the sanctioned raw-WS path (API Gateway can't carry these WebSockets); auth rides the Clerk session cookie / subprotocol, same as the current noVNC relay.
- **Do not regress the agent's own browser driving.** The agent uses `connectOverCDP` on 18800; a screencast client is an ADDITIONAL CDP consumer — verify multi-client CDP coexistence (it's allowed, but confirm the fork's session handling doesn't assume a single client).
- **Input fidelity matters for anti-bot** (see memory `project_container_browser_automation_tells_audit`): CDP `Input.*` events are `isTrusted:true` and fine, but keep human-driven input as-is (real coordinates/timing from the operator's mouse) — do not synthesize.
- Fork changes ship via fork release → image roll → `openclaw-version.json` pin. Backend/frontend ship via the normal pipelines.

---

## Phase 0 — Spike (de-risk before committing). FULLY SPECCED.

**Purpose:** prove CDP screencast + input works end-to-end for ONE target through the real container, and measure latency/quality/CPU vs the current VNC, BEFORE building the production relay. The spike's measurements gate Phases 1-4.

### Task 0.1: Screencast + input spike script (throwaway)

**Files:**
- Create: `docs/superpowers/spikes/cdp-screencast-spike.md` (findings write-up) and a throwaway node script run in a dev container via ECS exec (not committed to app code).

**Interfaces:** none (spike).

- [ ] **Step 1:** In a dev container, connect to the managed Chrome CDP (18800), pick the active page target, call `Page.startScreencast({format:'jpeg', quality:60, maxWidth:1920, maxHeight:1080})`, and count `Page.screencastFrame` events/sec + measure average frame size and end-to-end latency (timestamp in frame vs receipt). Ack each frame (`Page.screencastFrameAck`).
- [ ] **Step 2:** Drive a synthetic `Input.dispatchMouseEvent` click and confirm the page reacts in the frame stream (round-trip works).
- [ ] **Step 3:** Measure CPU on the 1-vCPU box during screencast vs during VNC (compare `top`/cgroup). Record fps, latency, frame size, CPU.
- [ ] **Step 4:** Open a `window.open` popup (a second target) and confirm a second screencast can run concurrently on that target (the multi-panel premise).
- [ ] **Step 5:** Write findings to `docs/superpowers/spikes/cdp-screencast-spike.md`: fps/latency/CPU vs VNC, whether quality/resolution is acceptable, whether multi-target concurrent screencast is stable, and whether a second CDP client (screencast) interferes with the agent's `connectOverCDP` session. **GATE:** if latency/CPU is worse than VNC or multi-client CDP is unstable, STOP and reconsider (WebRTC track vs raw screencast frames) before Phase 1.

---

## Phases 1-4 — OUTLINE (detailed tasks written AFTER Phase 0, informed by its measurements)

> These phases are intentionally an architecture + task outline, not bite-sized TDD steps. Writing full placeholder-free steps now would be fabricated precision — the spike settles frame transport (raw JPEG frames vs WebRTC), ack/backpressure strategy, and resolution handling, which shape every task below. After Phase 0, expand the chosen phase into full TDD tasks in this doc.

### Phase 1 — Backend screencast relay (single target)
- New WS endpoint alongside `browser_view.py` (behind the `bv` door): connects to the container CDP (via the gateway pool / discovered IP), starts `Page.startScreencast` on a requested `targetId`, forwards frames to the frontend, and relays inbound input messages as CDP `Input.*`. Frame acking + backpressure per spike findings.
- Gateway/RPC: reuse `browser.request` for target listing (`GET /tabs`, already used by #1928) so the frontend knows the targets; the frame path is the new WS.
- Keep VNC relay intact and unflagged-on.

### Phase 2 — Frontend screencast panel (single target)
- New panel component rendering `Page.screencastFrame` JPEGs to a `<canvas>`; capture mouse/keyboard on the canvas and send CDP-shaped input (map canvas coords → device coords per the frame's metadata). Resolution set via viewport, not the display.
- Behind a workspace flag; VNC `BrowserDockPanel` remains the default.

### Phase 3 — Multi-target → one dockview panel per target
- Drive panel creation off the `GET /tabs` target list (reuse #1928's `normalizeTabs`/`tabId` alias): each target opens/updates its own dockview panel; a `window.open` popup naturally becomes a new panel — **this is what kills the matchbox trap** (no shared fullscreen display; each target rendered independently). Close/focus via the existing `/tabs` RPCs.
- Panel lifecycle: target created → panel added; target closed → panel removed.

### Phase 4 — Input fidelity, resolution, cutover
- Full keyboard (modifiers, IME), scroll, drag mapping via CDP `Input.*`; DPR/viewport sizing so the panel is crisp.
- Flag flip: screencast becomes default; VNC path (browser_view.py relay + Xvnc/matchbox boot in container-stack.ts) retired only after prod validation. Update `reference_apigw_no_websockets_bv_cloudfront_door` / live-view memories.
- Validate anti-bot posture unchanged (human input stays real; CDP input is `isTrusted`).

## Self-Review

- **Coverage:** Phase 0 de-risks the core unknowns (transport, multi-client CDP, CPU/latency); Phases 1-4 cover relay → single panel → multi-panel → cutover. The multi-panel phase (3) is the one that resolves the trapped-popup problem structurally and delivers the "each window = its own panel" the founder asked for.
- **Honesty flag (per skill's no-placeholder rule):** Phases 1-4 are deliberately outlined, not step-detailed, because a rework this size cannot be honestly bite-sized before the Phase 0 spike settles transport + backpressure + multi-client behavior. Expand the chosen phase into full TDD tasks after the spike — do not execute Phases 1-4 from this outline as-is.
- **Interim already shipped:** #1928 (tab-list + Close) is the cheap unblock; this plan is the substantive fix and can proceed at a deliberate pace.
