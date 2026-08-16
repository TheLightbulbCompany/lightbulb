# Container Browser Live View — Design

**Date:** 2026-08-16
**Status:** Proposed — not approved, not started
**Driver:** Grok Bot parity (persistent cloud computer with a watchable, drivable browser),
plus the standing "agent hands me a `localhost:3000` link that goes nowhere" bug.

---

## 1. Goal

Let an owner **watch and drive** the Chromium running inside their own per-user
OpenClaw container, from the Isol8 web UI, with real mouse and keyboard — not a
screenshot feed.

Interaction is in scope **from the first ship**, not a follow-up phase. The
capability only pays for itself when a human can take over a step the agent
cannot finish alone: a CAPTCHA, a 2FA prompt, an OAuth consent screen where the
user must type their own password, or a final confirm click.

### Non-goals

- Multi-window desktop. Xvfb with no window manager gives one full-screen
  Chromium. Adding a WM is a later decision, not part of this.
- Recording / session replay.
- Any change to the `user` browser profile (the desktop-node path that drives
  the owner's real Chrome). This spec covers the `openclaw` profile — the
  container's own managed Chromium — only.
- Learning-from-demonstration (the third Grok capability). Separate spec.

### Why this also closes the localhost bug

The container browser shares the container's network namespace, so
`http://localhost:3000` resolves **correctly** inside it. The fix for dead
localhost links was never "give the user a reachable URL" — it is "stop handing
the user a URL." The agent opens it in its own browser and the owner watches.

No preview proxy, no wildcard subdomain, no port-forwarding scheme. An earlier
draft of this work proposed exactly that; it is dropped.

---

## 2. Evidence base

Every load-bearing claim below, with its citation. Anything not in this table is
an assumption and is marked as such in §7.

| # | Claim | Evidence |
|---|---|---|
| E1 | OpenClaw ships a complete headful-browser + live-view recipe | `~/Desktop/agent/scripts/docker/sandbox/Dockerfile.browser`, `~/Desktop/agent/scripts/sandbox-browser-entrypoint.sh` |
| E2 | That recipe is `Xvfb :1 -screen 0 1280x800x24` → headful Chromium on that display → `x11vnc -display :1 -rfbport N -localhost` → `websockify --web /usr/share/novnc/ 6080 localhost:5900` | `sandbox-browser-entrypoint.sh`, the `Xvfb`, `x11vnc`, and `websockify` invocations |
| E3 | Its apt set is `chromium novnc python3 websockify x11vnc xvfb` + fonts, on `debian:bookworm-slim` | `Dockerfile.browser` |
| E4 | OpenClaw's sandbox-browser feature is hard-wired to Docker — it creates a *sibling container* | `src/agents/sandbox/browser.ts` — `execDocker`, `ensureDockerNetwork`, `docker network create`, `args.push("-p", "127.0.0.1::" + noVncPort)` |
| E5 | Upstream explicitly endorses Xvfb as the supported Linux path | `extensions/browser/src/browser/chrome.ts:739` — error text: *"No DISPLAY/X server was detected. Set OPENCLAW_BROWSER_HEADLESS=1, remove the headed override, **start Xvfb**, or run the Gateway in a desktop session."* |
| E6 | Headless resolution precedence: request override → `OPENCLAW_BROWSER_HEADLESS` env → explicit profile source → **linux-no-DISPLAY fallback** → config `browser.headless` | `extensions/browser/src/browser/config.ts:701-731` |
| E7 | The backend may already reach **every TCP port** on any per-user container | `apps/infra/lib/stacks/service-stack.ts:223-230` — `ContainerFromServiceIngress`, `fromPort: 0, toPort: 65535`, source = backend service SG |
| E8 | Only port 18789 is mapped today, and it speaks the gateway WS protocol | `apps/infra/lib/stacks/container-stack.ts:799` |
| E9 | The task already runs a shell startup command we can extend | `container-stack.ts:588-702` (`startupCommand`), wired at `:717` as `command: ["sh", "-c", startupCommand]` |
| E10 | `startupCommand` **ends in `exec`** — nothing can be appended after it | `container-stack.ts:701` — `exec /usr/local/bin/isol8-local-sqlite-boot.sh` |
| E11 | `HOME=/home/node` is load-bearing: `.npm`, `.mcporter`, `.xurl`, `.openclaw/browser` are all symlinks hanging off it | `container-stack.ts:592-669`, env block at `:721` |
| E12 | `XDG_CONFIG_HOME` points at **EFS** | `container-stack.ts` env — `XDG_CONFIG_HOME: "/home/node/.openclaw/.config"` |
| E13 | Container is 1 vCPU / 4 GB | `container-stack.ts:520` — `PER_USER_TASK_RESOURCES = { cpu: 1024, memoryMiB: 4096 }` |
| E14 | Current browser config is headless, profile `openclaw`, CDP 18800 | `apps/backend/core/containers/config.py:1903-2013` |
| E15 | Container IP must be resolved per-request via Cloud Map, never cached | `CLAUDE.md` ("Never cache a container IP across reconnects" — a cached dead IP caused a prod outage); `ecs_manager.discover_ip` |
| E16 | Backend already holds `operator.admin` on pooled gateway connections | `apps/backend/core/crypto/operator_device.py:85-89` |
| E17 | The base image is `node:24-bookworm-slim`; our extended image is built on it and already has apt layers | fork `Dockerfile:192`; `apps/infra/openclaw/Dockerfile:84, 22, 101` |
| E18 | Xvfb is **not** guaranteed present in the base — it sits behind the fork's `OPENCLAW_INSTALL_BROWSER` build arg, and our extended image installs its own Chromium instead | fork `Dockerfile:283-297`; `apps/infra/openclaw/Dockerfile:226, 257-261` |
| E19 | Workspace panels are a fixed registry of four | `apps/frontend/src/components/workspace/panelComponents.ts` — `chat`, `teams`, `control`, `file-viewer` |
| E20 | **Step 0 passed.** Chromium launches cleanly in a live prod container and CDP answers | measured 2026-08-16 via `ecs execute-command` on prod task `0459161c`: `/json/version` → `Chrome/149.0.7827.55`, `webSocketDebuggerUrl` present |
| E21 | `.config` is `node:node` (uid 1000) on a live prod container — the reported permission blocker is gone | same session: `drwxr-xr-x. 2 node node … /home/node/.openclaw/.config` |
| E22 | Per-user containers idle at ~4-6% CPU but **peak at 45-89% of the single vCPU hourly**, on both a user and an org container | CloudWatch `AWS/ECS` `CPUUtilization`, 1h periods, 2026-08-15→16, services `openclaw-user_3Ht8A88…` and `openclaw-org_3H3viGAf…` |
| E23 | Memory is not the constraint: ~14% average, 24% peak of 4 GB (≈560 MB / ≈950 MB) | CloudWatch `AWS/ECS` `MemoryUtilization`, same window and service |

---

## 3. What we take from upstream, and what we deliberately do not

### Take: the process recipe (E2, E3)

Xvfb + x11vnc + noVNC/websockify is the industry-standard stack for exactly this
and is what upstream already validated against OpenClaw's own Chromium. We copy
the package list and the process topology.

### Do not take: the Docker sandbox subsystem (E4)

`src/agents/sandbox/` spawns a sibling Docker container. **ECS Fargate has no
Docker daemon and no privileged mode.** OpenClaw's sandbox-browser feature is
therefore unusable for us as a feature. This is the single biggest thing to
understand about this design: we are reusing a *recipe*, not enabling a
*feature*. No `agents.defaults.sandbox` config, no `openclaw-sandbox-browser`
image, no `/sandbox/novnc` route.

### Do not take: the CDP relay

`sandbox-browser-entrypoint.sh` contains a ~120-line Python TCP relay that
fronts Chrome's loopback-bound CDP with Basic/Bearer auth and a CIDR allowlist.
It exists **only** because upstream's gateway and browser live in different
containers, so CDP has to cross a network boundary.

In our architecture the gateway and Chromium are in the **same** container.
OpenClaw keeps talking to Chrome over loopback on 18800 exactly as it does today
(E14). We need no off-loopback CDP, so we ship no relay and open no CDP port.
This removes the entire authentication surface that relay exists to protect.

### Do not take: `HOME=/tmp/openclaw-home`

Upstream's entrypoint relocates `HOME` and the XDG dirs to `/tmp`, off any
network filesystem. **We cannot do this** — `HOME=/home/node` anchors the
`.npm`, `.mcporter`, `.xurl`, and `.openclaw/browser` symlink relocations that
the startup command builds (E11). Changing it would break MCP caching, the X
CLI token store, and the mcporter OAuth vault at once.

> An earlier verbal version of this design claimed that adopting upstream's
> entrypoint would move XDG off EFS "for free" and thereby sidestep the Chromium
> launch bug. **That claim is withdrawn.** We keep `XDG_CONFIG_HOME` on EFS
> (E12), so the launch question stays live and is handled explicitly in §4.0 and
> §7.1 instead of being assumed away.

---

## 4. Design

### 4.0 Step 0 — prove Chromium launches at all — ✅ PASSED 2026-08-16

**Nothing in this spec is worth building if the container's Chromium does not
start.** This step ships no code. It was an empirical check on a live container,
and it gated everything after it.

**Result: passed** (E20, E21). On prod task `0459161c`, `.config` is
`node:node` and Chromium launched clean on the first attempt, with CDP
answering `Chrome/149.0.7827.55`. The reported launch blocker is not present on
current containers. The test process was killed afterward.

This does **not** retroactively prove the crashpad diagnosis in §7.1 was the
correct root cause — only that whatever the cause was, the symptom is gone on
containers provisioned under the current code. That distinction still matters if
it ever regresses. Original procedure retained below for that case.

```
aws ecs execute-command --cluster <cluster> --task <task> --container openclaw --interactive \
  --command "/bin/sh"
# then, in short separate execs (SSM drops the session on long output):
ls -ld /home/node/.openclaw/.config
/usr/bin/chromium --headless=new --remote-debugging-port=19999 about:blank
```

Run the binary by hand and read **Chrome's own stderr**. Every tool-layer log
for this failure historically reduced to a generic `browser failed: timed out`,
which is why it stayed unsolved through three shipped fixes.

Exit criteria: Chromium stays up and CDP answers `/json/version`. If it does
not, fix that first and do not proceed — see §7.1.

### 4.1 Image (`apps/infra/openclaw/Dockerfile`)

One apt layer, alongside the existing ones (E17):

```
xvfb x11vnc novnc websockify python3
```

Chromium and the fonts are already present (`Dockerfile:257-261`). Xvfb is not
assumed present (E18).

Plus one baked script, `/usr/local/bin/isol8-display-boot.sh`, holding the
display stack. Baking it rather than inlining into `startupCommand` keeps the
CDK string readable and gives the boot chain a version-pinned guard of the same
shape as the existing `isol8-local-sqlite-boot.sh` check (`container-stack.ts:700`).

### 4.2 Startup (`container-stack.ts` `startupCommand`)

Insert **before** the terminal `exec` line (E10 — nothing may follow it):

```sh
/usr/local/bin/isol8-display-boot.sh || exit 1
```

**The three processes split across two lifetimes**, and the measured CPU
numbers (E22) are why — see §7.2. Only the display starts at boot:

| process | when | bind | why |
|---|---|---|---|
| `Xvfb :1 -screen 0 1280x800x24 -ac -nolisten tcp` | **boot** | no TCP | Chrome must have a display to attach to *at launch*; an X server that nobody is drawing on costs almost nothing idle |
| `x11vnc -display :1 -rfbport 5900 -shared -forever -rfbauth <pwfile> -localhost` | **on demand** | loopback only | polls the framebuffer for damage — the expensive one; `-localhost` means only websockify can reach it |
| `websockify --web /usr/share/novnc/ 0.0.0.0:6080 localhost:5900` | **on demand** | container IP | the only port the backend talks to; bind pinned explicitly rather than relying on the default |

Xvfb is backgrounded from the boot script, so it survives the shell being
replaced by the subsequent `exec` — it is a separate process, not a shell job.

x11vnc and websockify are started when the owner **opens the panel** and stopped
when they close it (or on an idle timeout). Steady-state cost of the feature is
then zero for the overwhelming majority of containers, which nobody is watching
at any given moment. Start/stop is driven over the existing gateway RPC — the
backend already holds `operator.admin` on every pooled connection (E16), so this
needs no new control channel.

This is a deliberate asymmetry, not an oversight: the display must be eager
because Chrome's headless-vs-headful decision is made once at launch, while the
capture stack can be lazy because nothing depends on it until a human looks.

The VNC password is generated per boot and never leaves the container. It is a
defence-in-depth layer behind the real fence (§6), not the fence itself.

### 4.3 Config (`apps/backend/core/containers/config.py`)

Two coupled changes, and **both are required** (E6):

- env `DISPLAY=:1` on the task definition
- `browser.headless: True` → `False`

Precedence is why: the linux-no-DISPLAY fallback is evaluated *before* the
config value, so `headless: false` alone is silently overridden to headless when
`DISPLAY` is unset. Conversely, `DISPLAY` alone leaves `headless: true` winning
at the `default` source. Setting one without the other is a no-op in one
direction and a broken launch in the other.

`noSandbox` is already `True` and stays. `cdpPort: 18800` is unchanged and stays
loopback.

`config.py:1936-1945` currently carries a comment warning that a future image
setting `DISPLAY` would "silently launch a headful Chrome with no X server."
That comment must be rewritten in the same PR — we are now setting `DISPLAY`
*and* providing the X server, deliberately. Leaving it would make the file lie.

### 4.4 Backend — one proxy route

An owner-scoped, Clerk-authenticated HTTP + WebSocket passthrough to
`http://{container_ip}:6080/…`.

- The container IP is resolved **per request** via Cloud Map `discover_ip`, and
  never cached (E15). This is not a style preference — a cached dead IP has
  already caused one production outage.
- `routers/paperclip_proxy.py` is the pattern to follow: long-lived
  `httpx.AsyncClient`, header filtering, and it already handles the WebSocket
  case. Its route-filter security model does **not** apply here and must not be
  copied wholesale — see `docs/audit-2026-05-02-paperclip-routes.md`.
- Trial containers scale to zero when idle. A request against a sleeping
  container must return a structured "asleep" state the panel can render, not a
  timeout.

### 4.5 Frontend — a fifth panel

A new `browser` entry in `PANEL_COMPONENTS` / `PANEL_COMPONENT_KEYS` (E19),
rendering the proxied noVNC client in an iframe. noVNC is a real VNC client in
the browser: watching, clicking, typing and scrolling all work through it with
no additional input plumbing on our side.

States the panel must render: container asleep, browser not started, connected,
disconnected.

---

## 5. Why not CDP screencast instead

The alternative — `Page.startScreencast` for frames plus `Input.dispatchMouseEvent`
for input — is what Browserbase and browser-use ship, and it avoids the X stack
entirely.

Rejected because Chrome's CDP binds to `127.0.0.1` (`chrome.ts:798` hardcodes
`http://127.0.0.1:${cdpPort}`), so reaching it from the backend requires either
changing Chrome's bind address or reintroducing exactly the authenticated relay
we deleted in §3. That is *more* moving parts than Xvfb + x11vnc, and it buys a
browser-only viewport where Xvfb gives us a display we can grow into. If the
resource cost in §7.2 proves fatal, this is the fallback to reconsider.

---

## 6. Security posture — built in from the first commit

- **Port 6080 is never publicly routable.** It is not added to
  `portMappings` (E8), so it is not registered anywhere; the container SG
  already admits only the backend service SG (E7).
- **The only door is the authed proxy** (§4.4): Clerk session → owner scope →
  that owner's container only. Owner-scoping is the whole fence; a route that
  takes a container id from the client is a cross-tenant hole.
- **x11vnc stays `-localhost`**, so even inside the container only websockify
  can reach the VNC port.
- **No CDP is exposed** (§3). Chrome's debugging port stays loopback.
- Anyone who can open this panel can drive a real browser that holds the owner's
  logged-in sessions. That is the intended product, and it is the same trust
  level as the existing chat surface — but it means the auth check is the
  feature, not a wrapper around it.

---

## 7. Open risks

### 7.1 The Chromium launch diagnosis is contested — and this design does not rely on it

The record says the container's Chromium never started because
`XDG_CONFIG_HOME` was root-owned 755 on EFS while the access point squashes root
to uid 1000, so Chrome could not create its crashpad directory and dumped core
~26ms into launch. The claim cites an in-container A/B on 2026-08-04 in which
the same argv against a *writable* directory on the *same* EFS mount started
fine, and `ensure_xdg_dirs` (`workspace.py:201-238`) shipped as the fix, called
during provisioning at `ecs_manager.py:3596`.

**Treat this as unconfirmed.** Reasons for doubt worth stating plainly:

- The evidence is a docstring and a memory note recording a manual session, not
  a reproducible test. `test_workspace.py:455` tests that `ensure_xdg_dirs`
  chowns — it does not test that Chromium then launches.
- The same investigation produced two previously-retracted root causes and three
  shipped fixes that did not work, so its track record on this specific bug is
  poor.
- `ensure_xdg_dirs` runs at provision time, so any container provisioned before
  it shipped has never had the fix applied regardless of whether it is correct.

Handling: §4.0 made this an **empirical gate**, not an assumption — and the gate
**passed** (E20, E21). `.config` is `node:node` on a live prod container and
Chromium launches clean. So the design proceeds on measured fact rather than on
the diagnosis being right, which was the entire point of gating it.

The diagnosis itself remains unproven and should stay written that way: a
correct prediction is not a proof of mechanism, and the fix and the observation
are separated by a fleet-wide reprovision that could have changed other things.
If the symptom ever returns, re-run §4.0 rather than trusting this section.

If it does fail in future, the fallback is to point Chrome's XDG at
container-local storage — note that this is *narrower* than upstream's `HOME=/tmp` move, which
we cannot do (§3), and it must be scoped to the browser without disturbing gog's
keyring, which genuinely needs EFS (`container-stack.ts` env comment).

Do **not** re-add `--disable-breakpad` / `--crash-dumps-dir`; those were tested
in-container and produced a byte-identical failure (`config.py:1954-1966`).

### 7.2 Resource cost — measured, and it splits cleanly

Measured on two live prod containers (a personal one and an org one) over
2026-08-15→16:

- **Memory is a non-issue.** ~14% average, 24% peak of 4 GB — roughly 560 MB
  used against 3+ GB free (E23). Headful Chrome, Xvfb, x11vnc and websockify
  together add on the order of 400-700 MB. Comfortable.
- **CPU is the real constraint.** Containers idle at ~4-6% of their single vCPU
  but **peak at 45-89% hourly, already, today** (E22) — on both containers, with
  no display stack running at all. One org container averaged 24% for a full
  hour with an 89% peak.

So the headroom at peak is thin before we add anything, and the cost profile of
the three processes is very uneven: an idle Xvfb is nearly free, while x11vnc
polls the framebuffer for damage and is the genuine CPU consumer, proportional
to how much the screen is changing.

**That asymmetry is what §4.2's split buys.** Running the capture stack only
while a human has the panel open confines the expensive part to exactly the
window where someone is watching — and during which the agent is typically
*waiting on that human* rather than competing for CPU. Steady state for every
unwatched container is unchanged from today.

Carry across upstream's cost flags regardless: `--renderer-process-limit=2`,
`--disable-gpu`, `--disable-software-rasterizer`, `--disable-dev-shm-usage`.
Our config already caps tabs aggressively (`config.py:1920` — 4 tabs, 30-minute
idle) for the same reason.

**One adjacent failure mode, stated at its actual scope.** CPU starvation on
this container class has produced SQLite `database is locked` rather than plain
slowness — `ecs_manager.py:438-443` records a prod owner with 11 agents
crash-looping on boot 2026-08-14, where 24 SQLite databases restoring and
replicating concurrently saturated the single vCPU (the gateway got 13% of a
core), blew OpenClaw's hardcoded 5s busy timeout, and tripped the ECS deployment
circuit breaker after three failures.

**That was a boot-time restore storm, and it is not evidence that a browser
will do the same.** The load shapes differ: a burst of concurrent synchronous
SQLite I/O at startup versus sustained rendering load well after boot, with no
DB contention implied. It is cited here as the reason to *watch* CPU on this
container class rather than assume slowness is the worst outcome — not as a
prediction about the live view. Treat any claim that the live view will trigger
it as unproven until §8 measures it.

**Remaining open decision:** whether a *watched* container still fits in 1 vCPU.
The split makes this plausible but does not prove it. Measure during §8 with a
real browsing session under live view. If it does not fit, the bump to 2 vCPU is
**a pricing decision, not a config one** — container size is the billing plan.
This spec does not decide that.

### 7.3 The display stack is unsupervised — known ceiling

Upstream's entrypoint ends in `wait -n` and exits when any child dies, letting
Docker restart the whole container. **We cannot do that**: our PID 1 is the
local-sqlite boot chain, and the display processes are backgrounded siblings of
it (§4.2). If Xvfb or websockify dies, nothing restarts it, and the failure is
silent — the agent's browser keeps working (Chrome holds its X connection) while
the live view goes dark, or Chrome dies with it and the agent loses the browser.

Accepted for v1: the panel surfaces a disconnected state, and the container
health check already restarts the task on gateway failure. If this proves flaky
in dev, the upgrade is a small supervisor (or `s6`/`tini`-style process manager)
rather than restart logic scattered through the boot script.

### 7.4 Blast radius of `headless: false`

This flips a fleet-wide default that every container inherits. The rollback is
cheap and does not require a config rewrite: `OPENCLAW_BROWSER_HEADLESS=1` as a
task-definition env var takes precedence over the config value (E6, `env`
source beats `default` source), so a single task-def change restores headless
behaviour fleet-wide.

That rollback lever must be verified working *before* the config flip ships, not
after.

---

## 8. Verification

- **Step 0** (§4.0): Chromium launches, CDP answers, on a live container.
- **Display stack**: `xdpyinfo -display :1` succeeds; websockify answers on 6080
  from the backend task; x11vnc refuses a non-loopback connection.
- **Headless resolution**: a unit test asserting that `DISPLAY=:1` +
  `headless: false` resolves to headed, and that `OPENCLAW_BROWSER_HEADLESS=1`
  overrides it back — this pins E6's precedence chain, which the whole config
  change depends on.
- **Proxy**: an owner cannot reach another owner's container. This is the one
  test that must exist before the route is merged.
- **End to end**: agent starts a dev server on :3000, opens `localhost:3000` in
  its own browser, owner sees the page in the panel and clicks a button in it.
  That is the acceptance criterion, and it is also the demo.
- **Resource**: task CPU/memory under a live browsing session, against the
  1 vCPU / 4 GB ceiling.

---

## 9. Sequencing

1. ~~Step 0 gate (§4.0)~~ — **done 2026-08-16, passed.**
2. Image + display boot script (§4.1, §4.2), behind no flag; harmless while
   `headless` is still `true`, since nothing connects to the display.
3. Config flip + the `config.py` comment rewrite (§4.3), with the rollback lever
   verified first.
4. Backend proxy (§4.4) with the cross-tenant test.
5. Frontend panel (§4.5).

Steps 2–5 are separate PRs. Step 3 is the one that changes fleet behaviour, and
it should sit in dev long enough to read the resource numbers before prod.
