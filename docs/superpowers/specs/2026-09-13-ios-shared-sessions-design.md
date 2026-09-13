# iOS shared sessions — design

**Date:** 2026-09-13
**Status:** approved in chat (scope "watch + send", entry "header action"); spec for the implementation plan.
**Disposable:** delete this file in the PR that ships it (CLAUDE.md rule).

## 1. Goal

Shared Claude Code / Codex sessions (PR #2565) are visible today only in the web app and the Tauri desktop webview. The iOS app has no UI for them. This adds, to the phone:

1. A **Devices & sessions** screen per room: which Macs and shared sessions the room's members have exposed, with status.
2. A **session transcript** screen that renders a shared session in the same terminal grammar the web uses (prompt, prose, thinking, tool rows with expandable output and diffs, turn markers, attribution, "Working…").
3. A **prompt row** on that screen to send a follow-up into the session, with the same receipt states the web shows.

Non-goals this pass: interrupt/steer, approvals, universal-link handling of the web `?panel=session` permalink (follow-up), sharing enablement (that lives on the Mac).

## 2. Approach

**Share the model, build the views natively.** The transcript model (`buildRows`, `stripAnsi`, `toolHeader`, `fullOutput`, `diffLines`, glyphs, lifecycle labels) and the two data hooks (`useRoomResources`, `useLocalSessionHistory`) have no DOM dependencies. They move into `@lightbulb/shared` and the web imports them from there. iOS gets two pushed routes in the room stack plus a header action. No WebView, no duplicated model.

Rejected: duplicating the model in `apps/mobile` (two copies of a grammar that already drifted once), and a WebView of the web pane (auth handoff, no native feel, double render path).

## 3. Shared package changes (`packages/shared`)

### 3.1 `src/messaging/localSessionTranscript.ts` (new)

Moved verbatim from `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx`, exported:

- `type Provider = "claude" | "codex"`
- `type TranscriptRow` (the web's `Row` union, renamed so the barrel does not export a bare `Row`)
- `GLYPH` (`prompt`, `bullet`, `output`, `thinking`, `working` per provider)
- `buildRows(events): TranscriptRow[]`
- `stripAnsi(text)`
- `toolHeader(provider, call)`
- `fullOutput(result): string`
- `diffLines(result): string[]`
- `lifecycleLabel(row): string` — the switch currently inline in `NativeSessionTranscript` (`Worked for 12s` / `Interrupted` / `Context compacted` / `Waiting for approval on the owner's Mac` / `Turn started` / `""`), including `formatMs`.
- `toolRowView(row): { status, preview, full, diff, more, showOutput, truncated }` — the derivation block at the top of the web `ToolRow` (status, preview/full text, `more` line count, `showOutput`, `truncated`), so both renderers draw from one computation. The web `ToolRow` keeps only JSX and its `open` state.

Tests: the existing web tests that cover these helpers move alongside (`packages/shared/src/messaging/localSessionTranscript.test.ts`, vitest). Web tests that exercised them through the component keep passing unchanged.

### 3.2 `src/messaging/localSessionLabels.ts` (new)

`sessionLabel(session)`, `sessionStatus` (record), `unavailableReason` (record, currently `unavailable` in `LocalSessionPanel.tsx`), `canSendToSession(session): boolean` (the `canSend` expression in `LocalSessionPanel`). Web imports them; `LocalSessionPanel` keeps re-exporting `sessionLabel`/`sessionStatus` for `RoomResources.tsx`.

### 3.3 `src/messaging/useRoomResources.ts` (moved)

`useRoomResources`, `useLocalSessionHistory`, `accessLost`, `RoomResourceSource` move from `apps/frontend/src/hooks/useRoomResources.ts` (drop the `"use client"` pragma; the package has none). Importers are repointed (five web files plus `hooks/__tests__/useRooms.test.tsx`), and the web file is deleted. `useRooms` already returns `resourceSource: { client: messaging, onEvent, connectionRevision }`, so both apps get a ready-made `RoomResourceSource`.

One addition: `useRoomResources(roomId, source, { enabled = true } = {})`. When `enabled` is false the mount effect returns before subscribing or polling (and clears `data`/`loading` untouched — the last snapshot stays). On the phone three screens of one room can be mounted at once (conversation → devices → session), and only the focused one should poll. Web callers pass nothing and behave exactly as today.

### 3.4 Barrel

`src/index.ts` exports everything in 3.1–3.3. The barrel comment's "held for the mobile port" note about `useRooms` stays as is.

## 4. iOS

### 4.1 Routes

Both live under the existing `room/_layout.tsx` nested `<Stack>`, so they push over the conversation like the thread screen does:

- `app/(app)/room/[roomId]/devices.tsx` — Devices & sessions.
- `app/(app)/room/[roomId]/session/[sessionId].tsx` — one session's transcript.

Names deliberately avoid "sessions": `(app)/control/sessions.tsx` is the OpenClaw gateway sessions screen.

Each route mounts `useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused })` the way the thread route does, so the room's live subscription, `activeRoom.members`, and `resourceSource` are available. `useRoomResources(roomId, rooms.resourceSource, { enabled: focused })` runs on both screens (the session screen needs `revision`, `accessRevision`, the fresh `LocalSession` row for status/generation, and `revoke`).

### 4.2 Header action (conversation screen)

`RoomHeaderActions` gains an optional third button, left of star: Feather `terminal`, `accessibilityLabel="Devices & sessions"`, `onPress` → `router.push(\`/(app)/room/${roomId}/devices\`)`. When `liveSessions > 0`, a small count badge (`colors.green` disc, `colors.white` 10pt digit) sits on the icon's top-right; label becomes `Devices & sessions, N live`.

`liveSessions` = sessions whose `status` is `busy` or `waiting-approval`. The conversation screen computes it from `useRoomResources(roomId, rooms.resourceSource, { enabled: focused })`. That reuses the 5 s poll + `room.resources.changed` hint the hook owns, and stops while a pushed screen (thread, devices, session) has focus. ponytail: one extra `GET /rooms/{id}/resources` every 5 s while a room is open on the phone, the same cost the web room tab pays. If it shows up in traffic, gate it on `data.sessions.length > 0` after the first load.

### 4.3 Devices & sessions screen

Header title "Devices & sessions", `HeaderBack` to the conversation. Body is a `SectionList` grouped by human member (display name from `activeRoom.members`, fallback "Room member"), the same grouping `RoomResources.tsx` does:

- Section header: member name.
- Mac row: laptop icon, `Mac · <deviceId first 8>`; sub-line `Paired Mac + associated CLI · availability unknown` when paired nodes exist.
- Session row (pressable, `accessibilityLabel="Open <sessionLabel>"`): provider tag (`Claude`/`Codex`), title (`session.title` or first 8 of id), `cwdName` in mono if present, status dot (green busy, amber waiting-approval, grey idle/unknown, hollow offline) + `sessionStatus` text. Press → push the session route.
- "No shared local sessions." under a Mac with none.
- Paired CLI endpoints without a Mac: terminal icon, `Paired CLI endpoint · <id8>`.
- Footer note (same copy as web): pairing does not confirm availability.

States: loading (skeleton text "Loading devices and sessions…"), error banner with "Try again" (keeps last snapshot, notes staleness), empty ("No paired devices or shared sessions in this conversation."), access removed (`RoomBlockedState` `access-removed` style message and a back affordance).

### 4.4 Session screen

Header title `sessionLabel(session)`; subtitle line under the header (inside the body, first row): `sessionStatus[status]` · `providerVersion` · owner display name; when `workspace.overlap` add "Multiple sessions may overlap this checkout."

Body: a `FlatList` of `TranscriptRow` from `buildRows(history.events)`, `keyExtractor = seq`, non-inverted, scrolled to end after the initial load and whenever a new tail arrives while the viewer is within 80 px of the bottom (same `BOTTOM_SLACK` rule the conversation uses). A "Load older" header button when `history.hasOlder`; a "Jump to latest" pill when `history.hasNewer` or the user scrolled up.

Row rendering (`components/session/TranscriptRow.tsx`), all glyphs from `GLYPH[provider]`, glyph column in `fonts.mono`:

- `prompt`: glyph + text in mono on a faint `creamDeep` band; meta line "<name> · HH:MM" (owner name when `who` is null, else the member's display name or "Teammate").
- `prose`: bullet glyph + `MessageMarkdown` (existing component). Codex final prose draws the small `codex` label instead of a bullet, as on web.
- `thinking`: thinking glyph + italic muted `MessageMarkdown`, or "Thinking…" when empty.
- `tool`: header line (`toolHeader`), "running…"/"…" suffix while running, `⎿ Error` line on error, diff block with +/− tinted lines when `diff.length > 0`, else the preview/full block in mono with the output glyph, and a pressable "… +N lines" / "show output" / "collapse" toggle driven by `toolRowView`. Expanded state is per row (`useState` in the row component, keyed by seq via the list).
- `notice` and `lifecycle`: muted meta text from `lifecycleLabel`.
- Trailing `Working…` row with a live seconds counter when `busy` and the last event is not `turn_end`/`turn_aborted` (port of `Working`).

Live cadence: exactly what the hooks already do — `useRoomResources` polls the list every 5 s while enabled and reacts to `room.resources.changed`; `useLocalSessionHistory` loads "newer" on every `revision` bump and tails at 1 s while `status === "busy"`. Pause: the screen's `useFocusEffect` sets `focused`; the resources hook gets `enabled: focused`, and the history hook gets `{ busy: busy && focused }`, so a blurred or popped screen makes no requests. Backgrounding the app: the screen also subscribes to `AppState` (the same listener shape `useTypingReport` uses) and treats a non-active state as unfocused. Session-row status (dot, subtitle, prompt enablement) always reads the fresh row from `useRoomResources`, never a copy captured at push time.

### 4.5 Prompt row

`components/session/SessionPromptRow.tsx`: a `TextInput` (mono, multiline, placeholder "Send a follow-up to <Claude|Codex>…") plus a send button, above the keyboard via the same `KeyboardAvoidingView` + `useComposerKeyboardOffset` the conversation uses.

Enablement: `canSendToSession(session)`; when false, the row shows the reason instead of the input (`unavailableReason[controlUnavailableReason]`, or "Offline", or "This session does not accept remote input."). Locked while an attempt is `sending`/`uncertain`, or a receipt is `unknown` for the current generation.

Send: build `LocalSessionCommand` exactly as the web does (`requestId` from `expo-crypto` `randomUUID`, `expectedGeneration`, `kind: "send"`, `text`, `expectedTurnId` only when busy and `sendMode === "immediate"`); refuse > 8 KiB with the web's copy. Call `client.submitLocalSessionCommand`. Attempt state machine and copy are the web's (`sending` → `received` with receipt; 4xx → `rejected` with the 409 copy; 5xx/network → `uncertain`). Receipt line under the input uses the web's `receiptText` mapping and "Agent <id>: " prefix. Buttons: "Check delivery" (calls `getLocalSessionCommand`) when uncertain or receipt pending/dispatched/unknown; "Retry same request" when uncertain. No implicit resend.

The hook that owns this state (`useSessionCommand(client, roomId, session)`) is the web's attempt logic lifted out of `LocalSessionPanel` into `packages/shared/src/messaging/useSessionCommand.ts`, with `uuid` injected (the web passes `crypto.randomUUID`, the phone `expo-crypto`). Web `LocalSessionPanel` switches to it in the same PR so the two cannot drift. Size check uses `new TextEncoder().encode(...)` — available in RN 0.86 (Hermes) and jsdom.

### 4.6 Theme

Terminal surfaces use the existing tokens: `colors.cream` ground, `colors.codeBg` prompt band and output blocks with a `colors.border` hairline, `colors.ink`/`colors.mutedSecondary` text, `colors.muted` meta lines, `fonts.mono` for glyphs/prompt/tool output, `MessageMarkdown` for prose. Status dots: `colors.green` busy, `colors.warn` waiting-approval, `colors.muted` idle/unknown, hollow `colors.border` offline. Diff tints: `+` lines `colors.green`, `−` lines `colors.error`, context `colors.mutedSecondary`. Tool error line `colors.error`. No new colour literals.

## 5. Data flow

```
conversation screen ── useRoomResources(5 s poll + resources.changed) ──> badge count
        │ push
devices screen ── useRoomResources ──> sections; press ──> push
        │
session screen ── useRoomResources (row, revision, accessRevision, revoke)
               ── useLocalSessionHistory(client, room, session, latestEventSeq, revision, revoke, {busy})
               ── useSessionCommand(client, room, session) ──> submit / check receipt
```

All HTTP goes through the shared messaging client already mounted by the mobile `LiveEventsProvider`; no new endpoints, no new backend work.

## 6. Error handling

- 403/404 on resources or history → `revoke()` → both screens show "Access to these resources was removed." with back; a pushed session screen for a revoked session pops on the next `accessRevision` change if its row disappeared.
- Resource refresh failure → banner + last snapshot retained (hook behaviour).
- History load failure → "Could not load session history. Try again." row with retry.
- Command 409 → "Session changed or is offline. Refresh before sending again." and the row refreshes resources.
- Session removed from the list while open (sharing disabled) → "This session is no longer shared." with back.

## 7. Testing

- `packages/shared`: vitest. The `buildRows` block and the "adapter result shapes"/"small fixes" cases of the web `NativeSessionTranscript.test.tsx` that only exercise the model move to `localSessionTranscript.test.ts` against `buildRows`/`toolRowView`/`lifecycleLabel`; the component-level cases stay on web. New `useRoomResources.test.ts` covers `enabled: false` (no fetch, no poll) and the flip to enabled (fetches once). New `useSessionCommand.test.ts`: sending → received; 409 → rejected copy; network → uncertain, check delivery flips to received; oversize refused. The web `useRooms.test.tsx` case that renders `useRoomResources` keeps running with the import repointed.
- `apps/frontend`: existing `RoomResources.test.tsx`, `RoomResourcesDock.test.tsx`, `LocalSessionWork.test.tsx` and the remaining `NativeSessionTranscript.test.tsx` component cases keep passing with imports repointed. `LocalSessionPanel` behaviour (send, receipt copy, lock) stays covered by `RoomResources.test.tsx`.
- `apps/mobile` (jest-expo, RNTL), mocks in the style of `conversation.test.tsx` (expo-router, clerk, live-events, whole MessagingClient):
  - `RoomHeaderActions.test.tsx`: terminal button present, badge shows count, label carries count, press pushes the devices route.
  - `devices.test.tsx`: grouping by member, status text, empty/error/access-removed states, press pushes the session route.
  - `session.test.tsx`: a fixture event list renders prompt/prose/tool/thinking/lifecycle rows with the right glyphs; tool toggle expands; "Working…" shows when busy; prompt row disabled with reason when `controlUnavailableReason`; send success/409/uncertain paths render the right copy and lock state.
  - `TranscriptRow.test.tsx`: diff tinting and `(truncated)` marker.

## 8. Rollout

One PR, one branch (`feat/ios-shared-sessions`). CI: `mobile-ci.yml` (PR-only) covers typecheck/lint/jest/`expo export`; `frontend-ci.yml` covers the web repoint. **Merging to main ships a mobile OTA** (`mobile-ota.yml`, fingerprint policy) — no native module is added, so the OTA is deliverable to the current TestFlight binary. Verify on dev after merge by opening a room with a shared session from the enrolled Mac. This spec file is deleted in the same PR.
