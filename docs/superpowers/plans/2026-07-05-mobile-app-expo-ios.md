# Isol8 iOS Mobile App (Expo / React Native) — Implementation Plan

**Status:** Draft — awaiting review

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a native iOS app (`apps/mobile`) with full parity to the **end-user web application** — auth, chat, control panel, teams inbox, marketplace, settings — reusing the web app's protocol/data layer via a new shared package, distributed through TestFlight → App Store with EAS-based CI/CD.

**Scope boundary (hard):** end-user product surfaces ONLY. No admin dashboard, no ops/internal tooling of any kind — the `/admin` surface, admin routers/hooks (`useSystemHealth`, admin components), marketplace moderation, debug endpoints, and the unmaintained dark control panels (Config/Logs/Instances/Nodes/Debug) are all permanently out of scope for mobile, not deferred. The marketplace **seller** side (listing creation/publish, payouts) is also web-only — mobile is buyer-only (D2). The landing/marketing pages are out too — the app opens into sign-in/chat.

**Architecture:** Expo (managed workflow) + expo-router app in `apps/mobile`, consuming a new framework-neutral `packages/shared` extracted from the web frontend's logic layer (API client core, gateway WebSocket protocol, chat/session/agent hooks, TypeScript contracts). The web app is refactored to consume the same package so the two clients cannot drift. All UI is rebuilt in React Native primitives (no DOM). Backend is untouched — the mobile app speaks the exact same REST (`/api/v1`) + WebSocket gateway protocol the browser does.

**Tech Stack:** Expo SDK (latest stable, React 19-compatible), expo-router, `@clerk/clerk-expo` + expo-secure-store, TypeScript, NativeWind (Tailwind-for-RN, keeps styling mental model), SWR (works on RN), react-native-markdown-display + a RN syntax highlighter, expo-notifications (APNs), EAS Build / Submit / Update for CI/CD.

---

## Why this shape (context for reviewers)

- **The web frontend's brain ports; its face doesn't.** `lib/api.ts` (fetch + Clerk bearer), `hooks/useGateway.tsx` (WS RPC protocol: reconnect/backoff, ping, pending-RPC map, chat/event dispatch), and the data hooks built on them (`useAgentChat`, `useAgents`, `useContainerStatus`, `useGatewayRpc`, teams data layer) are pure JS/React logic with only four Next.js couplings: `@clerk/nextjs` imports, `NEXT_PUBLIC_*` env reads, `"use client"` directives, and two harmless `window.__TAURI__` branches. Every `.tsx` component is DOM/Tailwind and must be rewritten in RN primitives.
- **expo-router was modeled on the Next.js App Router**, so the `src/app/` file-based routing mental model transfers ~1:1 (`app/(auth)/sign-in.tsx`, layouts, dynamic segments). Middleware/SSR do not transfer: Clerk route protection and the admin-host gate become navigation guards; the admin surface is **excluded** from mobile scope (it's an internal ops tool gated to admin hosts).
- **Extraction over copy-paste.** A copied gateway hook drifts the moment the web one is patched (and it is patched often — see the run_ended / concurrency work). `packages/shared` with per-platform adapters (Clerk token getter, env config, storage) keeps one protocol implementation.

## Global Constraints

- **Backend is read-only for this plan.** The mobile app must work against the existing `/api/v1` REST surface and WS gateway with zero server changes, except the push-notification endpoint in Phase 7 (explicitly scoped there).
- **`packages/shared` must be framework-neutral**: no `next/*`, no `react-native` imports, no DOM globals (`window`, `document`, `localStorage`), no `crypto.randomUUID()` without going through an injected `uuid()` seam (RN needs `react-native-get-random-values`). Platform adapters (web/mobile) inject: `getToken()`, env config (`API_URL`/`WS_URL`), and UUID.
- **Web refactor must be behavior-preserving.** Extracting into `packages/shared` may move code but must not change it — existing frontend vitest suites must pass unmodified (import paths aside). Frontend: never run full `pnpm test` (RAM-heavy) — targeted single-file vitest only.
- **pnpm + Expo/Metro:** add `node-linker=hoisted` to the repo `.npmrc` scope needed by `apps/mobile` (Metro does not follow pnpm symlinks). Validate this does not break the Next.js or Tauri builds before merging Phase 0.
- **Apple billing rules (Guideline 3.1.1):** the app must NOT initiate Stripe checkout for the $50/mo subscription or paid marketplace listings in-app for v1. Mobile renders subscription/entitlement *state* and directs users to manage billing on the web (see Decisions & Risks). Marketplace **browsing + deploying free/already-owned listings** is in scope; in-app purchase of paid listings is out.
- **No secrets in the app bundle.** Only `EXPO_PUBLIC_*` values that are safe-public (API URL, Clerk publishable key, PostHog key) — same posture as `NEXT_PUBLIC_*`.
- **Env parity:** dev builds point at `api-dev.isol8.co` / `ws-dev.isol8.co`; prod at `api.isol8.co` / `ws.isol8.co`. The `deriveWebSocketUrl` hostname rewrite in shared code is reused as-is.
- **Bundle ID:** `co.isol8.mobile` (sibling of desktop's `co.isol8.desktop`). Apple Developer team is the prerequisite for Phases 7–8; everything before runs in Expo Go / simulator.
- **iOS only for v1.** Do not add Android config beyond what Expo scaffolds by default; do not spend effort making Android work.

## Decisions & Risks (resolve before/while executing)

| # | Item | Position |
|---|------|----------|
| D1 | **Subscription billing on iOS** | v1: no in-app purchase flow. Show plan status read-only + "Manage billing on isol8.co". Post-2025 US external-purchase-link rules allow an outbound link; keep copy neutral and revisit IAP only if App Store review forces it or conversion demands it. |
| D2 | **Marketplace is buyer-only** | Browse everything, deploy free/owned listings; paid purchase deep-links to web (same posture as D1). The seller side — listing creation, publish pipeline, payout accounts — is web-only, permanently. |
| D3 | **Admin surface + all ops tooling** | Permanently out of scope (see Scope boundary above) — never port `/admin`, admin components/hooks, marketplace moderation, or debug surfaces to mobile. |
| D4 | **ChatGPT OAuth / BYOK flows** | Settings shows provider state; the OAuth dance and key entry use in-app browser (`expo-web-browser`) against existing web/API flows. Verify redirect URIs work with `isol8://` or universal links; if not, defer to "manage on web" like D1. |
| D5 | **Desktop-node features** (exec approvals, browser proxy, iMessage) | Render approval cards read-only where they appear in chat; node pairing itself is desktop-only and out of scope. |
| D6 | **Styling system** | ~~NativeWind vs StyleSheet~~ **Resolved in Phase 0:** scaffold ships plain StyleSheet + a future tokens file; NativeWind re-evaluated when Phase 2 builds the first real screens (adopting it later is additive, ripping it out isn't). |
| R1 | **App Store review risk** | Full-parity app with real native UI clears Guideline 4.2 easily; the billing posture (D1/D2) is the main review surface. Have a demo account ready for review notes. |
| R2 | **WS behind cellular/backgrounding** | iOS suspends sockets on background. Gateway hook must handle app-state transitions (reconnect on foreground) — covered in Phase 2 Task 6; push notifications (Phase 7) cover the "agent finished while backgrounded" gap. |
| R3 | **pnpm hoisting side-effects** | ~~`node-linker=hoisted` may be needed~~ **Resolved in Phase 0: NOT needed.** Metro on Expo SDK 57 resolves pnpm's default isolated `.pnpm` symlink layout — `expo export --platform ios` bundles cleanly (1097 modules) with zero `.npmrc` changes, so the repo install layout is untouched. If a future native dep breaks resolution, revisit then. |

---

## File Map (net-new + touched)

| Path | Action | Responsibility |
|------|--------|----------------|
| `packages/shared/` | **New** | Framework-neutral core: `src/api/` (client core, ApiError, URL derivation), `src/gateway/` (protocol types, connection state machine), `src/hooks/` (useAgentChat/useAgents/etc. against injected adapters), `src/types/` (OpenClaw + BFF contracts) |
| `apps/mobile/` | **New** | Expo app: `app/` (expo-router routes), `src/components/`, `src/adapters/` (Clerk/env/uuid platform adapters), `app.json`, `eas.json` |
| `apps/frontend/src/lib/api.ts`, `src/hooks/useGateway.tsx`, `src/hooks/useAgentChat.ts`, `src/hooks/useAgents.ts`, `src/hooks/useGatewayRpc.ts`, `src/hooks/useContainerStatus.ts`, teams data layer | Refactor | Re-export from / delegate to `@isol8/shared` with the web adapter (behavior-preserving) |
| `pnpm-workspace.yaml`, `turbo.json`, root `package.json` | Modify | Already glob `packages/*`; add turbo tasks for mobile (`lint`, `test`, `typecheck`); no workspace-glob change needed |
| `.npmrc` / `apps/mobile/.npmrc` | New/Modify | `node-linker=hoisted` scoped for Metro |
| `.github/workflows/mobile-build.yml` | **New** | Path-filtered EAS build/submit workflow (mirrors `desktop-build.yml`'s standalone pattern, but on GitHub-hosted runners — EAS does the macOS part) |
| `.github/workflows/frontend-ci.yml` | Modify | Ensure shared-package tests/lint run when `packages/shared/**` changes |
| `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md` | This file | Track execution |

---

## Phase 0 — Foundations: workspace, scaffold, shared package skeleton

**Exit gate:** `apps/mobile` boots in the iOS simulator showing a placeholder screen; `turbo run lint test` green across frontend + shared; frontend prod build unaffected.

- [x] **Task 1: Workspace wiring.** Scaffolded `apps/mobile` with `create-expo-app` (Expo SDK 57 / RN 0.86 / React 19.2.3 — exact match with frontend's React). No `.npmrc` change needed (see R3). `pnpm install` green across all 7 workspace projects; `typecheck` task added to `turbo.json`.
- [x] **Task 2: `packages/shared` skeleton.** `@isol8/shared` created with tsconfig (ES2022, no DOM lib), vitest harness, and the `PlatformAdapter` interface (`getToken`/`apiUrl`/`wsUrl?`/`uuid`). Smoke test passing.
- [x] **Task 3: Styling + navigation baseline.** StyleSheet for now (D6). expo-router skeleton: `(auth)/sign-in` placeholder + `(app)` tab shell (Chat/Teams/Control/Settings), typed routes + React Compiler enabled. `expo lint` configured (flat config).
- [x] **Task 4: Env config.** `src/config/env.ts` reads `EXPO_PUBLIC_API_URL`/`EXPO_PUBLIC_WS_URL` with the same WS-derivation rules as web (temporary duplicate; delegates to shared in Phase 1). Documented in `apps/mobile/README.md`.

**Phase 0 verification record (2026-07-05, Linux CI container):** shared vitest 1/1 + tsc clean; mobile tsc clean + `expo lint` clean; `expo export --platform ios` bundles 1097 modules → 2.3MB Hermes bundle; frontend unaffected (lint 0 errors, targeted `api.test.ts` 13/13). Simulator/device verification pending a human with Expo Go (no macOS in CI).

## Phase 1 — Shared extraction + auth

**Exit gate:** web frontend fully on `@isol8/shared` with its existing tests passing; mobile signs in via Clerk and makes an authenticated `GET /users/me`-class call against dev.

- [x] **Task 1: Extract API core.** Done: `packages/shared/src/api/` (error/url/client) parameterized by `ApiClientConfig` (`getToken`/`apiUrl`/`onRequestId`); `apps/frontend/src/lib/api.ts` is now the web wrapper (env source, Clerk hook, request-id ring buffer) with its historical export surface intact. `transpilePackages: ["@isol8/shared"]` added to next.config. New shared tests cover the 401-retry-once, detail-message extraction, FormData content-type skip, and request-id reporting.
- [x] **Task 2: Extract gateway core.** Done: `packages/shared/src/gateway/` (`types.ts` + `useGatewayConnection`) with `getToken`/`wsUrl`/`uuid` routed through refs to preserve the single-flight connect contract; web `useGateway.tsx` keeps React context + both Tauri effects (node:status via the new `setNodeConnected` escape hatch) and re-exports the protocol types.
- [ ] **Task 3: Extract data hooks.** `useAgentChat`, `useAgents`, `useGatewayRpc`, `useContainerStatus`, `useProvisioningState`, and the teams data-layer hooks — moved to shared where framework-neutral; anything DOM-touching stays web-side and is noted here.
- [x] **Task 4: Mobile auth.** Done: `@clerk/clerk-expo` + keychain token cache (`expo-secure-store`), headless email+password sign-in (other first factors deferred to web with an explicit message), `(auth)`/`(app)` navigation guards, sign-out in Settings, mobile adapter (`expo-crypto` randomUUID). Chat tab proves the plumbing with the real `GET /container/status` 200-vs-202 call through the shared client. **Clerk dashboard check still pending:** confirm the dev instance allows native-app requests; sign-in against dev is untested until a human runs Expo Go.
- [ ] **Task 5: Cookie posture for Teams BFF.** Verify RN `fetch` cookie behavior for the `.isol8.co` Paperclip HttpOnly cookie + retry-on-401 reseed flow against dev; if RN cookie jar handling breaks it, record the fallback (e.g. explicit cookie header plumbing) before Phase 5.

**Phase 1 verification record (2026-07-06, Linux CI container):** shared vitest 22/22 + tsc + eslint clean (incl. a `no-restricted-globals` ban on DOM-only APIs); frontend `api.test.ts` 13/13, `useGateway.connectRace` 2/2, `useAgentChat` queue/demux/reconnect/cancel/busyGate + `useBilling` + `useProvisioningState` 48/48, full frontend `tsc --noEmit` clean, lint 0 errors; mobile tsc + `expo lint` clean, `expo export --platform ios` bundles 1238 modules with Clerk + shared included. Pending human: Expo Go sign-in against dev.

## Phase 2 — Chat surface (the core product)

**Exit gate:** on a dev account: list agents, open Friday, stream a full run (chunks, thinking, tool pills incl. progress/command output, run_ended), multi-session sidebar, file upload, abort — on device over LTE, surviving background/foreground.

- [ ] **Task 1: Gateway provider on RN.** Mount shared gateway with RN WebSocket; add `AppState` handling — teardown-tolerant suspend on background, reconnect + `sessions`/status refresh on foreground (R2).
- [ ] **Task 2: Agent list + shell.** Sidebar-equivalent drawer/tab: agent roster (Friday/Atlas + customs), health indicator, provisioning stepper states (reuse `useProvisioningState`), blocked-state rendering (provision gate).
- [ ] **Task 3: Message timeline.** RN rewrite of `MessageList` semantics: markdown (react-native-markdown-display), code blocks w/ highlighting, thinking blocks, tool pills (start/progress/end/error + command output), agent_activity pill, btw side-cards. Virtualized list (FlashList) — chat transcripts get long.
- [ ] **Task 4: Composer.** Input, send (`sendChat` + clientRunId), stop/abort, file upload via `expo-document-picker`/`expo-image-picker` → existing `POST /container/files` multipart, suggested-message affordance.
- [ ] **Task 5: Sessions.** Multi-session-per-agent UI per the 2026-07-01 chat-sessions plan: session list, create (lowercase UUIDv4 constraint!), model·effort controls via `sessions.patch` semantics — reusing whatever the web shipped, through shared hooks.
- [ ] **Task 6: Resilience QA.** Kill network mid-run, background mid-run, token expiry mid-session; verify reconnect and no wedged chat queue (the run_ended-driven drain must behave identically to web).

## Phase 3 — Onboarding, provisioning, billing state

**Exit gate:** fresh dev account can sign up on-device, watch provisioning complete, land in chat; an unsubscribed account sees the correct blocked state with a working "manage on web" path.

- [ ] **Task 1: Onboarding flow.** Port `/onboarding` semantics: `GET /container/status` polling (200 vs 202 semantics via `fetchJsonWithStatus`), stepper UI, error/blocked branches.
- [ ] **Task 2: Billing read-only surface.** Plan status, trial state (Stripe owns the trial clock — render only what the API returns), Bedrock credits, usage summary. All mutation paths (checkout, portal) → `expo-web-browser` to the web app (D1).
- [ ] **Task 3: Provider status.** Show provider choice (Bedrock / ChatGPT OAuth / BYOK) state; OAuth/key management per D4.

## Phase 4 — Control panel parity

**Exit gate:** Overview, Agents (CRUD + inspector tabs), Sessions, Usage, Skills, MCP servers, Cron (view/edit/vet), LLM + Credits panels functional on device. Dark panels (Config/Logs/Instances/Nodes/Debug) explicitly excluded — they're unmaintained on web (see audit-2026-05-04).

- [ ] **Task 1: Overview panel** (health, uptime, agents, sessions, billing, usage tiles).
- [ ] **Task 2: Agents panel** — roster CRUD via shared `useAgents`, overview/tools/channels tabs, create form.
- [ ] **Task 3: Sessions + Usage panels.**
- [ ] **Task 4: Skills + MCP servers panels.**
- [ ] **Task 5: Cron panel** (list/detail/edit/vet).
- [ ] **Task 6: LLM + Credits panels.**
- [ ] **Task 7: Workspace file browser** — FileTree/Viewer against `workspace_files` endpoints; save-file for text; gallery for images via authenticated blob fetch (RN file handling replaces object URLs).

## Phase 5 — Teams inbox parity

**Exit gate:** Teams surfaces usable on device against dev: inbox triage, issue detail (comments incl. rich content, runs, approvals, sub-issues, documents), agents/org chart, members, routines, dashboard, kanban. Realtime updates arrive via the existing teams subscribe envelopes over the shared gateway `send`.

- [ ] **Task 1: Teams data layer on mobile** — shared hooks + cookie flow (Phase 1 Task 5 findings applied).
- [ ] **Task 2: Inbox + triage.**
- [ ] **Task 3: Issue detail** (comments, runs, approvals, sub-issues, documents, attachments upload/download).
- [ ] **Task 4: Agents / org chart / members.**
- [ ] **Task 5: Routines + dashboard + kanban** (charts via a RN chart lib or simplified tiles — decide during task, favor simple).
- [ ] **Task 6: Realtime** — teams.subscribe/unsubscribe over gateway, re-fire on reconnect, background/foreground behavior.

## Phase 6 — Marketplace + settings parity

**Exit gate:** search/browse listings, listing detail, deploy free/owned listing end-to-end on device; settings parity minus admin. Marketplace is **buyer-only on mobile** — the entire seller side (create/publish listings, artifact-from-agent, payouts) stays web-only.

- [ ] **Task 1: Buyer surface** — search (existing 60s-cached endpoint), listing detail, deploy (entitlement-aware); paid listings deep-link out (D2). No seller entry points anywhere in the mobile UI.
- [ ] **Task 2: Settings** — profile, channels (MyChannels), API keys (BYOK entry per D4), integrations list.

## Phase 7 — Push notifications + polish

**Exit gate:** agent-run-completed push arrives with app backgrounded; deep-link opens the right chat; app icon/splash/launch polish done.

- [ ] **Task 1: Backend push endpoint (the one server change).** Design + implement device-token registration (`POST /api/v1/devices` on the users surface, DDB-backed) and a notification dispatch hook on `run_ended` for backgrounded users, via `expo-notifications` push service (or bare APNs — decide with infra posture in mind; CDK changes go through `apps/infra`). This slice follows the repo's full CI/CD rules (CDK for any infra, watch runs after push).
- [ ] **Task 2: Client side** — permission prompt UX, token registration, notification → deep-link routing (`isol8://` or universal links), badge counts.
- [ ] **Task 3: Polish pass** — icons, splash, haptics, keyboard avoidance audit, accessibility (VoiceOver on chat timeline), performance profile of the message list.

## Phase 8 — CI/CD, TestFlight, App Store

**Exit gate:** merged-to-main mobile changes produce an EAS build; tagged releases auto-submit to TestFlight; App Store submission checklist complete.

- [ ] **Task 1: EAS project setup.** `eas.json` with `development` / `preview` (dev API, internal distribution) / `production` profiles; EAS-managed Apple signing under the Isol8 Apple Developer team; `EXPO_TOKEN` + Apple credentials in GH secrets (never in repo).
- [ ] **Task 2: `mobile-build.yml`.** Path-filtered on `apps/mobile/**` + `packages/shared/**`. PR: lint/typecheck/vitest only (no build). Main: `eas build --profile preview --non-interactive`. Tag `mobile-v*`: production build + `eas submit` to TestFlight. Runs on `ubuntu-latest` — EAS's hosted macOS fleet does the native build, so **no self-hosted mac runner needed** (unlike `desktop-build.yml`).
- [ ] **Task 3: OTA updates.** Wire `eas update` for JS-only hotfixes on the production channel; document the "native change ⇒ store build, JS change ⇒ OTA" rule in `apps/mobile/README.md`.
- [ ] **Task 4: Store submission.** App Store Connect listing (screenshots, privacy nutrition labels — Clerk/PostHog data flows disclosed), demo account for review (NOT the reserved `isol8-e2e-testing@mailsac.com` account), review notes covering the D1 billing posture.

---

## Effort shape (for planning, not promises)

Phases 0–2 are the critical path to a usable dogfood build (chat on your phone). Phases 4–6 are wide-but-shallow UI ports off the same shared layer and parallelize well across workers. Phase 7 Task 1 is the only backend/infra work. Expect the shared-extraction refactor (Phase 1) to be the highest-risk merge against ongoing web work — land it early and small.
