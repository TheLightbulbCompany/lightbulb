# Sidebar coupling: backend session list vs engine `directory`

Read-only trace, `origin/main` fetched 2026-10-30. Monorepo = `lightbulb.nosync`; engine = `lightbulb-network.nosync` (v0.7.5, `a2ee6a4`).
Short names: `row` = `apps/frontend/src/components/messaging/SessionListRow.tsx`; `labels` = `packages/shared/src/messaging/localSessionLabels.ts`;
`types` = `packages/shared/src/messaging/types.ts`; `ctx` = `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx`;
`rd` = `apps/frontend/src/components/messaging/RoomDirectory.tsx`; `asv` = `.../messaging/AllSessionsView.tsx`; `client` = `packages/shared/src/messaging/client.ts`;
`ls.py` = `apps/backend/core/services/local_sessions.py`; `relay.py` = `apps/backend/core/services/local_session_relay.py`;
engine `presence.rs`, `frames.rs` = `src/net/frames.rs`, `ipc.rs` = `src/daemon/ipc.rs`, `sessions.rs`, `roster.rs`, `proof.rs`.

Builds on `desktop-session-pipeline.md` (not repeated).

## 0. The two row shapes

**Backend row** (`LocalSession`, `types:412-524`; server model `ls.py:296-...`): sessionId (uuid), deviceId, humanUserId (Clerk), provider, generation, status (idle/busy/waiting-approval/unknown/offline/ended), terminal, terminalReason, done, waitingFor, note, changes, agents, pr, title, displayTitle, cwdName, lastEventAt, endedAt, endedReason, latestEventSeq, shared, teamControl, allowedSenders, join*/pickup* fields, pendingJoins/pendingPickups, pendingApproval, chatRoomId, chatUnread, resume, resumable, forkedFrom, kind, capabilities, controlUnavailableReason ... **No native conversation id** (see §2).

**Engine directory record** (`presence.rs:425-444`): `session_id` (`<member12>/<provider>:<conversation>`, `sessions.rs:8-17`), `person` (roster name), `person_id` (= member key, `roster.rs:90-92`), `person_verified`, `computer`, `agent`, `cwd` (full path for this Mac, **empty for other computers**, `presence.rs:422`, `521`), `repo`, `branch`, `status` (`busy`|`idle` only, `frames.rs:43-44`, `presence.rs:102`), `doing`, `title`, `tier`, `online`, `can_send`, `needs_owner_ok`; local rows add `handle` and `hidden` (`presence.rs:510-514`).
- Local rows = live sessions only; `Gone` dropped (`sessions.rs:260`, `ipc.rs:1408-1424`). No ended/history.
- Remote rows = each member's latest presence, kept a day after last seen, with `online` (`presence.rs:19-22`, `400-408`, `519`).
- `Remote` carries `folder` (basename, `presence.rs:87-95`, `frames.rs:36`) but `record()` **does not output it**; only the query matches it (`presence.rs:487`). So another computer's folder name is not in the directory output today.

## 1. Every sidebar field/action: backend source and engine equivalent

| UI element | Where | Backend field | Engine directory equivalent |
|---|---|---|---|
| Title | `row:84`, `labels:23-24` | `displayTitle` → `title` → "New chat" | `title` (harness title). **No `displayTitle`** (owner rename is backend-only, `client:209`). |
| Tool icon / label | `row:81`, `labels:25` | `provider` | `agent` (same words for claude/codex/cursor/grok; plus `opencode`, `sessions.rs:20`; named sessions can be e.g. `Friday`, `frames.rs:61-73`). |
| Person face / name / own | `row:52,64-65,80`; built in `ctx:439-443` from `humanUserId` + Clerk roster (`ctx:431-432`) | `humanUserId` | `person`, `person_id` = **member key, not Clerk id** (`roster.rs:90-92`). No mapping in the record (§2). |
| Sections "You" then people | `labels:155-165` | `humanUserId` | group by `person_id`; "you" = `person_id == my_person` (`presence.rs:81-83`, `442`). |
| Folder ("in X", second line) | `row:71,74` | `cwdName` (basename) | Local: `cwd` (full; basename it). Remote: **missing** (record drops `folder`). Needs a one-line engine change to emit `folder`. |
| Status word / shimmer (Working, Idle, Online) | `row:84`, `labels:27-29,75-79` | `status` busy/idle/unknown | `status` busy/idle. No `unknown`. |
| Waiting on you (orange dot, "Waiting for input: …") | `row:69-70,88`, `labels:48,112-113` | `status=waiting-approval`, `waitingFor`, `pendingApproval` | **None.** Engine status is only busy/idle. |
| Running in background / Done | `labels:49-50` | `terminal=attach`, `done` | **None.** |
| Offline | `labels:47`, `sessionArchived` `labels:171-172` | `status=offline` (lease `onlineUntil`, `ls.py:835-849`) | `online:false` on a remote row (member's computer not connected). Not per session. |
| Stopped / ended reason ("stopped while idle") | `labels:46,117-121`, `row:64` | `status=ended`, `endedAt`, `endedReason` | **None.** Engine lists live sessions only; an ended one just disappears. |
| Faded "gone" row | `row:75`, `labels:175` | status not offline/ended | online + present. |
| PR badge, "N agents running" | `row:72-73`, `labels:94-107` | `pr`, `agents` (desktop `signals.rs:162,321-346` → heartbeat `relay.py:395` → row `ls.py:866`) | **None** (`repo`/`branch` only; no PR, no subagent count). |
| Unread / "finished since you looked" dot | `row:66,89`; `labels:249-275`; seen kept per browser (`ctx:407-420`) | `latestEventSeq` | **None.** Engine has no event sequence. |
| Chat unread count | `row:67,87` | `chatUnread`, `chatRoomId` | **None.** |
| Time on the right | `row:39-42,90` | `lastEventAt ?? endedAt` | **None** (no timestamp on a directory record). |
| Open (click row) | `row:78`, `ctx:891` `openSession(sessionId)` | backend `sessionId`; the panel then reads `/messaging/sessions/{id}` events/screen | Engine id `<member12>/<provider>:<conv>`; opening a shared session still needs the backend `sessionId` (§2). |
| Pin / Archive / Mark unread | `row:92-106`, `rd:400-415`, `labels:188-196` | per-browser marks keyed by `sessionId` + `latestEventSeq` | Keys could move to engine ids; "archived until new output" needs `latestEventSeq` (none in engine). |
| Rename | `row:107`, `ctx:892-895` | PATCH `/display-title` (`client:209`) | **None.** |
| End / Remove (own) | `row:108-109`, `labels:182-183`, `ctx:900-907` | DELETE `/messaging/sessions/{id}` (`client:198-206`) | Engine `end` only ends its row (`ipc.rs:1248-1263`), does not stop the process. Backend path stays. |
| Join / Ask to join / Pick up / allow senders | `LocalSessionPanel.tsx:355`, `484`; `client:217-238` | `joinRequest*`, `pendingJoins`, `pickup*`, `allowedSenders`, `teamControl` | **None.** Engine has `needs_owner_ok` (`presence.rs:442`) for its own mail, unrelated to the relay join. |
| Stop sharing / Only me | `SessionShareLink.tsx:20-125`, `client:217` | `shared` | Engine has `hidden` (folder off or marked, `ipc.rs:1421`) — a different switch; the desktop never tells the engine. |
| Resume | `labels:82-87`, `SessionResume.tsx`, `client:229` | `resume`, `resumable`, `resumeAllowed` | **None** (no ended rows). |
| Search (sidebar `SessionSearch`) | `rd:420`, `useSessionSearch.ts:29` | POST `/messaging/sessions/search` over uploaded transcripts (`message_search.py:236`) | `directory {query}` matches id/person/computer/agent/folder/repo/branch/doing/title (`presence.rs:484-499`) — metadata only, no transcript text. |
| All sessions filter | `asv:29`, `labels:278-283` | title, cwdName, provider, owner | Same fields available locally except remote folder. |
| "All sessions N" | `rd:398-399,436-439` | count of non-archived rows (incl. ended, offline < 3 days) | Count of live rows only. |
| Live / Stopped (Recent) grouping, Today/Yesterday… | `asv:31-35`, `labels:221-239` | `status`, `lastEventAt`/`endedAt` | Live only; no Stopped, no times. |
| "+N more", cap 5, pinned first, live then rest | `rd:396-429`, `labels:205-216` | as above | Ordering by live works; "rest" bucket empty. |
| "Sharing" presence on people | `ctx:405-406,425` | any non-ended non-archived row per `humanUserId` | Would need Clerk mapping. |
| Dock/tab count | `ctx:1055`, `labels:64-72` | `waiting-approval` own rows | **None.** |

Net: the engine gives *which live sessions exist, who, busy/idle, title, repo/branch, local cwd*. Everything about sharing, lifecycle (waiting/done/background/ended), history, unread, PR/agents, rename, and every action keys off the backend row.

## 2. Id mapping

**Backend row and the conversation id.**
- The desktop registers `provider`, `profileFingerprint`, `nativeConversationId` (`apps/desktop/src-tauri/src/local_sessions/transport.rs:3016-3021`).
- The backend stores them on the row (`ls.py:131-147` model; `**registration.model_dump` into the row, `ls.py:1096`) and derives `sessionId = uuid5(request_hash(owner_id, device_id, provider, profile, conversation))` (`apps/backend/core/repositories/local_sessions_repo.py:25-26`, called `ls.py:1067-1073`). It reuses them only server-side (offline outbox identity, `ls.py:1806-1810`; relay `relay.py:1516`).
- The **public** `LocalSession` (`ls.py:296-...`) has no `nativeConversationId`/`profileFingerprint`; `_public` is an allowlist that "never serialize[s] … native identity" (`ls.py:693-697`). The TS type has none either (`types:412-524`).
- `sessionId` cannot be recomputed from public data (needs `profileFingerprint`, and the hash is one-way).
- Public fields usable for a join: `provider`, `deviceId`, `humanUserId` (Clerk). No computer name, no member key.

**So:**
- *Own sessions on this Mac*: joinable locally today. The desktop mirror holds both the native `(provider, profile, conversation)` (key `transport.rs:2876-2878`) and the backend `session_id` from the register ack (`transport.rs:2143-2144`). Engine local row = `<my12>/<provider>:<conversation>` with the same Claude `sessionId` UUID. A Tauri command can return `{conversation → sessionId}`.
- *Teammates' sessions*: **not joinable** without a backend change. Minimal one: add `nativeConversationId` (it is a provider UUID, not a path) to the public `LocalSession` model, then join on `(provider, conversation)` (UUIDs; add person as a check). Whether exposing it is acceptable is the `_public` comment's call.

**Clerk user id ↔ network person.**
- Engine: `person_id` is always the member key; "people group by account later, with sign-in" (`roster.rs:90-92`; enforced by test `roster.rs:909-921`). The directory carries no Clerk id.
- The only link is the sign-in proof: `lightbulb login` gets a JWS `{sub: <Clerk user id>, computer: <member key>}` (`proof.rs:1-5,55-65`, `login.rs:1-8`). Each peer's proof arrives in its `hello` and is held in memory, `d.proofs: member key → token` (`src/daemon/mod.rs:105`, `src/net/mod.rs:1101-1104`), verified offline (`proof.rs:69-98`). Used for `same_person` (`src/net/memsync.rs:24-32`) and workspace admission (`proof.rs:113-126`). **Not exposed** by `directory` or any IPC op found.
- Backend: `NETKEY#{user_id}` registry, user → member keys (`apps/backend/core/services/network_login.py:300-338`), written on login approve (`:203`), refresh (`:226`) and route connect (`network_route.py:256`). Queryable by user id only (no key→user index).
- The desktop's `cliDeviceId` enrollment field (`ls.py:1051-1062`, `local_sessions_repo.py:71-77`) is the OpenClaw `node-host` pairing (`ls.py:1004-1008`, `PairedNode` `ls.py:456-460`), **not** the network engine's member key.
- Cheapest mapping: engine adds the verified proof's `sub` to each directory record (`user_id`, from `d.proofs`; own from `login::stored_proof`). Then person → Clerk id is local and offline-safe. Unsigned-in computers have none.

## 3. Other readers of the backend list

| Reader | Path | Can read a Mac's engine? |
|---|---|---|
| Web app sidebar / All sessions / mentions / dock count / "sharing" dots / RoomAppLinks | `useWorkspaceSessions` (`packages/shared/src/messaging/useWorkspaceSessions.ts:17-104`, GET `/messaging/sessions` + `workspace.sessions.changed` hints + 30 s poll) used at `ctx:404-458,790,1055`, `RoomAppLinks.tsx:67` | **No** (browser). |
| Desktop app | Same code: the Tauri window loads the hosted web app `https://thelightbulb.company/chat` (`apps/desktop/src-tauri/tauri.conf.json:9-10,24`; remote capability `capabilities/default.json:8-9`) | Yes, via a Tauri command (§4). The change must be gated to Tauri in shared web code. |
| Phone app | `useWorkspaceSessions` in `apps/mobile/src/app/(app)/room/[roomId]/index.tsx:158,234`, `thread/[messageId].tsx:72,134`; `session/new.tsx:105` GET `?includeEnded=true` | **No.** |
| Friday / cloud agents `lightbulb_local_sessions` | Hermes plugin → GET `/messaging/local-session-agent/resources` (`apps/infra/hermes/plugins/lightbulb/__init__.py:353-373`) → `list_resources` (`ls.py:1199`, router `messaging.py:324`) | **No** (cloud). |
| Agent turn context (room `sessions` entry) | `openclaw_work.py:525` `live_sessions_for_room` | **No.** |
| Session search | `message_search.py:236` `searchable_sessions` (uploaded transcripts) | **No.** |
| Activity (join/pickup asks, rows) | `activity.py:419,473,556` | **No.** |
| Notifications (waiting/finished/attention, phone push) | Server-side on heartbeat: `relay.py:508-569` `notify_attention` → live event + `send_push_to_user`; prefs `user_repo.session_notifications` | Server-side; independent of the sidebar list. |
| PR badge / agents / changes | Desktop computes (`signals.rs:321-346`), heartbeats them (`relay.py:395`), row exposes (`ls.py:866`) | Engine has no PR data. |
| Load sim | `apps/backend/scripts/load/mac_sim.py:323` | n/a |
| "Inspector" | No session-list reader named inspector found in backend routers/services (only `slack_app.py` mentions the word). | — |

All of these need the backend copy. Only the desktop sidebar can use the engine.

## 4. How the desktop frontend could call the engine

- **No existing engine socket client in the desktop.** `engine.rs` only installs/runs the bundled `lightbulb setup` and says "Not a Tauri command" (`apps/desktop/src-tauri/src/engine.rs:1-5,47-78`). No reference to `~/.lightbulb/connect/sock` or a `directory` op anywhere in `src-tauri/src` (grep). Unix sockets in the desktop are its own (`cli.rs:142-236`, holder `holder/panel.rs:173`, Claude/Codex native sockets).
- **Engine wire**: socket `~/.lightbulb/connect/sock` (or `$LIGHTBULB_CONNECT_HOME/sock`) (`src/paths.rs:5-35`); newline-delimited JSON, request `{id, op, ...body}`, one response line per request (`ipc.rs:86-144`); `subscribe` turns on pushed events on that connection (`ipc.rs:137-140`, `98-101`); `directory {query?}` → `{sessions:[...]}` (`ipc.rs:331-337`). Caller identity is the peer pid (`ipc.rs:88`); `directory` is not person-gated.
- **Commands to extend/pattern on**: `local_sessions_status` (`enrollment.rs:828-851`) is already invoked by the frontend (`DesktopPermissionsGate.tsx:102`, `LocalSessions.tsx:179,189`, `ExplicitSharingNotice.tsx:21,48`). A new sibling `#[tauri::command] engine_directory()` registered in `lib.rs:2008` `generate_handler!` is the minimal add: connect to the socket, send `{"id":1,"op":"directory"}`, read one line, and (for the join) attach `sessionId` from the in-process mirrors for own sessions. ~30 lines. Push updates: either poll, or a background `subscribe` connection emitting a Tauri event.

## 5. Shared sessions if the list comes from the engine

**What breaks or changes**
1. **Teammate rows cannot be opened** until the backend exposes `nativeConversationId` (or the engine row carries the backend id). Open, join, pick up, watch, type, approve, resume, rename, end, chat all take the backend `sessionId` (`client:197-240`).
2. **Membership mismatch.** The directory lists the P2P network roster (`presence.rs:416-419`); the backend lists the Clerk workspace with sharing enrolled (`ls.py:1351-1378`). A workspace teammate whose Mac has no engine / is not in the same network vanishes; a network member outside the workspace, or a cloud agent's named session (`frames.rs:61-73`), appears with no shared session to open.
3. **Privacy switches differ.** Lightbulb's "Only me" (`shared:false`, owner-only, `ls.py:1367`) is not known to the engine; engine `hidden` (folders off) is not known to the backend. A row the owner set to Only me could still be listed for teammates from the engine; filter remote rows through the backend's visible set, or teach the engine.
4. **Lifecycle words lost**: waiting-approval, done, background (attach), offline per session, ended/Stopped, ended reasons, Resume, Recent/Stopped grouping, times, "All sessions N" history (§1).
5. **Unread, archive-until-new-output, chat unread, PR badge, agents** need `latestEventSeq`, `chatUnread`, `pr`, `agents`: backend only.
6. **Status semantics differ.** Engine busy/idle comes from its own hooks/transcripts (`presence.rs:25-27,102`); the backend's from the desktop mirror (`desktop-session-pipeline.md` §3). The two can disagree (the "4 Idle vs 2 live" case); the engine's live set is stricter (process alive with birth time, `sessions.rs:120-123`, `260`).
7. **Remote folder** missing from directory output (§0).
8. **Search** becomes metadata-only unless the transcript search stays on the backend.

**What must stay on the backend (unchanged)**
- The whole relay: register/heartbeat/lease, screen + input streaming, commands, approvals, join/pickup/team control, link sharing, resume, end/remove, chat rooms, notifications/push, Activity, transcripts/search, uploads (`relay.py`, `ls.py`).
- The list endpoints for web, phone, Friday/agents, room context (§3).
- The row as the source of every per-session *detail* and *action* on desktop too.

**Shape that keeps shared sessions unchanged**
Desktop sidebar = engine directory for *existence + busy/idle + who*, each row joined to the backend row by `(provider, conversation)`; every field in §1 marked "None" and every action read from the joined backend row. Rows with no backend match are network-only (not shared through Lightbulb): show without Open/Join, or hide. Backend rows with no engine match (ended, offline, other workspace members without an engine) would disappear unless kept from the backend list — decide which.

Required changes (smallest set):
- Backend: add `nativeConversationId` to public `LocalSession` (`ls.py:296`) and `types:412`.
- Engine: add `folder` and the proof's `sub` (`user_id`) to `record()` (`presence.rs:425-444`).
- Desktop: one Tauri command reading `directory` (§4); gate the sidebar source on Tauri in `ctx:439-443`.
