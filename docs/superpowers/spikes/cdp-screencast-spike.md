# Spike: CDP `Page.startScreencast` live-view vs the current VNC-of-X-display

**Date:** 2026-08-20
**Companion plan:** `docs/superpowers/plans/2026-08-20-cdp-screencast-liveview.md` (Phase 0)
**Question that started this:** the founder reported the container browser live-view is "laggy and has bad resolution," and asked whether we should match what cloud-browser vendors actually do rather than keep polishing VNC.

## TL;DR verdict

**GREEN on direction, with one honest caveat.** Per-target CDP `Page.startScreencast` is the industry standard for interactive cloud-browser live view (proven in two open-source vendors), and it structurally fixes all three of the user's complaints — resolution, lag, and the trapped-popup problem — in ways VNC-of-an-X-display cannot. The one caveat: **the live fps/latency/CPU numbers the Phase-0 gate asks for could not be captured on the founder's prod container**, because ECS `execute-command` sessions on that box are cut after ~2–4 s (shorter than a Chrome cold-boot) and detached processes are reaped on session teardown. The go verdict therefore rests on primary-source vendor evidence + a code-grounded architectural analysis + the VNC baseline I *did* measure — not on a live screencast benchmark. **Recommendation: build Phase 1 (single-target relay, behind a flag) as the smallest next slice and capture the real numbers in dev, where a browser viewer makes the measurement trivial.** Do **not** build the full 4-phase rework yet.

**Second-CDP-client question (the ponytail crux): a screencast relay is inherently a second CDP consumer, but it does NOT disturb the agent's `connectOverCDP` session.** Evidence below.

---

## 1. What the vendors actually use (primary sources)

| Vendor | Transport | Open source? | Evidence |
|---|---|---|---|
| **Steel Browser** | **CDP `Page.startScreencast` (JPEG) over WS + `Input.dispatchMouseEvent`/`dispatchKeyEvent`** back | **Yes — code confirms** | `api/src/plugins/browser-socket/casting.handler.ts` calls `Page.startScreencast({format:"jpeg", quality:75, maxWidth, maxHeight})`, forwards each `Page.screencastFrame` `data` over `ws.send(...)`, dispatches inbound input via `Input.*`. |
| **Browserless.io** | **CDP-based `Browserless.liveURL` → same-origin WS streaming frames + input** (iframe-embeddable) | **Yes (SSPL) — code + docs** | Docs: the LiveURL "upgrades a same-origin WebSocket to stream frames and input"; "click, type, and scroll in it directly." `interactable:false` = view-only. `makeLiveURL()` in `src/browsers/browsers.playwright.ts`. |
| **Browserbase** | Interactive iframe live view via `debuggerUrl`/`debuggerFullscreenUrl` — **CDP/DevTools-based (inferred from "debugger" naming)** | No (docs only) | "watch, click, type, scroll in real-time"; read-only or read/write iframe modes. |
| **Anchor Browser** | `live_view_url` in an iframe; disable input with `pointer-events:none` — **transport undisclosed; headful-desktop model leans VNC-like** | No (docs only) | Mechanism not named. |
| **Hyperbrowser** | `liveUrl` real-time stream; `viewOnlyLiveView` toggle — **transport undisclosed** | No (docs only) | — |

**Bottom line:** the source-verifiable industry standard is **CDP `Page.startScreencast` (JPEG/WebP) frames relayed over a WebSocket, with user input echoed back via CDP `Input.*`.** WebRTC media tracks are **not** the norm for these headless-Chrome-in-cloud vendors. **VNC-of-an-X-display shows up mainly with vendors that run full *headful desktop* Chrome in a container — which is exactly Isol8's current approach.**

Sources: `github.com/steel-dev/steel-browser`; `docs.browserless.io/baas/monitor-sessions/hybrid-automation`; `docs.browserbase.com/features/session-live-view`; `docs.anchorbrowser.io/advanced/browser-live-view`; `hyperbrowser.ai/docs/sessions/live-view`.

---

## 2. Current VNC baseline (measured on the founder container)

Founder container `org_3H3viGAfq2SXc9dIdcatmGXg8Td`, prod, via ECS exec.

- **Display:** `Xvnc :1 -geometry 1280x800 -depth 24 -localhost -SecurityTypes None -rfbport 5900`, matchbox WM, `websockify --web /usr/share/novnc 0.0.0.0:6080 → 127.0.0.1:5900`. Software GL (llvmpipe). This box is **Ultra-sized: `nproc = 2`** (founder is over the agent-count floor) — **typical users are 1 vCPU**, so everything below is *better* than what most users see.
- **`resizeSession` works, and it is the resolution problem.** The panel drives `SetDesktopSize` to the panel's **CSS-pixel** size. I caught the live display at **768×388** and at **1920×1080** on separate connects — i.e. the framebuffer tracks the panel's CSS px, then noVNC/`scaleViewport` upscales it to the physical panel. On a Retina panel a 768×388 framebuffer upscaled to 2× device pixels is exactly the "bad resolution" the founder reported. VNC has no clean device-pixel-ratio path here.
- **Encode-CPU model:** Xvnc encodes the whole framebuffer on each client `FramebufferUpdateRequest`. Idle Xvnc read ~0.1% CPU only because nothing was connected; under a live viewer on 1 vCPU, full-framebuffer Tight/JPEG encode competes with Chrome's own rendering on the single core — the "laggy" report.

I could **not** capture Xvnc encode-CPU-under-load numbers: doing so needs a persistent connected viewer, which runs into the same ECS-exec session limit described in §4.

---

## 3. Container CDP architecture (measured + fork code)

- **Managed Chrome launches with `--remote-debugging-port=<cdpPort>` bound to `127.0.0.1` only** (fork `extensions/browser/src/browser/chrome.ts:818`). It **idle-closes when unused** (lazy) — during the spike Chrome was not running, which is normal.
- **`openclaw-gateway` (PID 310) owns the browser surface.** It listens on `18789` (gateway/healthz) and `18799` (the **extension-relay** CDP endpoint, HTTP Basic auth `openclaw:<OPENCLAW_BROWSER_CDP_AUTH_TOKEN>`, realm `openclaw-extension-relay`). The raw managed-Chrome debug port has **no** auth (loopback-trusted). Other listeners: `6080` websockify, `9090` litestream.
- **CDP connection model (fork code):** OpenClaw connects via `chromium.connectOverCDP(url, {headers})` (`pw-session.ts:1333`) and opens per-page sessions with `page.context().newCDPSession(page)` (`pw-session.page-cdp.ts:20`) + `Target.attachToTarget {flatten:true}` (`pw-session.ts:2113`).

**Infra finding for Phase 1 (the plan's outline glosses this):** the managed Chrome debug port is **loopback-only**, unlike websockify which is deliberately `0.0.0.0:6080` so the backend SG can reach it. A screencast relay must therefore either **expose the CDP port to the backend SG** (a new container port mapping + SG ingress rule, mirroring the 6080 pattern in `container-stack.ts` / `service-stack.ts`) **or proxy CDP through the gateway** (it already exposes `browser.request` for `/tabs`, `/start`). This is a real, concrete design decision Phase 1 must make up front.

---

## 4. Why the live screencast benchmark is missing (honest gap)

Phase 0 asks for fps / latency / frame-size / CPU of `Page.startScreencast` on the box. I wrote a full asyncio CDP spike (launch isolated headless Chrome on a private port/profile — cannot touch the founder's gateway or agent — then `Page.startScreencast`, drive an animation for motion, measure the Chrome process-tree CPU via `/proc`, do an `Input.dispatchMouseEvent` round-trip, and a multi-client coexistence test). It **could not run to completion** because:

- **ECS `execute-command` sessions on this container are cut after ~2–4 s** ("Cannot perform start session: EOF"), which is **shorter than a Chrome cold-boot (~3–5 s)**. Fast probes (<2 s) succeeded; anything holding a session open for a measurement window did not.
- **Detached processes are reaped on session teardown.** A `nohup setsid google-chrome …` launched fine (chrome logged `XNNPACK delegate` init, so headless Chrome *does* work on the box with `--no-sandbox --disable-dev-shm-usage`) but was gone by the next poll; `warm.log` was empty on a clean run — Chrome never survived to be connected to.

This is an **environment/tooling limitation, not a screencast limitation** — the correct place to get these numbers is dev, under a real browser viewer, which Phase 1 provides for free. The partial evidence that *did* land: headless Chrome initializes and serves `/json/version` on this exact hardware.

---

## 5. Second CDP client — do we actually need one, and does it disturb the agent?

**Yes, a screencast relay is inherently a second CDP consumer** — there is no way to get `Page.screencastFrame` without a CDP session. The real question is whether it disturbs the agent's `connectOverCDP` automation session. **It does not**, on three grounds:

1. **CDP is multi-client by design.** Chrome's DevTools endpoint accepts multiple simultaneous WebSocket clients; `connectOverCDP` does not claim exclusivity.
2. **`Page.startScreencast` is per-session.** Frames are delivered to the session that called it; it does not globally mutate the page in a way that blocks another session's `Input.*` / `Page.navigate` / `Runtime.evaluate`.
3. **OpenClaw already multiplexes sessions.** It uses `newCDPSession` + `Target.attachToTarget{flatten:true}` — an additional independent session (the screencast consumer) coexists with these exactly as CDP intends.

**Discipline the screencast client must keep:** attach to the specific page target, call `startScreencast` + dispatch `Input.*`, and **nothing browser-wide or exclusive** — no `Target.setAutoAttach` hijack, no `Browser.close`, no closing the agent's targets. Human-driven input stays real (real coordinates/timing); CDP `Input.*` events are `isTrusted:true`, so anti-bot posture is unchanged (see memory `project_container_browser_automation_tells_audit`).

*Not live-verified here* (same ECS-exec blocker); this rests on documented CDP semantics + the fork's own session handling. Phase 1 in dev should confirm coexistence with a running agent as its first check.

---

## 6. Why screencast beats VNC here (independent of the missing fps number)

- **CPU (the 1-vCPU win):** screencast is **change-driven** — `Page.screencastFrame` fires only on visual change, so a browser that is static between agent actions (the common case) costs ~0 encode CPU. VNC continuously re-encodes the framebuffer per client update request. On 1 vCPU this is the decisive difference behind "laggy."
- **Resolution:** screencast resolution follows the page **viewport / `Page.setDeviceMetricsOverride`**, and can be set to `panel × devicePixelRatio` for a crisp Retina image — vs the measured fixed X-display downscaled to panel CSS px (§2).
- **Trapped popups:** per-target rendering means each tab/window is its own panel; a `window.open` popup naturally becomes its own panel instead of matchbox-fullscreening over the page. This **structurally** fixes the trap that #1928 (tab-list + Close) only band-aided.

---

## 7. Recommendation (smallest next slice)

Proceed to **Phase 1 only**: a single-target screencast relay behind a flag, VNC kept intact and default. Ponytail applies — this is the smallest slice that both matches industry practice and *produces the fps/CPU/latency numbers this spike couldn't*, under a real viewer. Do **not** expand to the 4-phase rework until Phase 1's dev numbers confirm the win.

Concrete inputs Phase 1 must settle (from this spike):
1. **CDP reachability** — expose the loopback debug port to the backend SG (mirror 6080) *or* relay through the gateway (§3). Decide before writing the relay.
2. **Reuse the `bv-{env}` CloudFront VPC-origin door** for the frame WS (the sanctioned raw-WS path; API Gateway can't carry it).
3. **Frame acking/backpressure** — ack each `Page.screencastFrame`; on a slow client, drop/coalesce rather than unbounded-buffer.
4. **Coexistence check first** — with a real agent running, confirm a screencast session does not perturb its automation (§5).

**#1874 contract (peer session, auto-surface Browser panel):** when Phase 2 swaps the panel component, it **must** keep `browser` as the **parameterless singleton** panel key in `panelRegistry.ts`/`panelComponents.ts`, and keep the **mount-time `browser.request POST /start`** call (`openPanel("browser")` relies on it to ensure Chrome). The new screencast panel registers under the same key and keeps that mount effect. This spike touches no panel code, so there is no conflict today — flagged for Phase 2.
