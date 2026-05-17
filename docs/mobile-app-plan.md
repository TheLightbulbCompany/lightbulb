# iPhone Companion App — Phased Plan

**Status:** Draft, scoping only. No code written.
**Branch:** `claude/isol8-mobile-investigation-p75Il`
**Date:** 2026-05-17

## Context

Isol8 today is desktop-only outside of channel bots: chat (`/chat`), Teams
Inbox (`/teams`), billing, settings, and admin all assume a browser or the
Tauri desktop app. Channel bindings (Telegram, Discord, Slack) let agents
*reach* the user on a phone, but the user cannot operate isol8 itself from
a phone.

Upstream survey (2026-05-17, cloned `openclaw/openclaw@main` and
`paperclipai/paperclip@master`):

- **OpenClaw** ships native Swift (`apps/ios/`) and Kotlin
  (`apps/android/`) apps, both MIT-licensed. Both are alpha. Both connect
  to a local OpenClaw Gateway as `role: node` over LAN/Tailnet. That
  connection model does not match isol8's hosted multi-tenant
  architecture — those apps would need their transport layer rewritten
  before they could speak to `api.isol8.co`. Useful as UX / Swift / Kotlin
  reference, not as a fork base.
- **Paperclip** ships nothing for mobile. No `apps/` directory at all;
  `ROADMAP.md` has zero mobile mentions; "📱 Mobile Ready" in the README
  refers to responsive web. The Teams Inbox surface on mobile has to be
  built by us against `apps/backend/routers/teams/`.

## Tech choice: React Native + Expo

Decision and rationale captured for posterity:

| Option | Why not |
|--------|---------|
| Fork OpenClaw's Swift iOS app | Wrong connection model; gives us iOS only, not Android; commits the team to ongoing Swift work nobody currently does. UI/voice patterns can still be referenced. |
| Native Swift + Kotlin from scratch | Two codebases, two teams' worth of work, slow to keep parity with the web app. |
| **React Native + Expo** | Shared JS/TS with frontend; can port WS protocol + REST hooks shape-for-shape; `@clerk/expo` solves auth; `expo-notifications` solves push; iPhone-first with Android effectively free; team is already JS/TS+Rust, not Swift/Kotlin. |

Tradeoff accepted: native-only goodies (Watch app, Live Activities, share
extension, deep background behaviors) need EAS dev-client + custom native
modules. Treated as Phase 5+, not MVP.

## Phase 0 — Backend prerequisites

Mobile without these is a worse browser. Build them first.

- **Push infrastructure**
  - New DynamoDB table `isol8-{env}-push-tokens` keyed by `user_id`,
    holding APNs + FCM tokens, device IDs, last-seen, app version.
  - REST: `POST /api/v1/push/tokens`, `DELETE /api/v1/push/tokens/{id}`.
  - Fan-out helper invoked alongside the Management API push in
    `core/gateway/connection_pool.py` and `routers/websocket_chat.py` so
    important events (final chat reply, approval request, thread mention)
    notify the device when WS isn't connected.
  - APNs cert + FCM service account in Secrets Manager; CDK changes in
    `apps/infra/lib/stacks/auth-stack.ts` and `database-stack.ts`.
- **Teams BFF JSON-completeness audit.** `apps/frontend/src/app/teams/`
  embeds Paperclip HTML in places (`routers/paperclip_proxy.py`). For
  mobile we need first-class JSON for: inbox tabs, thread detail,
  message send, approval action, member list, agent picker, attachments
  (signed URLs). Output: gap list + tickets.
- **OAuth callback URL whitelist.** `routers/channels.py` and
  `routers/oauth.py` accept return URLs; add `isol8://` (mobile scheme)
  alongside the existing `https://` redirects so channel-link and
  ChatGPT-OAuth flows can finish on device via `expo-auth-session`.
- **Service-token issuance for mobile sessions** (likely a non-issue —
  Clerk JWT works the same — but verify `core/auth.py` accepts the JWT
  shape minted by `@clerk/expo`).

## Phase 1 — Expo skeleton + auth + read-only surfaces

Goal: TestFlight build that signs in, shows container health, shows
billing state, and ends the trial decision loop on mobile.

- Scaffold `apps/mobile/` (Expo SDK, TypeScript, expo-router).
  - Turbo task: `turbo run dev --filter=@isol8/mobile`.
  - Shared `packages/api-client/` (extracted from `apps/frontend/src/lib/api.ts`
    and reused by both web and mobile — small refactor, big payoff).
- `@clerk/expo` integration. Sign-in / sign-up screens. Token plumbing
  for REST + WS.
- App shell: tab nav (Chat, Inbox, Settings), dark mode parity with web.
- Container status panel (`GET /container/status`), billing summary
  (`useBilling`), provision-gate blocked state UI.
- CI: EAS Build + EAS Submit configured in `.github/workflows/mobile-build.yml`.
  Internal TestFlight on every push to `main`.
- Crash/analytics: Sentry Expo + PostHog React Native.

Exit criterion: an internal tester installs from TestFlight, signs in,
sees their container is healthy and their plan is active.

## Phase 2 — Agent chat MVP

Goal: a useful chat client. The smallest scope that meaningfully replaces
"I'd open my laptop right now."

- WebSocket client. Port message shapes from
  `apps/frontend/src/hooks/useGateway.tsx` and `useAgentChat.ts` into
  `apps/mobile/src/lib/gateway.ts`. Keep the same `chunk` / `tool_start` /
  `tool_end` / `done` / `error` payloads.
- Single-agent chat UI: streaming text, tool-use chips, thinking blocks,
  cancel button (`chat.abort` RPC).
- File send: photo from camera roll, image from camera, voice memo
  upload (no voice-to-text yet — defer to Phase 5).
- iOS share extension via Expo's `expo-share-extension` plugin (Phase 2
  if cheap, otherwise punt to Phase 5).
- Push integration: receive notification → deep-link into the right
  agent thread.

Exit criterion: a user finishes a real agent task from their phone,
end-to-end, while their laptop is closed.

## Phase 3 — Teams Inbox

Goal: triage and approve work from the phone. This is the killer feature
— most "I need to be at a computer" moments for power users are Teams
approvals.

- Inbox tabs (mine / recent / all / unread) backed by `GET /teams/inbox`.
- Thread detail screen: message list, infinite scroll, attachments.
- Compose / reply.
- **Approval cards** — first-class mobile UX for `routers/teams/approvals.py`,
  with Approve / Request changes / Reject actions and contextual diff
  view where applicable.
- Member + agent picker.
- Live updates via `teams.subscribe` on the existing WS, with push as
  fallback when the app is backgrounded.

Exit criterion: a user approves a real agent PR from their phone in
under 30 seconds from notification → tap → approve.

## Phase 4 — Channels & settings

- Linked-channels view (`GET /links/me`) with per-provider status.
- Provider OAuth via `expo-auth-session` + the `isol8://` callback scheme
  added in Phase 0.
- Unlink action.
- Settings parity: API keys (BYOK) — encrypted client-side before upload
  via the existing `key_service` flow; model picker; theme.

Exit criterion: a user can onboard a new Telegram bot for a new agent
from their phone without touching a computer.

## Phase 5 — Native power features (optional, demand-driven)

Pull each in only when a real user asks for it. Each is significant
native work.

- iOS Watch complications + Live Activities for in-progress agent runs.
  Closest reference: OpenClaw's `apps/ios/WatchApp` and `ActivityWidget`
  — MIT, can be ported.
- Voice mode (push-to-talk + transcription). Reference: OpenClaw iOS
  voice tab.
- Background presence beacon (parity with the OpenClaw Android push
  feature). Useful for "agent finished a long-running task" telemetry.
- Siri Shortcuts: "Hey Siri, ask Friday about today's bookings."
- iPad layout pass (probably falls out of expo-router naturally).

## Cross-cutting tracks

- **Observability.** Sentry + PostHog from Phase 1. CloudWatch EMF
  parity for `apps/mobile`-originating REST calls (correlation IDs via
  `core/observability/e2e_correlation.py`).
- **Release engineering.** EAS Build for binaries, EAS Update for OTA JS
  bundles between native releases. Pinned RN + Expo SDK versions in
  `package.json`; renovate / dependabot for security only, not feature
  bumps.
- **App Store review prep.** Privacy manifests, ATT prompts only if
  PostHog requires them, "Sign in with Apple" if Apple insists during
  review (Clerk supports it).
- **E2E coverage.** Detox or Maestro on EAS-built simulators in CI for
  the golden path (sign-in → chat → reply → approve).

## Open questions

- Do we want a single binary or split apps for the agent/chat surface vs
  the operator/admin surface? (Recommendation: one app, role-gated UI.)
- Is Android part of the launch goal or "free Phase 3+ bonus"?
- Do we ship "Sign in with Apple" at launch or after first App Store
  rejection?
- Are we comfortable taking on APNs + FCM ops, or punt to a managed
  service (OneSignal, Knock)?

## Non-goals

- Replacing the desktop app. Tauri stays.
- Embedding the full Paperclip HTML UI in a WebView. We build native
  screens on top of the JSON BFF.
- Running OpenClaw on-device. The phone is always a thin client to the
  hosted ECS container.
