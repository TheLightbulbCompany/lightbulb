# Agents message a person's session with their approval

Status: draft, 2026-10-07. Owner ruling (same day): "Friday should need my approval to message my
Claude Code session, but it can if it needs to; maybe a setting too." Builds on #3826 and #3729.

## 1. Problem

| Today | Where |
|---|---|
| `lightbulb_local_sessions` (`resources`/`read`/`send`/`receipt`) works only with a room grant; with none it answers `NO_TURN`. | plugin `__init__.py:199-286` (`NO_TURN` :18, check :241) |
| The grant is minted per room dispatch only, lives ≤ 300 s, and names a requester only when a human wrote the trigger. | `openclaw_work.py:641-656`, header `:817`; `local_session_grants.py:15,27,34-51` |
| Paperclip runs, heartbeats and routines get no grant. All three arrive on one route. | `routers/messaging.py:1117-1136`, bridge `server.py:93` |
| `resources` lists only sessions owned by humans in the grant's room. | `local_sessions.py:978-1050` (room loop :984-988) |
| Agents have no roomless path; the session owner must be in the room. An agent may `send`/`interrupt` only when the triggering human owns the session. Anyone else: 403. | `local_sessions.py:953-955`, `:962-963`, `:965-975` |
| The receipt enum is `pending\|dispatched\|applied\|rejected\|unknown`. | `local_sessions.py:520` |
| The skill says "room run" and "300 seconds", and names no rule for who may receive a `send`. | `platform-primitives.md:74-102`, `SKILL.md:48` |

So Friday cannot reach a session from Teams work, nor message one whose owner did not wake her.

## 2. Decisions

1. **One grant for every agent turn.** The backend mints the local-session grant for room turns
   (unchanged) and for every Paperclip run, heartbeat and routine, at the one facade route.
2. **`resources` lists every shared session in the workspace**, for both grant kinds: the same
   set a person sees (`list_workspace_sessions`, `local_sessions.py:1103-1130`; `shared` filter
   :1119). `read` needs no approval: any member may already read a shared session (ruling
   2026-09-24, docstring :944-946).
3. **Direct send is unchanged** when the grant's requester owns the session. A Paperclip grant
   has no requester (`requesterId: null`): no verified person wrote a Paperclip wake.
4. **Otherwise `send` and `interrupt` ask.** The call files ONE pending ask and returns
   `awaiting_approval` at once. Nothing reaches the Mac until the session owner chooses Send.
   Send → `submit_command` + `dispatch_session` on the owner's authority, attributed to the
   agent. Decline → `rejected`. No answer in 24 h → `rejected`.
5. **The ask is Lightbulb-native, not a Paperclip approval.** Any board member decides a Paperclip
   approval (`routers/teams/approvals.py:59-110`), its `type` is a closed fork enum
   (`packages/shared/src/constants.ts:689-694`), and only the session owner may decide this. The
   ask rides the join/pick-up request rail (`activity.py:460-500`, pick-up spec §3.1) with the
   exactly-once settle of `browser_extensions.settle_request` (`browser_extensions.py:493-508`).
6. **Per-person setting `sessionMessagesFromAgents: "ask" | "allow"`, default `ask`**, on the users
   row beside `dailySummary` (`user_repo.py:156-226`, `users.py:132-158`). `allow` makes decision 4
   a direct send, for every agent and every session of that person.
7. **`awaiting_approval` is an agent-only receipt state.** The ask is its own row, never `COMMAND#`,
   so desktop and human wires stay unchanged; the agent routes return `AgentCommandReceipt`
   (`state` adds `awaiting_approval`). Send reuses the agent's `requestId`.
8. **Grant lifetime 300 s → 1800 s** for both kinds: Paperclip runs last minutes, and every write
   outside the requester's own sessions is now gated by a person.

## 3. Contract

### 3.1 Grant (`local_session_grants.py`)

| Claim | Room turn | Paperclip run / heartbeat / routine |
|---|---|---|
| `origin` (new) | `{kind: "room", roomId}` | `{kind: "paperclip", runId, issueId \| null}` |
| `roomId` | room UUID | `null` |
| `requesterId` | human author or `null` (unchanged) | always `null` |
| `sessionKey` | `session_key(agent, room)` (unchanged) | the verified `X-Hermes-Session-Key` |
| `exp` | `iat + 1800` | `iat + 1800` |

- Mint for Paperclip in `create_paperclip_hermes_run` after `_verify_paperclip_hermes_identity`
  (`routers/messaging.py:1130`). Pass it in `X-Lightbulb-Local-Session-Token`, the same
  header room ingress uses. The bridge's Paperclip create (`server.py:93`) puts it in the turn
  context as `localSessionToken`, as the room path does (`server.py:288-289`, `turn.py:3351`).
- `verify` keeps its strict claim set, now with `origin`. `AgentInvocation` gains `origin`.
- `authorize_session` for an agent: room grants still pass `require_caller` (the agent is still
  in its room). The roomless refusal (:953-955) and the owner-in-room check (:962-963) go for
  agents. The session must be `shared` and not `unshared`.

### 3.2 Backend routes and rows

| Route | Change |
|---|---|
| `GET /messaging/local-session-agent/resources` | Workspace shared sessions (decision 2). Each session gains `agentSend: "direct" \| "ask" \| "unavailable"`. `unavailable` uses the liveness half of `native_session_tools.sendable` (:136-145). `devices`/`pairedNodes` stay room-scoped. They are empty for a Paperclip grant. |
| `POST …/local-session-agent/sessions/{sid}/commands` | `send`/`interrupt`: direct when `requesterId == humanUserId` or the owner's pref is `allow`. Otherwise it files an ask and returns `202 {state: "awaiting_approval", requestId, expiresAt}`. A repeated `requestId` returns the same ask. |
| `GET …/commands/{requestId}` | `COMMAND#` first, else the ask: pending → `awaiting_approval`, declined → `rejected` + `reason: "declined"`, expired → `rejected` + `reason: "expired"`. |
| `POST /messaging/sessions/{sid}/agent-asks/{requestId}` (new, Clerk) | `{decision: "send" \| "decline"}`. Only `humanUserId` may decide: never a teammate under team control. Decline: pending → `declined` (conditional update). Send: §3.3. Either once; a later decision → 409. Returns the receipt. |

Ask row, on the session's pk in the messaging table: `sk AGENTASK#<requestId>`, `agentId`,
`agentName`, `kind` (`send`/`interrupt`), `text`, `expectedGeneration`, `expectedTurnId`,
`origin`, `status` (`pending`/`sent`/`declined`/`failed` + `reason`), `createdAt`, `expiresAt` (+24 h,
read lazily: `<= now` is expired; nothing sweeps), `ttl` (`expiresAt` + 7 d, so the receipt outlives it).

### 3.3 Send on approval

The resolver builds `AgentInvocation(owner, origin.roomId, agentId, expires_at=now+30,
invocation_id=askId, requester_id=<session owner>, origin)` and calls `submit_command` with the
stored command, then `dispatch_session`, as `native_session_tools.session_send` does (:402-416).
The requester is the owner, so the owner-only rule (:965-975) admits it. The row keeps `actor:
{kind: "agent", id}` (:1455-1462) and, like its audit row (:1854), gains `approvedBy`;
`_actor_label` (`local_session_relay.py:255-275`) shows `Friday, approved by you`. A stale ask fails the normal
checks (generation, turn, offline; `_require_control` :1349-1386): the ask goes pending → `failed`
with that reason, read as `rejected`. Nothing is retried. **Atomic:** pending → `sent` is a
conditional Update in `admit_command`'s own transaction (`submit_command`'s `source_conditions` →
`extra`, :1612-1618), so no ask reads `sent` without its `COMMAND#` row. A crash leaves it `pending`;
Send again replays under the same `requestId`.

### 3.4 The setting

| Surface | What |
|---|---|
| Users row | `session_messages_from_agents`: `"allow"`, or absent (= `ask`). `set_prefs` gains the kwarg. |
| `GET/PUT /users/me/prefs` | `sessionMessagesFromAgents: "ask" \| "allow"`. |
| Web | Settings → Desktop → **Session sharing** card (`components/desktop/LocalSessions.tsx:192-211`): "When an agent wants to message your sessions" · `Ask me` / `Allow`. |
| Phone | Later (§6); the phone answers asks through Activity from day one. |

### 3.5 Notification and copy

One ask = one activity row for the session owner (`kind: "session"`, `subtype: "join_request"`,
row `request: "agent_message"`, item id `agentmsg:<sid>:<requestId>`). One
`workspace.session.attention` state `join_request` + push, as pick-up does.

| Where | Copy |
|---|---|
| Title / push title | `Friday wants to message <session title>` (`Friday wants to stop <title>` for interrupt) |
| Body / push body | the text, clipped like other activity summaries |
| Reason line | `Nothing is sent until you choose. Change this in Settings → Desktop.` |
| Actions | `[Send]` (primary) `[Decline]` |
| Links | the session, plus the origin room or Paperclip issue |
| Resolved | `Sent` / `Declined` / `Expired` |

Agent receipts: `awaiting_approval`; after Send the normal states plus `approvedBy`; after Decline
or expiry `rejected` with its `reason`. A decision does not wake the agent.

### 3.6 Skill text

`platform-primitives.md` § Human-shared local sessions replaces "In a canonical Lightbulb room run"
and the 300-second paragraph: any turn (room, Paperclip run, heartbeat, routine) may list and `read`
every shared session; `send` (`kind` `send`/`interrupt`) goes straight in only for the session of
the person who woke you, or when its owner allows agent messages; otherwise it returns
`awaiting_approval` and the owner gets a Send/Decline ask. Check `agentSend` first, ask once, say so
in your reply, never re-ask under a new `requestId`; `declined` is final; the grant lasts 30 min.
`SKILL.md` row: `| references/platform-primitives.md § Human-shared local sessions | Messaging or
stopping a person's Claude Code/Codex session: direct only on its owner's request, else one
approval ask. |`. Plugin schema description (:211-213) and `MAINTENANCE.md` (:19) follow.

### 3.7 Abuse bounds

| Bound | Value |
|---|---|
| Pending asks per (agent, session) | 1. A second `requestId` while one waits → `429 LOCAL_SESSION_ASK_PENDING`. |
| Asks per (agent, session) per 24 h | 5, declined ones included → `429 LOCAL_SESSION_RATE_LIMITED`. |
| `read`, `resources`, `receipt` / `interrupt` | Never ask / same ask as `send`. |
| `approve` / `end` | Still never an agent's (`local_sessions.py:1434-1436`, :1345-1347). |
| Hop caps | Unchanged. An agent command carries no `nativeHop` (`_outgoing_hop` counts session actors only, `native_session_tools.py:419-436`). `AGENT_REPLY_CAP` (`messaging.py:50`) governs rooms, not sessions. |

## 4. Rollout order

1. Backend: grant v2 (`verify` takes both shapes while old room grants live), widened reads, ask
   row + routes, setting, activity variant. Old images ignore the header on the Paperclip path.
2. Web: activity actions + the setting. Check the phone renders the generic row; OTA if not.
3. Hermes image: bridge threads the Paperclip grant. Plugin and skill text. Pin dev, validate,
   pin prod, then `roll_hermes_runtimes.py` (a pin alone moves nothing).
4. Dev acceptance: a Paperclip-run Friday asks a second account's session → push → Send → the
   terminal shows "Friday, approved by you"; then Decline, expiry, `allow`.

## 5. Tests to write

| Test | Proves |
|---|---|
| `test_local_session_grants`: Paperclip origin round-trips; extra or missing claim refused; `exp > iat+1800` refused | grant v2 |
| Paperclip create mints and forwards the header; a failed identity check mints nothing | one mint site |
| `resources` with a Paperclip grant lists a non-room member's shared session, never an `unshared` one | widening |
| Requester-owned send → `COMMAND#` row, no ask (regression) | decision 3 |
| Non-owner send → ask row, no `COMMAND#`, no dispatch, `awaiting_approval`; same `requestId` again → same ask | decision 4 |
| Send → command with `actor.kind: "agent"`, `approvedBy`; admission failure leaves the ask `pending`; 2nd decision → 409 | atomic, once |
| Decline → `rejected`/`declined`; expired → `rejected`/`expired`; decide after expiry → 409 | end states |
| Team-control teammate cannot decide; pref `allow` → direct send; absent pref = `ask` | owner only, setting |
| Interrupt asks; 2nd pending → 429; 6th in 24 h → 429 | bounds |
| Stale generation at Send → `rejected`, no retry; `_actor_label` names the agent with no room | stale, label |
| Bridge: Paperclip run context carries `localSessionToken` | image |

## 6. Not in this work

The terminal band / Mac answer door; the phone setting; waking the agent on a decision; allowlists.
## 7. Decisions taken (owner may overrule)

1. The ask lives in Activity (Needs you), not the Teams approvals list (decision 5).
2. Paperclip wakes never carry a requester, so they always ask unless the owner allows.
3. Grant 1800 s for every turn; `allow` per person, all workspaces and agents; asks expire in
   24 h; 1 pending and 5/day per agent per session; `read` never asks.
