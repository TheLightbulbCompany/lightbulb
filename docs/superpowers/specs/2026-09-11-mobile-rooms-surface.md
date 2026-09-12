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

## Mobile surface — Slack's shape (owner decision 2026-09-11)

The owner's ruling: copy how Slack's iOS app does it. That settles the three
questions that were open in the first draft (no agent roster; a real thread
screen; one shared room machine) and fixes the information architecture.

**Tabs (bottom bar):** Home · DMs · Activity · You. Search rides in the Home
header. "Later" is not copied.

- **Home.** The conversation list with collapsible sections Starred ·
  Channels · Direct messages (group DMs inside DMs), a search field in the
  header, and the pencil button for a new message. Row anatomy copies
  Slack: `#` or lock glyph for channels, avatar or stacked avatars for DMs,
  name, unread rows bold with a count badge for DMs and mentions, muted rows
  dimmed with no badge. Swipe right marks read/unread; swipe left opens
  Mute and Star. Sort is newest activity first within a section.
- **DMs.** The same rows filtered to `dm` and `group-dm`, with the other
  party's avatar, sorted by recency.
- **Activity.** Mentions of you and replies to threads you posted in, newest
  first, each opening the message. Phase B.
- **You.** The existing Settings screen.
- **New message (pencil).** Pick one or more people or agents; a single agent
  or person resolves to the existing DM (`selectAgentDm` / `selectDm`), more
  than one creates a group DM. The pre-pivot agent roster (`chat.tsx` grid)
  is retired; agents remain reachable under Control → Agents.

**Conversation screen** (`src/app/(app)/room/[roomId].tsx`, generalised from
today's `agent/[agentId].tsx`, which keeps a redirect so old push payloads
and deep links still resolve their DM room):

- Header: kind glyph and name, member count; tapping opens the conversation
  details sheet (members, notification setting, mute, leave for channels).
- Timeline: messages grouped by author with avatar and name on the first of
  a run, time on long-press, day dividers, a "New messages" line at the last
  read sequence, a jump-to-latest pill when scrolled up, images through
  `downloadAttachment`, the live agent run block (`ToolPill`, streaming text)
  driven by the shared timeline helper. A message with replies shows a
  "N replies" row that opens the thread screen.
- Long-press on a message: reaction bar (the shipped fixed set plus "more")
  and an action sheet — Reply in thread, Copy text, Mark unread, Copy link.
  Edit and Delete for your own messages (Phase B).
- Read marking: on open, on `AppState` active, and when the newest message
  arrives while the screen is focused, mirroring the web's `markVisibleRead`.
- Composer: multiline text, `@` typeahead sheet listing members with avatar,
  `@handle` and display name (humans and agents; the shared resolver builds
  `mentions`), `:` emoji typeahead, attach photo, send button; keyboard-aware.

**Thread screen** (`room/[roomId]/thread/[messageId].tsx`): the root message
pinned at the top, replies below in the same item renderer, its own composer
that sends with `replyToMessageId`/`threadRootMessageId`, and Slack's "Also
send to #channel" toggle omitted for this round.

**Notifications, copying Slack's rules:**

- Push title "Name in #channel" for channels, "Name" for DMs; body is the
  preview; grouped per conversation by the OS using `threadIdentifier` =
  roomId. Tapping lands in the conversation and scrolls to the message once
  loaded; a thread reply lands in the thread screen.
- The in-app banner is suppressed only when that conversation is on screen
  (`shouldSuppressBanner` gains a `roomId` case beside the agent/session one).
- The app icon badge counts DMs and mentions only, never plain channel
  unreads — Slack's rule, and the projector already knows which is which
  because its `notify` decision is exactly "DM, mention, or your thread".
  The client mirrors the same rule for the Home tab badge.
- Per-conversation setting: All new messages / Mentions only / Nothing, plus
  Mute, stored on the room member row (Phase 3's `muted` today; the
  three-way setting is Phase B and needs one backend field).

**Phases.** A: tabs, Home and DMs lists, conversation screen, thread screen,
composer with `@`, read marking, push routing, badges. B: Activity tab,
per-conversation notification setting, swipe actions, conversation details
sheet, edit/delete own messages, reactions beyond the fixed set.

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

## Decisions (settled 2026-09-11)

The owner ruled "copy how Slack does it", which resolved the first draft's
three questions: the agent roster is retired; threads get a real screen; the
room state machine moves into `packages/shared` behind an adapter with the
web app as a thin wrapper.
