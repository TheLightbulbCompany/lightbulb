# Session chat: people talk beside the terminal, not into it

Status: draft for review, 2026-10-07. Priority 2 of the 2026-10-07 brief (after
`2026-10-07-terminal-everywhere-design.md`). Builds on managed session sharing
(`2026-10-02-managed-session-sharing.md` rev 5) and the sharing mod (`2026-10-04-session-sharing-mod.md`).
Facts below cite `main` at `dfce908f8`.

## 1. Problem

A shared session has one input: the terminal (admitted people) or the follow-up box
(everyone else with control). Both instruct the coding agent. People who watch a session have
nowhere to talk to each other about it. "Message <owner>" closes the session and opens a DM in its
place (`MessagingWorkspaceContext.tsx:770`, `closeSession()` then `selectParticipant`), and a DM is
private to two people. On the phone the same button pushes the DM (`session/[sessionId].tsx:181-188`).

What is wanted: a discussion beside the terminal, with clear human and agent identities, its own
scroll, draft and focus, usable on a narrow screen, and no path from "can chat" to "can type into the
terminal".

## 2. Decisions (proposed; owner to confirm)

1. **One platform-managed room per session, created lazily.** `kind: "channel"`, hidden from the
   sidebar, id derived from the session (`uuid5("lightbulb-session-room|<owner>|<sessionId>")`,
   like `default_room_id`, `messaging.py:1754`), META `session: {sessionId, ownerId: humanUserId}`
   through the repo's `extra` (`messaging_repo.py:697-739`; the Meet rooms are the precedent,
   `meeting_invites.py:307`). The session's human is the room owner.
2. **Membership is derived from session read access, never chosen.** Anyone who can read the
   session (`authorize_session(auth, None, sid)`, `local_sessions.py:899-937`) joins on first open
   through the `join_default_room` pattern (AUTOJOIN claim marker, `messaging.py:1803`): a person
   who leaves is not re-seated. Humans cannot be added or removed by hand (same guard as the vendor
   room, `_deny_vendor_human_change`, `messaging.py:1778`).
3. **Chat never grants control.** The chat composer posts through `create_human_message` only.
   Nothing in it calls `submit_command`, `may_control`, `request_join`, `decide_join` or touches
   `allowedSenders` / `teamControl` (`local_sessions.py:769-772, 1944-2015`). Typing into the
   terminal keeps its own door (`/messaging/sessions/{id}/input`, `session_screen.type_into`). A chat
   member who wants to type uses Ask to join, as today.
4. **Agents are members on `mentions`.** The CEO and the agents the owner adds sit on `mentions`
   (`select_dispatch_targets`, `messaging.py:3398-3432`: an unmentioned `mentions` agent never
   runs), so chat chatter costs nothing until someone @mentions an agent. An agent in the room
   already receives the owner's live sessions in its turn context (`live_sessions_for_room`,
   `local_sessions.py:1089`) and reads the session with `lightbulb_local_sessions`; a session chip
   stays a reference (owner ruling 2026-10-02).
5. **Talking to people is visibly different from instructing the agent.** The chat column has its
   own composer labelled "Message the people here"; the terminal keeps the terminal; the follow-up
   box (where it still exists) keeps "Send to <session>". The glyphs differ (speech bubble vs `❯`),
   and a chat message is never relabelled or forwarded into the session.
6. **Notifications follow channel rules.** A hidden `channel` notifies only on @mention and thread
   replies (projector `_activity_kind`, `messaging-projector/index.py:691`), so watching a session
   does not spray pushes. Unread shows on the session's Chat toggle and on the session row, not in
   the sidebar total (hidden rooms are not listed, `messaging.py:1666-1675`).
7. **Lifecycle.** The room is archived when the session ends (`archivedAt` fences new posts,
   `messaging_repo.py:1018`); history stays readable. Pausing sharing (`unshared`) or revoking the
   session's owner removes every member but the owner with the delivery-key rotation
   (`_remove_member_with_notices`, `messaging.py:2372`), so an unshared session's chat goes silent
   at once. The room is never deletable by hand (extend the `delete_room` guard, `messaging.py:2398`).
8. **Guests of a public link do not get chat** (people outside the workspace have no user stream;
   `has_foreign_members` already blocks session references in shared rooms, `messaging.py:3488`).

## 3. What people see

| Where | Running session | Ended / offline |
|---|---|---|
| Web and desktop, ≥1180 px | Terminal in the middle, chat as a 344 px right column (the `.room-has-context` grid pattern, `messaging.css:363-390`), each with its own scroll and composer. A "Chat" toggle with an unread count in the session header. | Same, chat read-only (archived). |
| Web 860–1179 px | Chat overlays the right edge, like Room details. | Same. |
| Web < 860 px and the phone | A "Chat · N" header button pushes the room screen (phone: `router.push("/(app)/room/<id>")`, as "Message owner" does today); back returns to the session. | Same. |
| Identity | Rows use the room's own author rendering: a person's name and colour (the mod's `colorFor` palette on the terminal side, `rules.ts`), an agent's name with the agent mark. | Same. |

Focus: opening chat never takes focus from the terminal; the terminal's `attachCustomKeyEventHandler`
owns keys only while the xterm has focus. A draft in the chat composer survives a terminal
snapshot/reset (`term.reset()` on snapshot, `SessionTerminalView.tsx:84`) because the two live in
different components.

## 4. Contract

### 4.1 Backend
- `POST /messaging/sessions/{id}/chat` (Clerk, `get_current_user`) → `RoomDetail`. Checks
  `authorize_session(auth, None, id)`; creates the room if absent (owner as `owner`, default agents
  on `mentions`, `hidden: true`, `session` META); joins the caller (AUTOJOIN marker); returns the
  room. Refused 404 for an unshared session to anyone but the owner, like every session read.
- `RoomSummary`/`RoomDetail` gain `session?: {sessionId}` (add to `_room_summary_fields`,
  `messaging.py:1568`); `LocalSession` gains `chatRoomId?` and `chatUnread?` (owner-visible and
  member-visible once the room exists).
- Hooks: `set_sharing(unshared=True)` and `revoke_human_membership` remove members with rotation;
  `_ended` archives the room; `delete_room` refuses `is_session_room`.
- Agent members: the same `live_sessions_for_room` injection applies; no new tool.

### 4.2 Web
- A second room slot. Today the context holds one `useRooms` instance and
  `roomVisible = subscribeToRoom && !sessionFocus` (`MessagingWorkspaceContext.tsx:171-179`).
  Smallest change: `SessionFocus` mounts its own `RoomPanel`-shaped column driven by a separate
  `useRoom(roomId)` subscription (messages, composer, thread) rather than the workspace-wide
  `useRooms`. `openNotificationTarget` opens the session with the chat column instead of
  `closeSession()` when the room is a session room (`MessagingWorkspaceContext.tsx:879-892`).
- Reuse `RoomTimeline` / `RoomComposer` / `RoomMessageItem` as components; do not reuse the
  session follow-up composer.

### 4.3 Phone
- Header "Chat" button with unread badge on the session screen; pushes the room route. Verify that a
  hidden room loads through `GET /rooms/{id}` (`useRooms.ts:309`) when it is not in the list
  (unverified today).

## 5. Security

- Chat membership is a projection of session read access and is re-derived on every open; a
  member of the room who lost session access gets 404 from the chat door and is removed on the
  next unshare/revoke hook. Reads of the room itself still go through `require_room_member`.
- No route in this work writes `allowedSenders`, `teamControl` or `joinRequests`.
- The room is workspace-only; guests, OAuth/MCP clients and agents acting for a person cannot
  create or join it (`get_current_user` human sessions only; agents post through their room run).

## 6. Not in this work

Threads of chat into the terminal (quoting a screen line), pinning, search of hidden rooms
(the indexer does not skip hidden rooms today; decide before launch), chat on a public link.

## 7. Open

- Default agent members: CEO only, or every agent the owner's workspace seats in `#general`?
- Member cap: `MAX_ROOM_MEMBERS` is 32 (`messaging_repo.py:65`). A workspace larger than that
  needs the cap raised for session rooms or the vendor-room truncation with a metric.
