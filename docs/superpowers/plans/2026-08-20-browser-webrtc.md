# Browser WebRTC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task.

**Goal:** Replace the Browser panel's VNC/CDP visual transports with a smooth, full-display WebRTC experience while keeping OpenClaw as the sole browser-control authority.

**Architecture:** Neko v3.1.5 attaches to the owner container's existing X display and PulseAudio sink. Isol8's Clerk-authenticated backend proxies only Neko static/signaling traffic through the existing bv CloudFront door; encrypted WebRTC media/input connects directly to the owner task's public ICE mux. The frontend embeds Neko's video-only client, drives bounded display resize, and leaves browser lifecycle/tabs/navigation to browser.request and native Chrome.

**Tech Stack:** Next.js 16/React 19, FastAPI/httpx/websockets, AWS CDK/ECS Fargate/CloudFront, Neko 3.1.5/Pion, X11/Openbox, GStreamer VP8/Opus, PulseAudio, Vitest/pytest.

**Spec:** docs/superpowers/specs/2026-08-20-browser-webrtc-design.md

## Global constraints

- Work only in /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/browser-webrtc on feat/browser-webrtc, based on current origin/main.
- OpenClaw owns Chrome, CDP, tabs, navigation, and browser lifecycle. Never add a second browser controller or Isol8 tab model.
- Preserve useBrowserToolAutoOpen, browser.request POST /start, and core/services/browser_state.py behavior.
- Keep VNC as a temporary flag-selected rollback until the production observation gate passes; then delete VNC and CDP screencast completely.
- Keep one conservative stream. Do not add simulcast, a resolution ladder, 60 fps, 1080p, TURN, webcam, microphone, or a custom Neko client.
- No production mutation from the implementation branch. Image, backend, frontend, and infrastructure changes deploy only through version control and CI/CD.
- After every push, watch the triggered GitHub Actions runs to completion. Vercel previews are reviewed through their normal deployment status.
- Use tests first for every logic/security change. Do not weaken a failing dependent test because it is outside the immediate file.
- Planning docs are disposable. Task 10 deletes this plan and spec after the final cleanup has shipped.

## Task 1: Commit the approved design and implementation contract

**Files:**

- Add: docs/superpowers/specs/2026-08-20-browser-webrtc-design.md
- Add: docs/superpowers/plans/2026-08-20-browser-webrtc.md

- [ ] Verify the spec explicitly covers authority, auth, display/audio/input, direct ICE, CPU ceilings, rollout, rollback, and final deletion.

Run:

~~~bash
rg -n "OpenClaw|Neko|WebRTC|audio|CPU|rollback|Delete" \
  docs/superpowers/specs/2026-08-20-browser-webrtc-design.md \
  docs/superpowers/plans/2026-08-20-browser-webrtc.md
~~~

Expected: every listed concern appears in the design and executable plan where applicable.

- [ ] Scan both docs for unfinished markers.

~~~bash
rg -n "[T]ODO|[T]BD|[F]IXME|[f]ill in" \
  docs/superpowers/specs/2026-08-20-browser-webrtc-design.md \
  docs/superpowers/plans/2026-08-20-browser-webrtc.md
~~~

Expected: no matches.

- [ ] Commit the documentation foundation.

~~~bash
git add docs/superpowers/specs/2026-08-20-browser-webrtc-design.md docs/superpowers/plans/2026-08-20-browser-webrtc.md
git commit -m "docs(browser): design full-display WebRTC migration"
~~~

## Task 2: Add the pinned Neko runtime without changing the selected visual transport

**Files:**

- Modify: apps/infra/openclaw/Dockerfile
- Add: apps/infra/openclaw/browser/neko.yaml
- Add: apps/infra/openclaw/browser/default.pa
- Add: apps/infra/openclaw/browser/openbox.xml
- Add: apps/infra/openclaw/browser/browser-supervisord.conf
- Modify: apps/infra/lib/stacks/container-stack.ts
- Modify: apps/infra/test/container-stack.test.ts

- [ ] Write failing container-stack assertions for the inert rollout shape:

  - DISPLAY=:1 remains.
  - Xvnc and websockify remain for flag rollback.
  - Openbox replaces matchbox.
  - PulseAudio and Neko are supervised auxiliary processes.
  - Neko binds privately on :6081, uses :59000 for UDP/TCP ICE mux, and never launches Chrome.
  - Neko starts only after the X socket exists.
  - Neko failure does not kill the OpenClaw gateway.

~~~bash
pnpm --filter @isol8/infra test -- container-stack.test.ts
~~~

Expected: new assertions fail against the VNC-only startup.

- [ ] Build Neko reproducibly in the image:

  - add a dedicated Debian Bookworm Go 1.25.x builder stage, leaving the existing skill builder untouched;
  - install only build-time X11/GStreamer headers in that stage;
  - clone Neko commit 395ca1a6f62b7b0e270e654d366a2d57b8042efd and run its core server build;
  - copy matching client assets from ghcr.io/m1k1o/neko/base@sha256:20806497c78de64700cb7befa674dbdc6bc4e8f0848a3dc911f0878a6726fa36;
  - install Debian Bookworm runtime libraries for GStreamer VP8/Opus, PulseAudio, Openbox, XTEST, and Supervisor;
  - copy the Neko binary and Isol8-owned config into the final image;
  - run build-time smoke checks for neko, gst-inspect-1.0 vp8enc, gst-inspect-1.0 opusenc, pulseaudio, and openbox.

- [ ] Configure Neko with private HTTP :6081, path prefix /api/v1/browser-view/webrtc, noauth behind the backend/SG fence, secure HttpOnly cookie, implicit hosting, merciful reconnect, one VP8 24-fps 1.8-Mbps pipeline, PulseAudio/Opus, ICE Lite/trickle, and UDP/TCP mux :59000.

- [ ] Disable Neko JPEG screencast, webcam, microphone, broadcast, upload-drop, pprof, and public metrics.

- [ ] Start Xvnc as today, then let Supervisor keep PulseAudio, Openbox, and Neko alive. Keep websockify/VNC :6080 untouched for rollback. Do not start Chrome from Supervisor or Neko.

- [ ] Make the focused infra test pass.

~~~bash
pnpm --filter @isol8/infra test -- container-stack.test.ts
~~~

Expected: pass.

- [ ] Build the image locally and run binary/config smoke checks.

~~~bash
docker build --platform linux/amd64 -t isol8-browser-webrtc:local apps/infra/openclaw/
docker run --rm --entrypoint /bin/sh isol8-browser-webrtc:local -lc \
  'neko --version && gst-inspect-1.0 vp8enc >/dev/null && gst-inspect-1.0 opusenc >/dev/null && pulseaudio --version && openbox --version'
~~~

Expected: every command exits zero and Neko reports v3.1.5 metadata.

- [ ] Commit the runtime layer.

~~~bash
git add apps/infra/openclaw/Dockerfile apps/infra/openclaw/browser apps/infra/lib/stacks/container-stack.ts apps/infra/test/container-stack.test.ts
git commit -m "feat(browser): add pinned Neko WebRTC runtime"
~~~

## Task 3: Give existing and new owner tasks direct ICE reachability

**Files:**

- Modify: apps/infra/lib/stacks/container-stack.ts
- Modify: apps/infra/lib/stacks/service-stack.ts
- Modify: apps/infra/test/container-stack.test.ts
- Modify: apps/infra/test/service-stack.test.ts
- Modify: apps/backend/core/containers/ecs_manager.py
- Modify: apps/backend/tests/unit/containers/test_ecs_manager.py

- [ ] Add failing CDK tests proving the backend receives public subnet IDs for owner-service creation, the owner container SG admits exactly TCP and UDP 59000 from 0.0.0.0/0, and no Neko HTTP/CDP/VNC port becomes public.

- [ ] Add a failing EcsManager test proving service creation and every task-definition roll use one canonical awsvpcConfiguration with configured public subnets, the existing container SG, and assignPublicIp ENABLED.

~~~bash
pnpm --filter @isol8/infra test -- container-stack.test.ts service-stack.test.ts
cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py -q
~~~

Expected: new assertions fail.

- [ ] Implement one _network_configuration() helper in EcsManager and use it in create-service and all update/roll paths. This migrates existing services on their next controlled deployment instead of fixing only new owners.

- [ ] Change only the owner-service subnet value exported to the backend. Backend and provisioning-runner tasks stay private.

- [ ] Add only the two public 59000 SG rules. Keep Neko :6081 reachable solely through the backend service SG's existing ingress.

- [ ] Run focused tests and the full infra suite.

~~~bash
pnpm --filter @isol8/infra test -- container-stack.test.ts service-stack.test.ts
cd apps/backend && uv run pytest tests/unit/containers/test_ecs_manager.py -q
pnpm --filter @isol8/infra test
~~~

Expected: pass.

- [ ] Commit direct media networking.

~~~bash
git add apps/infra/lib/stacks/container-stack.ts apps/infra/lib/stacks/service-stack.ts apps/infra/test/container-stack.test.ts apps/infra/test/service-stack.test.ts apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager.py
git commit -m "feat(browser): expose owner-scoped WebRTC media mux"
~~~

## Task 4: Add the owner-scoped Neko session and proxy behind the existing bv door

**Files:**

- Modify: apps/backend/routers/browser_view.py
- Modify: apps/backend/tests/unit/routers/test_browser_view.py
- Modify: apps/backend/main.py
- Add or modify: apps/backend/tests/unit/core/test_security_headers.py

- [ ] Write failing tests for missing/invalid/expired auth, personal/org owner resolution, unreachable containers, Neko login/cookie reuse, short-lived Isol8 cookie attributes, HTTP header/cookie stripping, fixed upstream host/port, WS first-completion teardown, and iframe security headers.

~~~bash
cd apps/backend && uv run pytest tests/unit/routers/test_browser_view.py tests/unit/core/test_security_headers.py -q
~~~

Expected: new tests fail.

- [ ] Extend the existing router rather than adding another tenant resolver:

  - keep /ws as temporary VNC;
  - add POST /session;
  - add /webrtc/ws before catch-all routes;
  - add /webrtc/ and /webrtc/{path:path} HTTP proxy routes;
  - resolve through resolve_running_container for every bootstrap, request, and WS connection;
  - use bounded upstream connect/read timeouts and streamed responses;
  - preserve only safe response headers and Neko cookie updates.

- [ ] Bootstrap reuses a valid Neko session cookie or logs into http://resolved-private-ip:6081/api/v1/browser-view/webrtc/api/login as constant user owner.

- [ ] Refresh the Isol8 auth cookie from the current Authorization token. Do not add a signer or session table.

- [ ] Ensure the proxy never forwards the Isol8 cookie, client forwarding headers, hop-by-hop headers, client owner, host, IP, or port.

- [ ] Skip generic X-Frame-Options only for the exact authenticated WebRTC iframe prefix and set a narrow frame-ancestors policy derived from Isol8 frontend origins.

- [ ] Make focused tests pass, then run browser-view/auth tests.

~~~bash
cd apps/backend && uv run pytest \
  tests/unit/routers/test_browser_view.py \
  tests/unit/core/test_auth.py \
  tests/unit/core/test_auth_org.py \
  tests/unit/core/test_security_headers.py -q
~~~

Expected: pass.

- [ ] Commit the authenticated signaling/static path.

~~~bash
git add apps/backend/routers/browser_view.py apps/backend/tests/unit/routers/test_browser_view.py apps/backend/main.py apps/backend/tests/unit/core/test_security_headers.py
git commit -m "feat(browser): proxy authenticated WebRTC sessions"
~~~

## Task 5: Add the flag-selected seamless WebRTC panel

**Files:**

- Add: apps/frontend/src/components/workspace/panels/WebRtcBrowserDockPanel.tsx
- Add: apps/frontend/src/components/workspace/panels/browserViewport.ts
- Modify: apps/frontend/src/components/workspace/panels/browserViewUrl.ts
- Modify: apps/frontend/src/components/workspace/panelComponents.ts
- Modify: apps/frontend/src/components/workspace/__tests__/BrowserDockPanel.test.ts
- Add: apps/frontend/src/components/workspace/__tests__/WebRtcBrowserDockPanel.test.tsx
- Modify: apps/frontend/next.config.ts

- [ ] Write failing URL and viewport tests: dev/prod/local bv mapping, all Neko paths under /api/v1/browser-view/webrtc, rounded dimensions, no DPR multiplication, <=1,024,000 pixels, and 1280 x 800 at rate 24 by default.

- [ ] Write failing render tests proving:

  - panel open/retry still calls only browser.request POST /start for lifecycle;
  - Clerk bootstrap completes before iframe mount;
  - bootstrap failure shows Retry;
  - iframe uses cast=1&volume=1 and delegates autoplay/clipboard;
  - bounded Neko api/whoami polling marks connected only when state.is_watching is true;
  - ResizeObserver posts a debounced screen update only after connection;
  - browser-view auth refresh runs every 45 seconds while mounted;
  - unmount cancels timers/requests;
  - no CDP tab polling, fake tabs/address bar, or noVNC import exists in the WebRTC component.

~~~bash
pnpm --filter @isol8/frontend test -- \
  src/components/workspace/__tests__/BrowserDockPanel.test.ts \
  src/components/workspace/__tests__/WebRtcBrowserDockPanel.test.tsx
~~~

Expected: new tests fail.

- [ ] Implement getToken -> credentialed cross-origin bootstrap -> iframe, quiet connecting/Retry states, whoami readiness, Neko's native click-to-unmute behavior, bounded 300-ms display resize, and no custom browser chrome.

- [ ] Select the component with temporary NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT=webrtc. Missing/other values select VNC for rollback.

- [ ] Add bv hosts to report-only frame-src and connect-src. Do not turn CSP enforcement on in this feature.

- [ ] Run focused tests, lint, and the frontend suite.

~~~bash
pnpm --filter @isol8/frontend test -- \
  src/components/workspace/__tests__/BrowserDockPanel.test.ts \
  src/components/workspace/__tests__/WebRtcBrowserDockPanel.test.tsx \
  src/components/workspace/__tests__/useBrowserToolAutoOpen.test.ts
pnpm --filter @isol8/frontend lint
pnpm --filter @isol8/frontend test
~~~

Expected: pass.

- [ ] Commit the gated frontend.

~~~bash
git add apps/frontend/src/components/workspace apps/frontend/next.config.ts
git commit -m "feat(browser): add seamless WebRTC workspace panel"
~~~

## Task 6: Exercise the complete path in dev and tune within the 1-vCPU budget

**Files:**

- Modify only from measurements: apps/infra/openclaw/browser/neko.yaml
- Modify only from measurements: tests pinning the chosen stream values
- Modify through normal image promotion: openclaw-version.json

- [ ] Push the branch. List the numeric workflow run IDs and watch every relevant image/backend/infra run with gh run watch until completion.

~~~bash
git push -u origin feat/browser-webrtc
gh run list --repo Isol8AI/isol8 --branch feat/browser-webrtc --limit 10 --json databaseId,name,status,conclusion
~~~

- [ ] Promote the built extended image to dev through openclaw-version.json, deploy through CI, and roll one manual dev test owner. Verify the live task definition and public ENI rather than trusting a UI label.

- [ ] Enable NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT=webrtc in the normal dev deployment configuration.

- [ ] Validate tool auto-open, manual start, native Chrome tabs/omnibox, 30-minute YouTube video/audio, OAuth popup, right-click, keyboard modifiers, scroll, clipboard, touch, download, CAPTCHA/2FA entry, panel remount, dock resize, browser restart, task redeploy, and client network switch.

- [ ] Verify Chrome, Safari, and Tauri connect, and explicitly prove one owner's credential cannot reach another owner's Neko session.

- [ ] Record Container Insights during static and video segments. Require display stack <=25% vCPU static, <=45% motion, total task <80% sustained, memory <3.2 GB, and no correlated OpenClaw RPC/gateway regression.

- [ ] If CPU fails, change exactly one value per cycle: first 24 -> 20 fps; then maximum area 1,024,000 -> 576,000 pixels. Do not add a ladder, switch codec, increase task CPU, or tune for sub-100-ms latency.

- [ ] Rerun focused/full tests after the final measured config and commit only an evidence-backed tuning diff.

~~~bash
pnpm --filter @isol8/infra test -- container-stack.test.ts
pnpm --filter @isol8/frontend test -- src/components/workspace/__tests__/WebRtcBrowserDockPanel.test.tsx
git status --short
~~~

When measured values changed, stage only those files and commit with message perf(browser): tune WebRTC for owner task budget. Otherwise create no tuning commit.

## Task 7: Roll out WebRTC with a bounded rollback window

**Files:**

- Modify: normal frontend environment transport selection
- Modify only if existing signals are insufficient: existing backend/frontend/infra observability modules

- [ ] Add only rollout signals needed for the decision: backend bootstrap/signaling outcomes, frontend panel-ready/retry/time-to-ready analytics, and existing ECS CPU/memory plus gateway/RPC alarms.

- [ ] Do not add a telemetry service or scrape every owner container.

- [ ] Enable WebRTC for a limited production cohort through the normal deployment pipeline. Repeat the user journey and resource checks on production networking without changing production servers by hand.

- [ ] Observe one full production window. Require >=99.5% measured session establishment and no material increase in browser RPC timeouts, gateway health failures, or task restarts.

- [ ] Make WebRTC the production default while retaining the VNC flag value as one-step rollback for one further observation window.

- [ ] If supported networks fail both direct UDP and ICE-TCP and success misses the gate, stop rollout and write an evidence-backed TURN addendum. Do not expose reusable TURN credentials or silently relay all media.

- [ ] Commit rollout/observability code only when code changed. Environment changes use the repository's normal pipeline.

## Task 8: Delete VNC and CDP screencast after acceptance

**Files:**

- Delete: apps/backend/routers/screencast.py
- Delete: apps/backend/tests/unit/routers/test_screencast.py
- Modify: apps/backend/main.py
- Delete: apps/frontend/src/components/workspace/panels/ScreencastDockPanel.tsx
- Delete: apps/frontend/src/components/workspace/panels/screencastInput.ts
- Delete: apps/frontend/src/components/workspace/__tests__/ScreencastDockPanel.test.ts
- Delete: apps/frontend/src/components/workspace/__tests__/ScreencastDockPanel.render.test.tsx
- Delete: VNC-only BrowserDockPanel implementation after renaming WebRTC panel
- Delete: apps/frontend/src/types/novnc.d.ts
- Modify: apps/frontend/package.json
- Modify: pnpm-lock.yaml
- Modify: apps/frontend/src/components/workspace/panelComponents.ts
- Modify: apps/frontend/src/components/workspace/panels/browserViewUrl.ts
- Modify: apps/frontend/src/components/workspace/__tests__/BrowserDockPanel.test.ts
- Modify: apps/backend/routers/browser_view.py
- Modify: apps/backend/tests/unit/routers/test_browser_view.py
- Modify: apps/infra/openclaw/Dockerfile
- Add: apps/infra/openclaw/browser/xorg.conf
- Modify: apps/infra/openclaw/browser/browser-supervisord.conf
- Modify: apps/infra/lib/stacks/container-stack.ts
- Modify: apps/infra/test/container-stack.test.ts
- Modify: apps/infra/lib/stacks/api-stack.ts
- Modify: apps/infra/test/api-stack.test.ts

- [ ] First add failing assertions for the final single transport: registry always selects WebRTC, no VNC/screencast route or URL remains, startup uses Xorg dummy + Openbox + PulseAudio + Neko, and no Xvnc/x11vnc/Xvfb/websockify/noVNC/matchbox/CDP-socat bridge remains.

- [ ] Keep OpenClaw CDP port 18800 loopback for browser automation and prove Neko still never launches Chrome.

- [ ] Remove the frontend flag and make WebRTC the canonical BrowserDockPanel.

- [ ] Remove VNC and CDP screencast routes/tests. Keep the authenticated Neko bootstrap/proxy under the existing browser-view prefix.

- [ ] Replace Xvnc with Xorg dummy so dynamic screen resize remains supported without a VNC server.

- [ ] Remove VNC/screencast Debian packages and frontend dependency/types. Remove socat only after rg proves no non-browser use.

- [ ] Update stale architecture comments and CloudFront output descriptions without widening the ALB listener.

- [ ] Run the stale-code scan.

~~~bash
rg -n "@novnc|noVNC|websockify|Xvnc|x11vnc|startScreencast|ScreencastDockPanel|screencastInput|18801|matchbox" apps pnpm-lock.yaml
~~~

Expected: no live visual-transport references.

- [ ] Run all affected suites.

~~~bash
pnpm --filter @isol8/frontend test
pnpm --filter @isol8/frontend lint
cd apps/backend && uv run pytest tests/unit/routers/test_browser_view.py tests/unit/containers/test_config_tier1b.py -q
pnpm --filter @isol8/infra test
~~~

Expected: pass.

- [ ] Commit the final cutover.

~~~bash
git add -A apps/frontend apps/backend apps/infra pnpm-lock.yaml
git commit -m "refactor(browser): remove legacy visual transports"
~~~

## Task 9: Full verification and review

- [ ] Run repository-wide verification from a clean worktree.

~~~bash
git status --short
pnpm turbo run test
pnpm turbo run lint
pnpm turbo run build
cd apps/backend && uv run pytest tests/ -q
~~~

Expected: status contains only intentional plan progress before the commands; all checks pass.

- [ ] Run a final dev journey against the exact commit proposed for merge, including 30-minute YouTube/audio soak and task-redeploy reconnect.

- [ ] Review the authority/security diff.

~~~bash
git diff origin/main...HEAD -- apps/backend/routers/browser_view.py apps/backend/core/containers/ecs_manager.py apps/infra/lib/stacks apps/frontend/src/components/workspace
~~~

Confirm no client owner/container selector, no public Neko HTTP port, no duplicate tab/navigation state, no permanent fallback transport, and no unbounded resolution/fps path.

- [ ] Use superpowers:requesting-code-review, address findings, rerun affected checks, then use superpowers:verification-before-completion.

- [ ] Push the final branch, list numeric workflow IDs, and run gh run watch for every relevant run until completion.

## Task 10: Remove disposable planning docs after the migration ships

**Files:**

- Delete: docs/superpowers/specs/2026-08-20-browser-webrtc-design.md
- Delete: docs/superpowers/plans/2026-08-20-browser-webrtc.md

- [ ] Only after the final cleanup is deployed and accepted, delete the in-flight docs in that cleanup PR or a small follow-up.

~~~bash
git rm docs/superpowers/specs/2026-08-20-browser-webrtc-design.md docs/superpowers/plans/2026-08-20-browser-webrtc.md
git commit -m "docs(browser): retire shipped WebRTC plans"
~~~

- [ ] Watch the documentation-only push. PR descriptions and git history are the durable record.
