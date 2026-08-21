# Task 5 report — seamless WebRTC Browser panel

Base: `ab09f79f6cb2ff7e22059f520b81d0f90a504fa3`

## RED

- Frontend URL/viewport and panel tests initially failed because
  `browserViewport` and `WebRtcBrowserDockPanel` did not exist.
- The generated-workflow assertion initially failed because `deploy.yml` had
  neither transport build value.
- Follow-up RED checks caught duplicate iframe readiness pollers, retrying an
  unchanged failed screen resize, and a late `whoami` result from an older
  iframe load.

## GREEN

- Added import-free browser-view URL and viewport helpers, the bounded Neko
  iframe panel, exact build-time transport selection, report-only CSP hosts,
  and CDK-generated dev/prod workflow values.
- Regenerated `.github/workflows/deploy.yml` with `cdk synth`; it contains
  `webrtc` for dev and `vnc` for prod.
- Focused frontend suite: 40 tests passing (3 files).
- Focused generated-workflow suite: 5 tests passing.
- Focused changed-file ESLint: clean. Full frontend ESLint exits 0 with 30
  pre-existing warnings outside this task.

## React checklist

- Stable effect dependencies; timers, requests, and the resize observer clean
  up on retry or unmount.
- Transient state stays in refs; no new dependency or WebRTC SDK.
- Iframe has descriptive title, delegated autoplay/clipboard permissions, and
  fullscreen support; Retry has visible keyboard focus.
- The panel exposes no tabs, navigation, CDP, screencast, or noVNC surface.

## Verification note

Full frontend and infra test commands were started with direct local Vitest
binaries after the workspace `pnpm` wrapper stalled. The frontend full suite
exceeded the command runner's 30-second capture window without a final result.
The non-escalated infra full suite hit the known sandbox Docker/DNS asset
bundling restriction; focused infra coverage is green. No deployment or push
was performed.
