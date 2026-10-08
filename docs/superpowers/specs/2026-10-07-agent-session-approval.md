# Agents message a person's session with their approval

Status: draft, 2026-10-07; updated the same day to the owner's review decisions (§2, §3, §7).
Owner ruling: "Friday should need my approval to message my Claude Code session, but it can if it
needs to; maybe a setting too." Builds on #3826 and #3729.

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

Owner review 2026-10-07 changed decisions 1, 4, 5, 7 and the bounds; this section is the result.

1. **Room turns keep their grant; Paperclip runs, heartbeats and routines get one too**, minted at
   the one facade route. A Paperclip grant reaches a person's sessions only when that person has
   turned on **`sessionsReachableFromBackgroundWork`** (default OFF). Off is today's behaviour for
   that person: background work cannot list, read, ask about or message their sessions. The
   setting is per person, so the gate is per session owner and is checked on every call; the
   grant itself is minted for every verified run and is inert for anyone who has not opted in.
2. **`resources` lists every shared session in the workspace** for a room grant: the same set a
   person sees (`list_workspace_sessions`, `shared` filter). For a Paperclip grant, only the shared
   sessions of people who opted in (decision 1). `read` needs no approval: any member may already
   read a shared session (ruling 2026-09-24).
3. **Direct send is unchanged** when the grant's requester owns the session. A Paperclip grant has
   no requester (`requesterId: null`): no verified person wrote a Paperclip wake.
4. **Otherwise `send` and `interrupt` ask**, unless the session owner turned on
   **`agentSendWithoutAsking`** (default OFF, which means ask): then they go straight in, for every
   agent and every session of that person. An ask files ONE pending row and returns
   `awaiting_approval` at once. Nothing reaches the Mac until an approver chooses Send. Send →
   `submit_command` + `dispatch_session` under the approver's authority, attributed to the agent
   (`actor.kind: "agent"`, `approvedBy`). Decline → `rejected`. No answer in 24 h →
   `rejected`/`expired`.
5. **Approver = the session owner, or anyone in the workspace while the owner has team control on**
   for that session ("Let teammates control this session"). People let in one by one
   (`allowedSenders`) are not approvers. The relay re-checks the approver at delivery, so turning
   team control off before delivery stops a teammate-approved send.
6. **The ask is Lightbulb-native, not a Paperclip approval.** Any board member decides a Paperclip
   approval (`routers/teams/approvals.py:59-110`) and its `type` is a closed fork enum
   (`packages/shared/src/constants.ts:689-694`). The ask rides the join/pick-up request rail
   (`activity.py:460-500`, pick-up spec §3.1) with the exactly-once settle of
   `browser_extensions.settle_request` (`browser_extensions.py:493-508`).
7. **Where the ask shows:** the owner's Needs-you card + push (the join-request rail); one line
   from the agent in the originating room or session chat when the trigger was a room turn ("I've
   asked <owner> before sending that to their session.", a progress line: it wakes and notifies
   nobody); and the owner's Claude Code terminal band, as a presence `joinRequests` entry with
   `kind: "agent_message"` that the band answers through the native decision door (Allow → send,
   Decline → decline). The band half is a separate mod PR.
8. **`awaiting_approval` is an agent-only receipt state.** The ask is its own row, never
   `COMMAND#`, so desktop and human wires stay unchanged; the agent routes return
   `AgentCommandReceipt` (`state` adds `awaiting_approval`). Send reuses the agent's `requestId`.
9. **Grant lifetime 300 s → 1800 s** for both kinds: Paperclip runs last minutes, and every write
   outside the requester's own sessions is now gated by a person.

## 3. Contract

### 3.1 Grant (`local_session_grants.py`)

| Claim | Room turn | Paperclip run / heartbeat / routine |
|---|---|---|
| `origin` (new) | `{kind: "room", roomId}` | `{kind: "paperclip", runId, issueId \| null}` |
| `roomId` | room UUID | `null` |
| `eventId` | trigger message id | `null` |
| `requesterId` | human author or `null` (unchanged) | always `null` |
| `sessionKey` | `session_key(agent, room)` (unchanged) | the verified `X-Hermes-Session-Key` |
| `exp` | `iat + 1800` | `iat + 1800` |

- Mint for Paperclip in `create_paperclip_hermes_run` after `_verify_paperclip_hermes_identity`,
  forwarded to the runtime in `X-Lightbulb-Local-Session-Token`, the header room ingress uses. The
  bridge's Paperclip create (`server.py:93`) puts it in the turn context as `localSessionToken`, as
  the room path does (Hermes image follow-up; an old image ignores the header).
- `verify` keeps its strict claim set, now with `origin`, and still accepts the pre-`origin` room
  shape (an older backend task mints it during a roll). `AgentInvocation` gains `origin`,
  `turn_id` (the room grant's `eventId`, the Paperclip grant's `runId`) and `approved_by`.
- `authorize_session` for an agent: a room grant still passes `require_caller` (the agent is still
  in its room); the session owner no longer has to be in that room. A Paperclip grant has no room;
  its gate is the session owner's `sessionsReachableFromBackgroundWork`. The session must be
  `shared`. An agent command is admitted when the requester owns the session, the owner allows
  agent sends without asking, or `approved_by` is still an approver (decision 5).

### 3.2 Backend routes and rows

| Route | Change |
|---|---|
| `GET /messaging/local-session-agent/resources` | Workspace shared sessions (decision 2). Each session gains `agentSend: "direct" \| "ask" \| "unavailable"` (`unavailable`: not live, unverified, failed closed or takes no input). `devices`/`pairedNodes` stay room-scoped; they are empty for a Paperclip grant. |
| `POST …/local-session-agent/sessions/{sid}/commands` | `send`/`interrupt`: direct as in decisions 3-4. Otherwise it files an ask and returns `202 {state: "awaiting_approval", requestId, expiresAt}`. A repeated `requestId` returns the same ask. |
| `GET …/commands/{requestId}` | `COMMAND#` first, else the ask: pending → `awaiting_approval`, declined → `rejected` + `reason: "declined"`, expired → `rejected` + `reason: "expired"`, failed → `rejected` + the failure code. |
| `POST /messaging/sessions/{sid}/agent-asks/{requestId}` (new, Clerk) | `{decision: "send" \| "decline"}`. Only an approver (decision 5). Both conditional; a loser → 409 `LOCAL_SESSION_ASK_SETTLED` with the current receipt in `details.receipt`. Returns the receipt. |
| `POST /messaging/native-session-decisions` | The band answers a presence entry with `kind: "agent_message"` here: `userId` = the agent id, `requestId` = the ask; `allow` → send, `decline` → decline. |

Ask row, on the session's pk in the messaging table: `sk AGENTASK#<requestId>`, `agentId`,
`agentName`, `kind` (`send`/`interrupt`), `text`, `expectedGeneration`, `expectedTurnId`, `roomId`
(origin room or `""`), `origin`, `turnId`, `status` (`pending`/`sent`/`declined`/`failed` +
`reason`), `approvedBy`, `createdAt`, `expiresAt` (+24 h, read lazily: `<= now` is expired;
nothing sweeps), `ttl` (`expiresAt` + 7 d, so the receipt outlives it). A slot row
`AGENTASKSLOT#<agentId>` holds the one pending ask per (agent, session): it is written in the ask's
transaction and deleted in the settle's, and an expired slot may be overwritten.

### 3.3 Send on approval

The resolver builds `AgentInvocation(owner, ask.roomId, agentId, expires_at=now+30,
invocation_id=requestId, requester_id=<approver>, origin, approved_by=<approver>)` and calls
`submit_command` with the stored command, then `dispatch_session`. The row keeps `actor:
{kind: "agent", id}` and, like its audit row, gains `approvedBy`; `_actor_label`
(`local_session_relay.py`) shows `Friday, approved by you` when the session owner approved and
`Friday, approved by <name>` when a teammate under team control did. A stale ask fails checks
(`_require_control`): the ask goes `sent` → `failed` with that reason, read as `rejected`; nothing
is retried. **Once:** Send first flips pending → `sent` as its own conditional write; a loser gets
409 `LOCAL_SESSION_ASK_SETTLED` with the current receipt, and only the winner calls
`submit_command` (whose existing-command return would let a loser "succeed"). **Resume:** a crash
between the two leaves `sent` with no `COMMAND#`; any later Send on a `sent` ask reads
`COMMAND#<requestId>` and, if absent, submits under the same `requestId` (idempotent).

### 3.4 The settings

| Surface | What |
|---|---|
| Users row | `agent_send_without_asking`, `sessions_reachable_from_background_work`: `True`, or absent (= off). `set_prefs` gains both kwargs. |
| `GET/PUT /users/me/prefs` | `agentSendWithoutAsking: bool`, `sessionsReachableFromBackgroundWork: bool`; both default `false`. |
| Web | Settings → Desktop → **Session sharing** card (`components/desktop/LocalSessions.tsx`): "Agents can message my sessions without asking" and "Background work (issues, heartbeats, routines) can reach my sessions". |
| Phone | Later (§6); the phone answers asks through Activity from day one. |

### 3.5 Notification and copy

One ask = one activity row for the session owner (`kind: "session"`, `subtype: "join_request"`,
row `request: "agent_message"`, item id `agentmsg:<sid>:<requestId>`). One
`workspace.session.attention` state `join_request` + push, as pick-up does. A teammate approver
answers from the session (the decision route), not from their own Activity.

| Where | Copy |
|---|---|
| Title / push title | `Friday wants to message <session title>` (`Friday wants to stop <title>` for interrupt) |
| Body / push body | the text, clipped like other activity summaries |
| Reason line | `Nothing is sent until you choose. Change this in Settings → Desktop.` |
| Actions | `[Send]` (primary) `[Decline]` |
| Links | the session, plus the origin room |
| Resolved | `Sent` / `Declined` / `Expired` |
| Room line (room trigger only) | `I've asked <owner> before sending that to their session.` |

Agent receipts: `awaiting_approval`; after Send the normal states plus `approvedBy`; after Decline
or expiry `rejected` with its `reason`. A decision does not wake the agent.

### 3.6 Skill text

`platform-primitives.md` § Human-shared local sessions replaces "In a canonical Lightbulb room run"
and the 300-second paragraph: any room turn may list and `read` every shared session, and a
Paperclip run, heartbeat or routine may do so for people who allow background work; `send` (`kind`
`send`/`interrupt`) goes straight in only for the session of the person who woke you, or when its
owner allows agent messages; otherwise it returns `awaiting_approval` and the owner gets a
Send/Decline ask. Check `agentSend` first, ask once, say so in your reply, never re-ask under a new
`requestId`; `declined` is final for this run; the grant lasts 30 min. `SKILL.md` row:
`| references/platform-primitives.md § Human-shared local sessions | Messaging or stopping a
person's Claude Code/Codex session: direct only on its owner's request, else one approval ask. |`.
Plugin schema description (:211-213) and `MAINTENANCE.md` (:19) follow.

### 3.7 Abuse bounds

| Bound | Value |
|---|---|
| Asks per (agent, session) | 1 pending (another `requestId` → `429 LOCAL_SESSION_ASK_PENDING`). No daily cap. |
| After a decline | The same agent cannot ask the same session again in the same run (the grant's `turn_id`) → `409 LOCAL_SESSION_ASK_DECLINED`. |
| `read`, `resources`, `receipt` / `interrupt` | Never ask / same ask as `send`. |
| `approve` / `end` | Still never an agent's (`local_sessions.py:1434-1436`, :1345-1347). |
| Hop caps | Unchanged. An agent command carries no `nativeHop` (`_outgoing_hop` counts session actors only). `AGENT_REPLY_CAP` (`messaging.py:50`) governs rooms, not sessions. |

## 4. Rollout order

1. Backend: grant v2 (`verify` takes both shapes while old room grants live), widened reads, ask
   row + routes, settings, activity variant, presence entry. Old images ignore the header on the
   Paperclip path.
2. Web: activity actions + the two settings. Check the phone renders the generic row; OTA if not.
3. Hermes image: bridge threads the Paperclip grant. Plugin and skill text. Pin dev, validate,
   pin prod, then `roll_hermes_runtimes.py` (a pin alone moves nothing).
4. Mod: the band renders `kind: "agent_message"` and advertises the `agentMessageAsks` feature.
5. Dev acceptance: a Paperclip-run Friday asks a second account's session (background setting on)
   → push → Send → the terminal shows "Friday, approved by you"; then Decline, expiry,
   `agentSendWithoutAsking`, background setting off.

## 5. Tests to write

| Test | Proves |
|---|---|
| `test_local_session_grants`: Paperclip origin round-trips; extra or missing claim refused; `exp > iat+1800` refused; pre-`origin` room grant accepted | grant v2 |
| Paperclip create mints and forwards the header; a failed identity check mints nothing | one mint site |
| Background setting off → a Paperclip grant lists and reads nothing of that person; on → lists their shared session, never an `unshared` one | widening, setting |
| Requester-owned send → `COMMAND#` row, no ask (regression) | decision 3 |
| Non-owner send → ask row, no `COMMAND#`, no dispatch, `awaiting_approval`; same `requestId` again → same ask | decision 4 |
| Two concurrent Sends → one `COMMAND#`, loser 409; `sent` + no `COMMAND#` → next Send submits it | once, resume |
| Decline → `rejected`/`declined`; expired → `rejected`/`expired`; decide after expiry → 409 | end states |
| Team-control teammate may decide, an allowed sender may not; `agentSendWithoutAsking` → direct send | approver, setting |
| Interrupt asks; 2nd pending → 429; a decline blocks a re-ask in the same run, not in the next | bounds |
| Stale generation at Send → `rejected`, no retry; `_actor_label` names the agent and the approver | stale, label |
| Presence carries `kind: "agent_message"` for a Mac that advertises it; the native door's allow sends | band |
| Bridge: Paperclip run context carries `localSessionToken` | image (follow-up) |

## 6. Not in this work

The terminal band itself (mod); the phone setting; waking the agent on a decision; allowlists.

## 7. Decisions taken (owner review 2026-10-07)

1. The ask lives in Activity (Needs you), the room line and the terminal band, not the Teams
   approvals list.
2. Paperclip wakes never carry a requester. They reach a person's sessions only with that person's
   `sessionsReachableFromBackgroundWork`, and then ask unless `agentSendWithoutAsking`.
3. Approver = owner, or anyone while team control is on. Grant 1800 s for every turn; asks expire
   in 24 h; 1 pending per agent per session; no daily cap; no re-ask after a decline in the same
   run; `read` never asks.
