# Mobile Push Notifications — v1 (agent-reply) Design

**Date:** 2026-07-10
**Status:** Design — approved in brainstorm, pending spec review
**Author:** prez2307 (with Claude)
**Scope:** Refines Phase 7 (Task 1 + 2) of `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md`.
**Grounding:** file/line references verified this session against the working tree on branch `claude/mobile-expo-updates`.

---

## Summary (read this first)

When a run **you started** completes, your phone gets a notification — **even if the app is
closed** — that shows the agent's name + a preview of the reply, and taps straight into that
agent's chat. That's the whole of v1.

This is deliberately the *user-initiated* slice. Proactive notifications (cron, incoming
channel messages, Teams approvals) require a durable outbound-event mechanism and are **v2**
(see Non-Goals). v1 needs only: a per-user device-token store, a mobile registration step, a
`run_ended` dispatch hook, and the Expo Push API as the transport.

**Why this is small:** our hosted backend + Clerk JWT + Expo-managed app collapse the entire
self-hosted push apparatus OpenClaw ships (hosted APNs relay, App Attest, App Store receipt,
per-registration send-grant — see `apps/ios/Sources/Push/*` in the OpenClaw upstream) down to
*token → POST to our backend → Expo Push API*. Our backend is already the trusted sender; the
Clerk JWT already authenticates the registration; Expo/EAS manages the APNs key. **No APNs
credential handling in our infra.**

---

## Goals / Non-Goals

### Goals
- A run initiated by a member, on completion, produces a push to **that member's** device(s),
  delivered even when the app is backgrounded or fully closed.
- Notification shows agent name (title) + ~120-char reply preview (body); tap deep-links to
  that agent's chat at the run's session.
- Org-correct: only the member who started the run is notified, never the whole org.
- One backend surface change (device registration) + one send hook + one small
  connection-lifecycle guard. No changes to the client's existing chat/gateway path.

### Non-Goals (explicit ceiling)
- **Proactive / autonomous notifications** — cron runs, incoming channel (Telegram/Slack/…)
  messages the agent answers, Teams/Paperclip approvals. These fire *inside* the container with
  no client-initiated `chat.send`, so the backend may not be connected at completion time; they
  need a durable container→backend outbound-event mechanism. **v2, separate spec.**
- **Interactive notification actions** (approve/reply from the lock screen). v2 (P1 item).
- **Badge counts, notification inbox, Android.** Later.
- **Multi-device beyond one phone.** The data model supports N devices (map), but we design and
  test against the stated assumption of one phone (+ one desktop) per user.

---

## Architecture

### 1. Data model — `users` table, `push_tokens` map

Push tokens live on the **`users`** row (member-scoped = org-correct: a token addresses a
*human*, independent of org membership). NOT on the `containers` row (owner-scoped) and NOT a
new table — see §Decisions.

```
users[user_id].push_tokens = {
  "<deviceId>": { "token": "ExponentPushToken[…]", "platform": "ios", "updatedAt": "<iso>" }
}
```

- `deviceId` = a stable app-installation UUID generated once and stored in `expo-secure-store`
  (distinct from `paired_devices`' `sha256(pubkey)` id — a phone has no Ed25519 keypair).
- **Writes use scoped `update_item`, never `put`.** `user_repo.put` is a full `put_item` that
  rewrites the whole record (would clobber `email`/`created_at`) and there is no updater today
  (`core/repositories/user_repo.py` — methods: `get`, `put`, `delete`, no `update`). Add:
  - `set_push_token(user_id, device_id, entry)` — `SET #push.#did = :entry` with
    `if_not_exists(#push, :empty)` to init the map (two-step, mirroring
    `container_repo._put_paired_device`, `container_repo.py:144-183`).
  - `clear_push_token(user_id, device_id)` — scoped `REMOVE #push.#did` (mirrors
    `container_repo.remove_paired_device`, `container_repo.py:186-204`).
- Reads are free: `user_repo.get(user_id)["push_tokens"]`.

### 2. Registration (mobile client)

- Add deps + config: `expo-notifications` (dep + config plugin), `expo-device`; add the
  `aps-environment` entitlement via the plugin / `app.json` `ios.entitlements`. (`app.json`
  today: `ios.bundleIdentifier = co.isol8.mobile` (→ `ai.flickmeon.mobile` at the flag-day flip), `appleTeamId = WZX4U3C22Y`, no
  `aps-environment`; SDK 57; `expo-updates` present; `expo-secure-store` already a dep.)
- New `<PushRegistration/>` component mounted **inside the signed-in subtree** — in
  `apps/mobile/src/app/(app)/_layout.tsx` inside `<GatewayProvider>` (line ~28), a sibling of
  `<ProvisionGate>`. (Must NOT be a hook directly in `AppLayout`, which early-returns on the
  `isSignedIn` gate above.)
- Effect: request permission → `Notifications.getExpoPushTokenAsync({ projectId })` →
  `POST /api/v1/devices { token, platform, deviceId }`. Persist last-sent token in
  `expo-secure-store`; only re-POST when it changes.
- Auth/REST pattern (mandatory, per every existing call site, e.g. `lib/marketplace.ts`):
  ```ts
  const getTokenRef = useRef(getToken); getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  await api.post("/devices", { token, platform: "ios", deviceId });
  ```
  (`EXPO_PUBLIC_API_URL` already includes `/api/v1`; bearer auto-attached in
  `packages/shared/src/api/client.ts`.)

### 3. Backend registration endpoint

- `POST /api/v1/devices` (extend `routers/users.py` or a small `routers/devices.py`), auth via
  `Depends(get_current_user)`. Body `{ token, platform, deviceId }`. Validates the token looks
  like an Expo push token, then `user_repo.set_push_token(auth.user_id, deviceId, entry)`.
- Optional `DELETE /api/v1/devices/{deviceId}` for explicit sign-out cleanup (clears the token).

### 4. Send path (backend)

- **Hook:** where the once-per-run terminal `run_ended` is synthesized for a **user-initiated
  (member-attributed, webchat) session** in `connection_pool.py` (`run_ended` synthesis
  ~`:1385-1395`, gated on `_is_true_terminal`). This is the same reader loop that already
  spawns `_record_usage_from_session` on completion — the push dispatch is a sibling
  **fire-and-forget task** (`asyncio.create_task`), never on the hot path.
- **Audience:** `target_member` is already parsed from the OpenClaw sessionKey
  (`agent:{agentId}:{userId}`) via `_parse_session_key` (bound ~`:1104`). Push to
  `users[target_member].push_tokens`. If `target_member` is absent (personal/channel/cron
  sessions), **do not** owner-broadcast in v1 — those are the v2 proactive cases; v1 sends only
  when a member is attributed.
- **Content:** title = agent display name; body = `_extract_assistant_text(payload)` truncated
  to ~120 chars (`_extract_assistant_text`, `connection_pool.py:741`); `data = { agentId,
  sessionKey }`.
- **Transport:** new `core/services/push_service.py` → Expo Push API
  (`https://exp.host/--/api/v2/push/send`), batched over the member's tokens. Auth via an **Expo
  access token** stored in Secrets Manager and injected via CDK (`apps/infra`). APNs key is
  managed by EAS/Expo — not by us.
- **Optional dispatch skip (noise reduction, not correctness):** if the member currently has a
  live `chat` connection (`connection_service.query_by_owner` already exposes per-member
  `connectionType`), the backend may skip the send. The client handler (§6) is the authoritative
  suppressor regardless.

### 5. Deep-link (client)

- Register an `lightbulbai://agent/<agentId>?session=<key>` route (the scheme is OAuth-callback-only
  today — `lightbulbai://oauth-native-callback` in `sign-in.tsx`). Tapping a notification routes to
  `(app)/agent/[agentId]` and selects the session. Reuses the same deep-link plumbing the future
  Shortcuts/App-Intents item will need.

### 6. Foreground suppression (client)

- Backend always dispatches; the client's `Notifications.setNotificationHandler` suppresses the
  **banner** when the app is foregrounded *and* the current route is that agent's chat session
  (the reply already streamed live). Otherwise show it. No fragile backend "is-viewing"
  inference — the client knows its own route.

### 7. Connection observability — the teardown guard (v1, per approval)

The dispatch in §4 only fires if the backend↔container connection is alive at completion. It is,
whenever **any** client for the owner is connected. Under the "one phone + one desktop connected"
assumption the desktop keeps it alive, so v1 works with no lifecycle change. To also cover the
**phone-only send-then-close** case (no desktop), add a small guard:

- `remove_frontend_connection` schedules `_grace_close` 30s after the *last* frontend leaves
  (`connection_pool.py:1986`, `:2002`; `_GRACE_PERIOD = 30`), and that path **does not consult
  `_active_runs`**. Guard: **do not tear down while `_active_runs` is non-empty**, bounded by an
  existing parked-watchdog ceiling (~5 min). `_active_runs` is already populated by
  `mark_run_active` (`:267` / `:1737`) at dispatch and cleared on true terminal (`:1285-1290`).
- Effect: a run in flight keeps the container connection alive until `run_ended`, so the push
  fires for a phone-only user. ~20 lines, one file. **Accepted residual:** a run exceeding the
  ceiling with the app closed and no other client can still miss — negligible for v1.

---

## Error handling

- **Expo receipts / `DeviceNotRegistered`:** on this error for a token, `clear_push_token` prunes
  that `deviceId`. Handle both the immediate ticket errors and (best-effort) the receipt poll.
- **No tokens for the member:** no-op (user never granted permission or is web-only).
- **Expo API failure:** log + drop (fire-and-forget); never block or fail the run. Mirrors the
  `_record_usage_from_session` best-effort posture.
- **Permission denied on device:** client renders nothing intrusive; registration simply doesn't
  happen; re-checked on next launch.

---

## Testing (evidence-first)

### Backend (pytest, `apps/backend/tests/`)
- `user_repo.set_push_token` / `clear_push_token`: init-map-if-absent, scoped update doesn't
  clobber `email`/`created_at`, prune removes only the one `deviceId`. (mock DDB)
- `POST /api/v1/devices`: auth required; writes under `auth.user_id`; rejects malformed tokens.
- Member resolution: given a sessionKey `agent:a1:userB`, dispatch targets `userB`'s tokens; a
  keyless/personal session does **not** dispatch in v1.
- Preview: `_extract_assistant_text` truncation to ~120 chars, unicode-safe.
- `push_service`: builds correct Expo payload; batches multiple tokens; on `DeviceNotRegistered`
  in the response, calls `clear_push_token`; on transport error, swallows + logs.
- Teardown guard: with `_active_runs` non-empty, `_grace_close` does not close; once cleared,
  normal grace applies. (unit around the lifecycle helpers)

### Mobile (`apps/mobile`, vitest — single-file per feedback, RAM-heavy)
- `PushRegistration`: permission-granted path POSTs once; caches token; no re-POST when
  unchanged; no-op when permission denied.
- Notification handler suppression matrix: foreground+on-session → suppressed; foreground+other
  route → shown; background → shown.
- Deep-link parse: `lightbulbai://agent/a1?session=s1` → correct route params.

### On-device (manual, exit gate)
- Send from phone → background app → push arrives with agent name + preview → tap → lands on that
  agent's chat at the session. Repeat phone-only (desktop closed) to exercise the teardown guard.

CI: `mobile-ci.yml` (typecheck/lint/expo-export) must stay green; run `tsc --noEmit` after adding
tests (frontend CI typechecks tests). Backend tests run in the backend suite.

---

## Rollout / infra

- **CDK (`apps/infra`):** Expo access-token secret in Secrets Manager + IAM read for the backend
  task; `aps-environment` entitlement flows through the Expo config plugin + EAS credentials, not
  our CDK. New `POST /api/v1/devices` needs no gateway change (ordinary REST on the ALB path).
- **EAS:** a new native build (adds `expo-notifications` + entitlement) — a store/TestFlight
  build, not an OTA update. Register the APNs key with EAS (`eas credentials`).
- **Migration:** none — `push_tokens` is a lazily-created map attribute; absent = no push.

## Docs to update on implementation
- `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md` — mark Phase 7 Task 1/2 as specced
  here; record the Expo-Push-API (not bare APNs) decision.
- `apps/mobile/README.md` — push setup + the new native-build requirement.
- (Separate, unrelated) `CLAUDE.md` Cloud Map drift (lines 207/312/464 say `describe_tasks`;
  code uses `servicediscovery.discover_instances`) — tracked outside this spec.

---

## Decisions (resolved in brainstorm)

- **Expo Push API, not bare APNs** — hosted backend + Expo-managed app make the relay/attestation
  OpenClaw needs unnecessary; Expo fronts APNs and manages the key.
- **`users` row, not a new table, not `containers.paired_devices`** — push is a *delivery*
  address scoped to a *human* (member); `paired_devices` is an *auth* structure scoped to an
  *owner/container* for the WS authorizer. Different job, scope, trust, and id derivation; the
  1:N concern is handled by the map, matching the `paired_devices` *shape* without fusing the
  *storage*.
- **Reply-preview body + tap-into-that-agent's-chat** (vs generic text) — user's own content;
  iOS provides OS-level lock-screen preview hiding.
- **Teardown guard in v1** — makes phone-only delivery robust for ~20 lines.

## Open questions
- None blocking. (Expo `projectId` for `getExpoPushTokenAsync` is the one in `app.json`
  `extra.eas.projectId`.)
