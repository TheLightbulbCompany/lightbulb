# Slack-parity messaging — design

**Status:** approved direction (2026-09-11), spec for review
**Owner:** Claude (messaging lane). Codex keeps onboarding, workspace identity, billing.
**Supersedes:** `docs/superpowers/plans/2026-09-11-slack-like-essentials.md` (Codex; its two open items #2473 edit/delete and #2475 mark-unread merged 2026-09-11 and are the baseline here).
**Reference:** the owner's Slack-style mock (channel `#roadmap`, sidebar sections, hover actions, file cards). Style is settled — the white/cream shell, Source Sans, borders-only, 4px rhythm stay. This spec is about function, feel, and how each part is presented.

## Summary

Make Lightbulb's messaging feel like Slack to use — hover actions, grouped messages, inline `@` typeahead, per-room drafts, sections and stars, pins and topics, keyboard navigation — without changing the product model: humans and agents are coworkers in the same rooms, and the right panel is thread / details / linked work. Files, public channels, and human presence come last because they need backend work; everything before them is frontend over the existing canonical room contract.

The one thing Slack cannot do and we can — an agent that owns a filesystem and a task board handing you the actual artifact inside the message — is the destination signature (**the handoff card**), scheduled for Phase 4. Today's `RoomContext`/`contextIds` plumbing in `RoomPanel` has no producer and is treated as scaffolding, not capability.

## Goals

- Every everyday Slack gesture works and is discoverable: react, reply in thread, edit/delete (shipped), copy link, pin, star, mute, mark unread (shipped), jump to unread, quick-switch rooms, format text, mention by typing `@`.
- The message list reads like a conversation, not a form: author grouping, date dividers, a "New" line, hover timestamps, auto-scroll with a "new messages" pill, scroll-up history.
- The sidebar is the owner's: sections, stars, drafts, unread-first, collapsible groups.
- Agents stay coworkers. The UI states plainly which agents will respond before you send.
- Ship in reviewed batches to prod; nothing fake, nothing that the backend does not enforce.

## Non-goals

- No persistent agent/session panel beside the room (decided: rooms only).
- No global search (per-room search shipped in #2472; workspace-wide search is a later program).
- No rich-text editor dependency; the composer stays a markdown textarea with a toolbar and shortcuts.
- No notification-preference matrix beyond mute (per-room mute is in scope; keyword/schedule prefs are not).
- No mobile IA change. Every feature must degrade at ≤859px (touch: long-press / overflow menu instead of hover).
- No Slack purple, no bot chrome, no colour literals outside `globals.css`.

## Current state (2026-09-11, `main`)

Shipped: channels, DMs, group DMs, create dialog, add/remove members, rename, listen modes, markdown messages, image attachments (≤4, paste), persisted reactions (fixed set of 6), threads in the right panel, live agent runs with tool trace + Steer/Stop, typing, desktop notifications, per-room search, participant profiles, edit/delete own messages with tombstones (#2473), mark unread + next-unread navigation (#2475), and a hover/focus message toolbar — add-reaction dropdown (the six), reply in thread, own edit/delete menu; applied reactions render as chips; touch pointers keep the controls visible (#2476, `RoomMessageContent` + `.room-message-actions`).

Known gaps the reference makes obvious: every message repeats avatar + name; no date dividers; no "New" line; drafts are wiped on room switch (`MessagingWorkspaceContext` resets `draft` when `activeRoomId` changes); mention picker is a checkbox list behind an `@` button; `@handle` in text renders as plain text; history paging exists in the hook but nothing scroll-triggers it (`onLoadHistory` is only called from search); no auto-scroll / jump-to-latest; no stars, sections, mute, topic, pins, leave; no permalinks; no keyboard navigation; reactions limited to six with no picker; no quick reactions, copy link, or pin in the toolbar.

Dead scaffolding: `RoomContext`, `contexts`, `contextIds` (no producer). The `Tasks` tab opens the Tasks panel.

## Design direction

The world is established (`apps/frontend/.interface-design/system.md`, tokens in `globals.css`): cream ground, midnight ink, `--edge` hairlines, `--brass`/`--bulb` amber only for live agent work, state colours never as fills. Phase 0 documents it as `DESIGN.md` so subagents build inside it.

**Signature:** the handoff card — a message carries a real artifact (file, task, approval) as a compact card that opens in place. Phase 4. Until then, the signature the surface already has stays the only expressive element: the agent activity halo and the live run block.

**Defaults rejected →**
- six fixed reactions in a menu → full emoji picker with quick reactions in the shipped hover toolbar
- avatar + byline on every message → grouping by author within 5 minutes, date dividers, "New" line
- checkbox mention picker → inline `@` typeahead; mention chips in messages
- drafts reset on room switch → per-room drafts and a Drafts row in the sidebar
- Slack's tinted rail → cream stays; agents keep the quiet `Agent` label

## Surface anatomy — what changes where

### Sidebar (`WorkspaceSidebar` → `RoomDirectory`)

- Order: **Starred** (if any) · **Channels** · **Direct messages** · **Drafts** (if any) · **More**.
- Sections: owner-defined named groups of rooms (Slack "custom sections"), collapsible, reorderable by drag (`@dnd-kit/sortable` is installed). Default groups stay when no custom sections exist.
- Row anatomy: icon (`#` channel, lock icon reserved for Phase 4 private/public, avatar for DM, stacked avatars for group DM) · name · muted rooms render in `text-muted-foreground` with no badge · unread rooms bold with count · draft rooms show a pencil glyph · active row `--cream-deeper`.
- Row overflow menu (hover / focus / long-press): Star, Mute, Mark unread (shipped), Move to section, Copy link, Leave (channels only, not owner).
- Sort option in the group header: alphabetical (default) or unread first.
- Quick switcher `⌘K`: rooms + people + agents, fuzzy, Enter opens (reuses the existing command palette shell if one is mounted; otherwise a `Dialog` + list).

Persistence: `starred`, `muted`, `sectionId` are per-user, per-room and live on the existing `MEMBER#human#<id>` row (`PATCH /messaging/rooms/{id}/me` with those three optional fields; sections themselves — `{id, name, order}` — are a per-user list on the user's stream/prefs row via `PUT /messaging/me/sections`). Drafts are local (`localStorage`, keyed by owner scope + room + thread).

### Room header

- Left: kind icon + name · **topic** in muted text after a middle dot (click to edit for owner/admin; Phase 3 adds `topic` to `PATCH /messaging/rooms/{id}`).
- Right: search (shipped) · member facepile (up to 4 + count; opens Members tab) · star · bell (mute toggle with state) · overflow (Copy link, Rename, Leave, Details).
- Tabs stay: Conversation · Details (was "Context") · Tasks. Pinned count appears on Details.

### Message list

- **Grouping:** a message collapses its avatar and byline when the previous visible message has the same author, same thread level, no date divider between, no run block between, and is ≤5 minutes older. Collapsed messages show the time in the left gutter on hover/focus.
- **Date dividers:** "Today", "Yesterday", weekday for the last 7 days, otherwise the full date; sticky while scrolling.
- **"New" line:** rendered above the first message with `messageSeq >` the read cursor when the room opens with `unreadCount > 0`; stays until the owner sends a message or leaves the room. Mark-unread (#2475) re-derives it.
- **Hover/focus toolbar** (shipped in #2476 as `.room-message-actions`; this extends it, no second toolbar): quick reactions (last 4 used) · add reaction (opens the emoji picker instead of the six-item menu) · reply in thread (shipped) · copy link · more (edit, delete — shipped — pin/unpin, mark unread from here). Touch keeps the shipped always-visible compact controls.
- **Reactions:** rendered as chips only when present; the owner's own reactions are outlined; click toggles. Full emoji picker: a small in-repo picker (curated set ~400, search by name, recent row) — no new dependency.
- **Mentions:** `mentions[]` + text tokens render as chips (`@friday`, `@Aden Kim`); own mention gets a highlight background on the message row.
- **Permalinks:** `/chat?room=<id>&message=<id>` (extends the existing URL channel); opening one selects the room, loads history around the message if needed, scrolls and highlights it (the search-target path already does the scroll/highlight).
- **Auto-scroll:** pinned to bottom when within 48px of the end; otherwise a "N new messages ↓" pill; own sends always scroll to bottom; run frames follow the same rule.
- **History:** today a room loads its whole history forward from seq 0 in 100-message pages (`useRooms.reconcile(0)`); there is no backward pager in the API (`GET …/messages?afterSeq`). That is acceptable for founder-scale rooms. Backward paging (`beforeSeq` on the list endpoint + a top sentinel that preserves scroll position) is a Phase 3 backend item, added only if a real room exceeds ~500 messages.
- **Agent runs:** the live run block stays; when a run ends without a visible reply the block collapses to one muted status line ("Friday finished · no reply", "Friday stopped"). This is common by design: an ambient `room_event` turn may only speak through the message tool and otherwise leaves no trace. Tool traces stay behind a disclosure.
- **Edit/delete, tombstones:** as shipped in #2473; the action bar is the only entry point.

### Composer

- Markdown textarea with a **toolbar**: bold, italic, strikethrough, ordered/unordered list, inline code, code block, link. Toolbar buttons and `⌘B / ⌘I / ⌘⇧X / ⌘⇧C` wrap the selection with markdown. Preview is what the message renders.
- **`@` typeahead:** typing `@` opens a popover of room members (people and agents, agents first when in a DM with one agent), filters as you type, arrow keys + Enter/Tab insert `@handle` (agents) or `@Display Name` (people) and add the structured mention. Escape closes. Replaces the checkbox picker; the `@` button opens the same popover.
- **`:` emoji typeahead** after two characters; the picker button inserts at the caret.
- **Attachments:** images as shipped, plus drag-and-drop onto the composer and the message list. (Any-type files: Phase 4.)
- **Drafts:** per room and per thread, restored on return; the Drafts sidebar row lists rooms with a non-empty draft.
- **Who will respond** — a one-line hint under the field whenever the room has agents, derived only from room kind, listen modes, the draft's structured mentions, and live run state, so it can never disagree with `select_dispatch_targets` (`core/services/messaging.py`):
  - `observe` → "Atlas is observing" (never runs, even when mentioned — in any room kind).
  - DM → every other agent "will reply" (listen mode is ignored in a DM).
  - Channel or group DM (group DMs are *not* DMs to the dispatcher) → `active` agents "will reply"; `mentions` agents "reply when mentioned", switching to "will reply" the moment the draft carries a structured mention of them; unmentioned `mentions` agents get nothing.
  - An agent with a live run in this room → "Friday is working — sending now steers that run" (a second `user_request` mid-run is steered into the live run, not queued).
  - Compact form: "Friday will reply · Gauge when mentioned · Atlas observing".
- **Typed mentions resolve on send.** The backend only honors the structured `mentions[]` for humans and agents alike; the plugin parses `@handle` text for agents only. So the composer resolves any typed `@handle` (agent) or `@Display Name` (human) token that matches a current room member into a structured mention before sending — a typed `@friday` must work exactly like picking Friday from the typeahead. Unmatched tokens stay plain text and the hint does not promise a reply.
- **Keyboard:** Enter send · Shift+Enter newline · `↑` in an empty composer edits your last message (#2473) · `Esc` closes typeahead, then thread/overlay.

### Details / thread panel

- Tabs inside Details: **Members** (shipped, incl. add/remove/listen mode) · **Pins** · **Files** (messages with attachments, from loaded history) · **Links** (URLs extracted from loaded history) · **About** (topic, created, leave).
- Thread panel: shipped; adds the same hover action bar and grouping rules; the thread root shows "N replies · last reply 12m".
- Pins: any member can pin/unpin; `pinnedMessageIds` on room META (cap 50) via `PUT/DELETE /messaging/rooms/{id}/pins/{messageId}`; Pins tab fetches missing messages by id.

### Keyboard map (workspace-wide)

`⌘K` quick switcher · `⌥↑ / ⌥↓` previous/next room · `⌥⇧↑ / ⌥⇧↓` previous/next unread (extends #2475) · `Esc` close thread/overlay/typeahead · `⌘/` shortcut sheet.

## Phases and acceptance

Each phase is one or more PRs, each PR: focused unit tests, `node scripts/brand-tokens-scan.mjs src` = 0, impeccable `detect` clean on changed files, one screenshot round at 1440 and 390, finish-review subagent, merge, dev check, prod dispatch (`gh workflow run deploy.yml --ref main` when the deploy queue reads 0), founder check on flickmeon.ai.

**Phase 0 — Foundation (1 PR).** `DESIGN.md` written from the shipped world (impeccable `document`); baseline `critique`; initial scroll-to-bottom + auto-scroll + "new messages" pill; delete Codex's superseded plan file. *Accept:* opening a long room shows the newest message; new messages while scrolled up show the pill and do not yank the viewport; your own send scrolls to the bottom.

**Phase 1 — Message list feel (2 PRs).** Emoji picker + quick reactions + copy link in the shipped toolbar (pin joins it in Phase 3 with its backend); grouping; date dividers; "New" line; hover timestamps; mention chips; permalinks; run status line. *Accept:* any emoji can be reacted with in ≤2 clicks; consecutive Friday messages collapse; a permalink opens and highlights the message; keyboard users can reach every action.

**Phase 2 — Composer (2 PRs).** `@` and `:` typeahead; toolbar + shortcuts; drag-and-drop images; per-room/thread drafts + Drafts row; who-will-respond hint; `⌘K` switcher; `⌥` room navigation; shortcut sheet. *Accept:* typing `@fr` + Enter mentions Friday and adds the structured mention; leaving and returning restores the draft; `⌘K` reaches any room in ≤3 keystrokes.

**Phase 3 — Sidebar, header, details (2–3 PRs, small backend).** Stars, mute, sections (+ drag reorder), unread-first sort, row overflow menu, leave channel, topic, facepile, bell, pins + Pins/Files/Links/About tabs, member-prefs and sections endpoints. *Accept:* a starred room survives a reload on another device; a muted room never notifies or badges; a pinned message appears in Pins for every member.

**Phase 4 — Backend-shaped (separate plans, in this order).** (a) Any-type file attachments + handoff cards that open in the file-viewer panel, including agent-authored files; (b) public channels: `visibility` on META, channel browser, self-join, lock/`#` icons; (c) human presence: server-owned TTL presence record published over the existing live channel, dots in DM rows and facepiles; never inferred from message recency. Each gets its own short spec before build.

## Backend touches (all mine, all small until Phase 4)

| Phase | Change |
|---|---|
| 3 | `PATCH /messaging/rooms/{id}/me` — `starred`, `muted`, `sectionId` on the caller's `MEMBER#` row; projector publishes `room.member.updated` for the caller only |
| 3 | `PUT /messaging/me/sections` — ordered `{id,name}` list on the caller's user stream row |
| 3 | `PATCH /messaging/rooms/{id}` gains `topic` (≤250 chars, owner/admin) |
| 3 | `PUT/DELETE /messaging/rooms/{id}/pins/{messageId}` — member-authorized, cap 50, `room.updated` live event |
| 3 | `POST /messaging/rooms/{id}/leave` — non-owner humans; owner must transfer or delete first (delete stays out of scope) |
| 4 | file attachments, visibility/browse/join, presence — own specs |

Mute is enforced client-side for badges and notifications only; the server keeps sending events (an agent's dispatch never depends on mute).

## States and accessibility

Every new control has hover, focus-visible, disabled, pending, and error states; every list has loading, empty, and error states; every status keeps its word or icon (no colour-only). Popovers (typeahead, emoji, overflow) are `radix-ui` Popover/DropdownMenu with roving focus and Escape. Action bars are reachable by keyboard (Tab into the message article). Reduced motion disables the pill slide and halo. Touch targets ≥44px at ≤859px. Screen readers: dividers are `role="separator"` with the date as text; the "New" line is `role="separator"` "New messages"; grouped messages still expose author via `aria-label`.

## Coordination

- Codex finished #2473/#2475 and starts no new messaging work. Files under `components/messaging/**`, `hooks/useRooms.ts`, `components/workspace/{WorkspaceSidebar,WorkspaceShell,WorkspaceHeader}.tsx`, `components/notifications/**`, `packages/shared/src/{messaging,live}/**`, backend `routers/messaging.py`, `core/services/{messaging,openclaw_work}.py`, `core/repositories/messaging_repo.py`, `apps/infra/lambda/messaging-projector/**`, the `lightbulb-channel` plugin, and `openclaw-version.json` are Claude-owned for this program.
- The bundled `lightbulb` skill (`apps/infra/openclaw/skills/lightbulb/references/surfaces.md`) is updated in the phase that changes what an owner can be told to click (Phases 1–3).
- This spec and its plan are deleted when Phase 3 ships; Phase 4 specs replace them.

## Open decisions (builder must not invent)

1. ~~Human mention token in text~~ — resolved: `@Display Name`, resolved client-side to a structured mention (the plugin never parses human tokens from text).
2. Sections storage row: user stream item vs a new `PREF#` item — decide in the Phase 3 plan after reading `messaging_repo.ensure_user_stream`.
3. Whether `⌘K` reuses the Teams command palette or a messaging-local dialog — decide in Phase 2 after checking what the palette indexes.
