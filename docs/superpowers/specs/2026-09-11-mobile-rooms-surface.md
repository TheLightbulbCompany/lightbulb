# Mobile rooms surface — design (2026-09-11)

> Short spec for the Expo iOS app (`apps/mobile`) catching up to the messaging
> workspace. Disposable per repo policy: delete when the plan that argues from
> it ships. Companion plan: to be written after the owner settles the three
> open decisions at the end.

## Summary

The product is now "a messaging workspace where AI agents are members". On
the phone the app still shows the pre-pivot shape: an agent roster on the
Chat tab and a per-agent conversation. Under the hood that conversation
screen (`src/app/(app)/agent/[agentId].tsx`) already resolves or creates a
`dm` room through `createMessagingClient`, reconciles history with
`listMessages`, and renders the live run over `room.run.frame`. So the gap is
not the protocol, it is the surface: there is no list of rooms, no way to
open a channel or group DM, no `@` mention, no read marking, and a push tap
cannot land on a room.

This spec reuses that screen and the shared package, and deliberately ships
the phone at "read, reply, mention, get notified" parity, not full Slack
parity.

## Goals

- The Chat tab lists the same rooms the web sidebar shows (channels, DMs,
  group DMs) with unread counts, newest activity first.
- One room screen handles every room kind: multi-author messages with
  author names and avatars, agent run status inline, images, replies-in-thread
  shown inline as quoted context (no thread panel).
- Composer with `@` typeahead over room members (humans and agents), sending
  structured `mentions` exactly like the web.
- Read marking on open and while focused; the tab badge is the unread sum.
- A push tap opens the room (and scrolls to the message when it is loaded).
- Everything degrades: no live connection → HTTP history and a banner; no
  push permission → the app still works.

## Non-goals (this round)

Threads panel, reactions UI, edit/delete, search, drafts, sections and
stars, room creation and member management (web does those), typing
indicators, file attachments beyond images, Android.

## What moves into `packages/shared`

- The room-list and room-state machine: a platform-neutral `useRooms` cut
  from `apps/frontend/src/hooks/useRooms.ts`. The web hook stays as a thin
  wrapper. The three things that block a direct move are all injectable:
  `@clerk/nextjs` → the caller passes `userId`/`orgId`; `swr` → shared already
  depends on it or the hook takes a fetcher; DOM focus/visibility in
  `markVisibleRead` → an injected `isVisible()` (web: `document`; mobile:
  `AppState`). `uploadAttachment(File)` widens to `Blob | { uri, name, type }`.
- Message merge/ordering helpers currently local to the mobile screen
  (`mergeMessages`, `lastSequence`) and the web's `frameText`/`runView` in
  `MessagingWorkspaceContext.tsx` become one shared `roomTimeline.ts`.
- Mention token parsing from `RoomComposer.tsx` (`sameMember` and the send-time
  resolver added by #2500/human-mentions) becomes shared `mentions.ts`, so
  both composers resolve `@handle` identically.

Nothing else is shared. Rendering stays per platform.

## Mobile surface

- **Chat tab → Rooms list.** `src/app/(app)/chat.tsx` replaces the agent grid
  with a `FlatList` of rooms: kind glyph, name (`roomDisplayName`), last
  activity time, unread count bold. Sections: Channels, Direct messages
  (group DMs inside). The existing `AgentPickerSheet` stays as the "New
  message" affordance and routes to `selectAgentDm`.
- **Room screen.** `agent/[agentId].tsx` is renamed to `room/[roomId].tsx`
  (keep an `agent/[agentId]` redirect that resolves the DM room, so old push
  payloads and deep links still work). It renders `CanonicalRoomMessage`
  items with author name/avatar when the author changes, the existing
  `MessageMarkdown`/`ToolPill`/`AgentHead`, image attachments through
  `downloadAttachment`, and the live run block driven by the shared timeline
  helper. `markRead` on mount, on `AppState` active, and when the newest
  message arrives while the screen is focused.
- **Composer.** The current input gains the `@` typeahead (a `FlatList` above
  the keyboard, filtered on `mentionHandle`/display name) and sends
  `mentions` built by the shared resolver. Image attach stays as is.
- **Push.** `routeFromResponse` gains the `roomId` branch first:
  `/(app)/room/{roomId}` with `messageId` as a param; the foreground handler
  suppresses the banner when that room is on screen (`shouldSuppressBanner`
  gets a `roomId` case beside the agent/session one).
- **Badge.** The Chat tab badge is the sum of `unreadCount`; `setBadgeCount`
  mirrors it on the app icon when the app backgrounds.

## Notifications contract (already shipped or in flight)

- Projector push (#2509): title, body preview, `data.roomId/messageId/
  messageSeq/threadRootMessageId`, honours the room member's `muted`.
- Web notifications (#2510) are the desktop counterpart; the phone gets the
  push regardless of desktop state (no delay, by decision).

## Rollout

1. Merge #2349 so a TestFlight build can be signed; cut `mobile-v0.4.0` from
   the first PR that lands the rooms list (OTA cannot carry the route rename
   across a native fingerprint change, so a store build is required anyway).
2. Ship in two PRs: (a) shared extraction + web wrapper, no behaviour change,
   CI green on web; (b) mobile surface.
3. Verify on a device against dev with the e2e account: open a channel from a
   push, `@` a human, receive a push for a DM with the app killed.

## Open decisions (owner)

1. **Keep the agent roster anywhere?** Recommended: no; "New message" opens
   the picker, and agents are reachable through Control → Agents as today.
2. **Threads on the phone:** inline quoted context only (recommended) or a
   real thread screen now.
3. **Scope of the shared extraction:** move `useRooms` wholesale behind an
   adapter (recommended, one PR, web wrapper keeps the web contract) versus
   duplicating a smaller hook in mobile and converging later.
