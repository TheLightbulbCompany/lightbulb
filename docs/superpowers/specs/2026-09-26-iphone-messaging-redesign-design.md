# iPhone app redesign: a messaging app — design

Status: draft for owner review, 2026-09-26. Delete when all three parts ship.
Visual references (throwaway mockups): `.superpowers/brainstorm/73529-1790476059/content/`
(`chats-list.html` option C, `conversation-muse-v2.html`, `activity.html` option B).

## Goal

The iPhone app stops imitating Slack and becomes a messaging app in the iMessage /
Telegram / Signal family: one list of conversations with people, agents and Claude
Code / Codex sessions; bubbles; visible "typing…" / "working…"; sent/read state; and
an Activity tab for what needs you. The remote-session screen is rebuilt in the same
language (today it has broken icons, stray monospace and a crammed header), and
sessions stop being reachable only from Activity / More.

## Owner rulings (2026-09-26)

- Sessions are ordinary conversations in the Chats list (not a tab, not a separate screen).
- Tabs: **Chats · Activity · Teams · Settings** (Home + DMs merge into Chats; More folds into Settings).
- Chats list = option C: a **pinned strip** on top + one list below.
  - Pins default to the agents onboarded in the workspace (starter roster), and are
    customizable (pin / unpin / reorder). **Stored on the phone only.**
  - Running sessions in the strip: **optional** — a Settings switch "Show running
    sessions in pinned", default **on**.
- Conversation = **Muse-style bubbles for everything** (ref: Alex Cornell, Muse, 2026-09-24 —
  "an entity, not a tool"; one canonical thread; proactive agents need clear message boundaries).
  Agent bubbles are wide (≈94% of the column) so long formatted replies stay readable.
- Must match the current web/desktop look: **today's neutral tokens (no cream)**, the same
  avatar shapes, the same vendor marks.
- Activity = **grouped rows** (option B).

## Shared visual language (all parts)

- **Tokens:** `apps/mobile/src/config/theme.ts` as it is (mirrors web `globals.css`
  `[data-chat-theme]`): light panes `#FFFFFF`, plane `#EFEFEF`, raised `#F7F7F7`, edge
  `#E2E2E2`, ink `#1A1A1C`, soft `#52525A`, mute `#606069`, link `#1E52CC`, live `#186D48`,
  alert `#A83C29`; dark rail `#101012`, pane `#1E1E21`, raised `#27272B`, edge `#323236`,
  ink `#ECECEE`. Add any missing dark values to `theme.ts` from `globals.css`; no new colours.
  Contrast is checked against every plane a token sits on (same rule as web).
- **Type:** Source Sans 3 for content, Lora Medium for display titles (e.g. the large
  "Chats" title), system font for navigation chrome. Message body 16pt; metadata ≥ 12pt.
- **Avatars:** rounded squares (mobile `Avatar` already: `radii.md`/`radii.lg`), per-agent
  grey tint (`agentTint`, as web). State rings as web `.participant-avatar`: `running` = live
  green ring, `attention` = alert ring, `paused` = warn ring; presence dot bottom-right;
  terminal badge bottom-left when a human is sharing a session. Claude Code / Codex / other
  tools use the existing vendor marks (web `providerBrand.ts`; mobile `SessionToolIcon` must
  render those marks — no emoji, no SF-symbol fallbacks that render as boxes).
- **Bubbles:**
  - Mine: ink-filled (light) / light-filled (dark), right-aligned, max 78% width, 18pt radius.
  - Others (agents and humans): pane-coloured bubble with a 1px edge stroke on the plane
    background, left-aligned, max ≈94% width, 18pt radius; avatar + name on the first
    bubble of a run; consecutive bubbles from one sender group without repeating the avatar.
  - Hanging off a bubble (not bubbles themselves): tool-run chip ("Ran 5 tools ›"), sub-agent
    chip, "Thought ›", reactions, reply count, timestamp, read state.
  - **Working** = a live bubble at the bottom with animated dots and a caption from the run
    ("Rolling the fleet · 1m"); humans typing = the same bubble with dots only.
  - **Unprompted agent messages** (nightly, routines, finished sessions) carry a small label
    under the bubble ("nightly check-in", "routine", "session finished").
  - Markdown in bubbles: headings, lists, links, inline code; code blocks keep monospace in a
    raised well. No monospace anywhere else.
- **Motion:** native iOS transitions; dots pulse respects Reduce Motion.

## Part 1 — Tabs, Chats list, pins, sessions in the list

- `NativeTabs`: Chats (`message` SF symbol), Activity, Teams, Settings. Remove Home, DMs, More.
  Settings absorbs More's rows (Teams moves to its tab; Control, Plans, workspace switcher,
  integrations, marketplace, Help, Legal).
- **Chats screen:** large Lora title "Chats", native search bar, compose button (existing
  `NewMessageSheet`).
  - **Pinned strip:** horizontally scrolling rounded-square avatars (56pt) with names; state
    rings live. Default contents on first run per workspace: the starter agents in roster order.
    Long-press a chat row or pin → Pin / Unpin; long-press in the strip → reorder mode.
    Stored in local storage keyed `lightbulb.pins.<orgId>` (ids only). When the switch is on,
    running sessions appear at the start of the strip while running.
  - **List:** every room the user is in (channels, DMs, group DMs, agent DMs) and every
    workspace session (`useWorkspaceSessions`, ended sessions included — they are ordinary
    chats) merged, sorted by last activity. Row: avatar
    (with state ring), name, time, two-line preview. Preview shows "X is typing…" /
    "working… <caption>" in link colour when live — for EVERY row, via a compact
    `room.activity.changed` hint on each member's user channel (see Backend slices). Blue unread dot on the left; muted rows show
    a bell-slash. Swipe: Mark read / Mute / (sessions) Archive.
  - Empty and provisioning states reuse today's `ProvisionGate` / onboarding components.
- **Sessions:** the `/sessions` screen and More → "Shared sessions" are removed; session rows
  open `/session/[id]`. Push-notification taps keep routing there.
- Settings gets the "Show running sessions in pinned" switch.

## Part 2 — Conversation screen (rooms, DMs, agents, sessions)

- One screen component family for `room/[roomId]` and `session/[sessionId]`, sharing the
  bubble, chip, working-bubble and composer components.
- **Header:** back, avatar, name, status line (agent "working…" / "idle", human "online",
  session "on <owner>'s Mac · idle|working"), tap → details (existing `RoomDetails`; session
  details sheet = today's `SessionOverview` content).
- **Read state:** under my last message: "Sent" → "Delivered" (an addressed agent recipient's
  delivery cursor passed it, i.e. its runtime accepted the turn) → "Read" (another member's
  `lastReadSeq` ≥ the message's seq; for an agent, its run started). Group rooms: "Read by 2".
  Humans have no delivery signal, so a human DM goes Sent → Read. Sessions show their own
  follow-up receipt ("Queued on your Mac", "Delivered", …).
- **Composer:** "+" (attachments, existing), growing text field, send; sessions keep "Send a
  follow-up to Claude…" semantics (follow-ups queue on the Mac) — without monospace.
- **Session screen specifics:** header line replaces today's two grey lines; tool runs,
  sub-agents and thoughts render as chips under the reply (tap → expand inline); approvals
  still happen on the Mac (unchanged copy, moved into the details sheet); "Load older" is a
  standard pull/scroll-to-load.
- Existing features keep working inside bubbles: threads, reactions, message actions sheet
  (reply, react, copy, report), attachments, link previews, document cards, run blocks,
  catch-up summary, onboarding band, blocked state.

## Part 3 — Activity, Teams, Settings

- **Activity:** grouped inset rows (same row component as Chats). Sections: "Needs you"
  (approvals, sessions waiting, login requests) then Today / Earlier. One action button per
  row ("Review", "Open") opening the existing sheets (`PickerSheet`, `ReasonSheet`, approve /
  deny). Filters: All · Needs you · Mentions. Data and actions unchanged (`useActivity`).
- **Teams / Settings:** restyle to the shared language (grouped inset lists, avatars, type);
  no feature or data changes.

## Backend slices (owner ruling 2026-09-26)

Two small backend slices are in scope; everything else is client-only.

1. **Chats-list liveness (Part 1):** typing and agent run start/end publish a compact
   `room.activity.changed` hint (`{roomId, actor{kind,id,displayName}, state: typing|working|idle,
   expiresAt?}`) to every CURRENT human member's private user channel
   (`/ws/<workspaceId>/users/<opaqueUserKey>`), best-effort, from the typing route and the run
   ledger's close paths. Membership is strong-read at publish time, mute stays client-side, the
   typist is skipped, shared-room members resolve in their home workspace, and it adds no queue,
   table or bus (AppSync is the fanout). Run callbacks carry no activity text, so the list caption
   names the agent ("CEO is working…").
2. **Read receipts (Part 2):** the room detail returns `readCursors` (every current human's
   `lastReadSeq`) and `agentDeliveries` (every current agent's delivery cursor); the room channel
   carries `room.read.changed` (a human cursor moved, or an agent's run started on a message) and
   `room.delivery.changed` (an agent's delivery cursor advanced past an addressed message).

Web behaviour is unchanged; shared types and clients grow only.

## Rulings on flagged gaps (owner, 2026-09-26)

- **Tokens:** `theme.ts` held older main-block values and no dark set; a new `chatPalette`
  (light + dark, copied one-for-one from `[data-chat-theme]`) is added with a contrast test on every
  plane; legacy screens keep `colors` until restyled.
- **Unprompted label:** one label, "on its own", for any agent message posted outside a room turn
  (`runId` null) — the backend exposes no finer kind.
- **Rings:** no ring for Paperclip's `active` (created-but-never-run) status; `running`/`thinking`/
  `covered`/`stalled` → live ring, `error`/`failed` → attention, `paused` → paused. (Web differs; web
  is unchanged.)
- **Pins:** long-press a pin opens one menu with Unpin and Reorder; reorder mode uses move-left/right
  buttons (VoiceOver-friendly, no drag). Sessions are not pinnable; running ones appear
  automatically when the Settings switch is on.
- **Large title:** Lora large "Chats" title via the native header; if it does not collapse or jumps
  between tabs on the simulator, fall back to the standard title.
- **Monospace:** only code — fenced/indented blocks, inline `code`, tool output wells and diffs. The
  session TUI glyphs (❯ ⏺ ⎿ ✻) are removed.
- **Activity:** one button per row; an approval's payload (destination, preview, fields) moves into
  the Review sheet verbatim. The phone shows All · Needs you · Mentions (web keeps five filters).
- **Settings switch:** uses `@expo/ui`'s Toggle; the implementer verifies the installed prop names.
- **Teams:** only the Teams tab root and its shared cards/avatars are restyled; issue/run/routines
  detail screens and Control stay on the legacy light palette for now.
- **Ended sessions** stay in Chats as ordinary chats; swipe → Archive hides one until it has new output.
- **Presence:** the existing `/messaging/presence` heartbeat + snapshot (web-only until now) is
  wired on the phone, so online dots and a human DM's "online" status are real.

## Non-goals

Backend work beyond the two slices above. No web or desktop behaviour changes. No new colours or fonts. Server-side pin sync
(ruled out). Android.

## Constraints

- Mobile is JS/TS only — no new native modules, no `app.json` change, so each part ships as an OTA
  (merging to main publishes `mobile-ota.yml`). Verify the fingerprint per PR. The backend slices
  ship through the normal pipeline and must reach dev before the phone's acceptance screenshots.
- `@lightbulb/shared` stays framework-neutral; any new shared logic (e.g. merging rooms and
  sessions into one sorted list) goes there with tests, UI stays in `apps/mobile`.
- Accessibility: VoiceOver labels on rows/bubbles/state rings; Dynamic Type scales message
  text (existing `textScale.ts` rules); 44pt touch targets.
- Acceptance: Chrome-free — simulator + device screenshots on each PR (no E2E suite exists).

## Testing

- pytest for the backend slices (activity fan-out, run announce on every close path, receipt
  fields and events); vitest for the shared contracts.
- Jest for every new component and the list-merge/sort, pins storage, read-state derivation,
  tab routing (sessions reachable from Chats), and that no removed route is still linked.
- Visual acceptance on the iOS simulator in light and dark for: Chats (with pins, typing,
  working, unread, muted), an agent chat with long markdown + tool chips + working bubble, a
  Claude Code session, a group channel with read-by, Activity with Needs-you items.
