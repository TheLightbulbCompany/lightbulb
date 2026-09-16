# Proactive agents: self-directed wakes and origin routing

Status: proposed, 2026-09-16. Owner rulings recorded in §3. Implementation plans are
written from this spec per packet (§13); this file is deleted when the last packet ships.

## 1. Goal

An agent that says "I'll post the link here" comes back and posts the link there. An
agent that notices something worth saying says it to the person who cares, or in a room
that asked to hear from it. Agents feel like teammates because they keep promises and
speak up, not because they answer faster.

## 2. Why nothing does this today

On 2026-09-16 Forge answered Leo in #report-bugs, opened GitHub issue #2686, wrote "I'm
tracking it through to the PR and will post the link here", and never ran again. The run
ledger shows one `user_request` run (14:55:31Z to 14:56:49Z, completed). The promise had
no mechanism behind it:

- An agent runs only when woken. The wake sources are a room message that targets it,
  a Paperclip run (assignment, comment, monitor, routine, watchdog), and OpenClaw's
  30-minute maintenance heartbeat.
- The heartbeat is a process-local timer with no delivery target. Under AgentCore there is
  no resident process, so it can never be the answer.
- The `cron` tool is denied by design: Paperclip routines are the only scheduler.
- The `message` tool already reaches any room the agent belongs to, out of turn and with
  no run id (`create_agent_message` requires membership only). Nothing tells the agent
  this, and nothing gives it a turn to use it in.

On AgentCore every turn is `InvokeAgentRuntime` from the backend, so "the agent posts at
will" is not a runtime property. It is: who writes the trigger, and what context the
invoke carries. This spec answers both.

## 3. Rulings and non-goals

Owner rulings, 2026-09-16:

- **Self-directed.** Agents create their own wakes. No approval gate on a wake.
- **Governed.** Every wake is a Paperclip object under the company's governance: on the
  board, in the activity log, cancellable, bounded by Paperclip's own recovery rules.
- **No room binding on Paperclip objects.** Rooms are laid out differently in every
  workspace, and work items come from anywhere. Paperclip never learns what a room is.
- **No budgets for now.** Deferred, with the cost note in §11.
- **Trial behaviour is out of scope.** Whatever the ladder yields on a trial is accepted
  for now.

Non-goals for this spec: a second event bus, a one-shot routine trigger kind (the issue
monitor is the one-shot), a per-room routing table or any new routing setting, a
backend-side "worth saying" gate (§11), email or terminal-session origins beyond declaring
the type, and a room UI for pending follow-ups (slice two, §9).

## 4. Concepts

**Wake.** A reason for an agent to run. Existing: a room message, a Paperclip assignment
or comment. Added by this spec, all through Paperclip primitives that already exist:

| promise | Paperclip primitive | wake reason |
|---|---|---|
| "I'll come back on this." | issue monitor on the agent's own issue | `issue_monitor_due` |
| "I'll tell you when X happens." | routine with a webhook trigger | routine fire → issue → run |
| "I'll keep working toward this." | routine with a schedule trigger and a goal | routine fire → issue → run |

**Origin.** Where a work item came from. A small typed reference, not free text:

```
{ kind: "room", roomId, threadRootMessageId?, messageId? }
```

`kind` is a closed set. Only `room` is built by this spec; `email` and `session` are
reserved names so the reply path can grow without renaming. An item with no origin is
plain board work.

**Routing ladder.** Where an agent voices the result of a wake. Stop at the first rung
that applies:

1. **Origin.** The item came from a conversation, so reply there, in the thread if one
   was recorded. Always allowed: it is a reply.
2. **A relevant Listening room.** The agent is `active` in a room that fits the item.
   The agent chooses by name, topic, members, and recent conversation. Listening is the
   broadcast upgrade a human opts into per room; it is never required.
3. **DM the item's human.** The issue's creator, the routine's creator, the approval's
   requester, or the workspace owner as the last resort. A DM is addressed by nature and
   the human can mute it.
4. **Issue only.** Nothing fits, or there is nothing worth saying. Today's behaviour.

`Mentions` rooms are never posted into unprompted. `Off` rooms are never posted into.

**Governance** is Paperclip's, unchanged: the object is on the board from creation, every
fire is logged, the run is the run of record with its `X-Paperclip-Run-Id` trail,
watchdogs and monitor bounds apply, and cancelling is closing the issue or pausing the
routine.

## 5. Paperclip's view

Nothing changes in Paperclip for slice one. The agent uses what the bundled skill already
documents: `paperclip-api.sh create` for the issue, `checkout` to own it, `monitor --at`
for the one-shot wake, and the routines API for events and loops. Monitor preconditions
hold as documented: the issue must be `in_progress` or `in_review` and assigned to the
agent, so a promise made in chat with no issue behind it becomes create → checkout →
monitor in that turn. `nextCheckAt`, `notes`, `timeoutAt`, `maxAttempts` and
`recoveryPolicy` are Paperclip's fields with Paperclip's semantics.

Paperclip stores no origin and no room, and this spec changes nothing in the fork.

## 6. Lightbulb's view

Three small additions on the backend, all behind the existing per-owner hook token
(`_require_hook_token`), so only the owner's own container can call them.

**Origin registry.** Rows in the messaging table:

```
OWNER#<owner>   ORIGIN#<objectId>                {objectKind, objectId, agentId, origin, dueAt?, createdAt}
ROOM#<room>     ORIGIN#<objectId>                same item, for the room's pending list (slice two)
```

`objectId` is the Paperclip issue or routine id. Writes are idempotent on `objectId`; a
second write replaces `dueAt` and `origin` and never duplicates. Routes:

- `PUT /internal/messaging/origins/{ownerId}/{objectId}` body `{objectKind, agentId,
  origin, dueAt?}`. The backend verifies the agent is a current member of `origin.roomId`
  and that the room belongs to the owner; otherwise 403.
- `GET /internal/messaging/origins/{ownerId}/{objectId}` returns the row or 404.
- `DELETE` the same path when the item is done.

**DM door.** `POST /internal/messaging/dm/{ownerId}` body `{agentId, humanUserId}`
ensures a `dm` room between the agent (`active`) and that workspace member, and returns
`roomId`. It reuses the DM branch of `create_room` and the idempotent shape of
`ensure_vendor_room`. `humanUserId` must be a member of the workspace; otherwise 403.
The agent obtains it from the Paperclip user on the item through
`GET /internal/messaging/members/{ownerId}?paperclipUserId=`, which the backend resolves
from the `paperclip-companies` rows (`paperclip_user_id` → `user_id`).

**Skill.** The `lightbulb` skill gains `references/follow-ups.md` and one routing-table
row, plus two non-negotiables in `SKILL.md`:

- *A promise needs a wake.* Never say you will follow up, track, or post later unless you
  have created the issue, checked it out, set the monitor, and registered the origin in
  that same turn. If you cannot, say what you did instead.
- *A room-bound wake ends with a message.* When a wake carries an origin, your turn ends
  with a message at that origin, even if it is "still waiting, checking again at T".

`lightbulb-api.sh` gains `origin set|get|clear` and `dm` subcommands so the agent never
types a bearer header (the existing redaction rule).

Rung 2 of the ladder needs a room list with listen modes and topics. That route
(`GET /internal/messaging/rooms/{ownerId}?agentId=`) is slice two; no workspace has a
Listening room today except a seeded CEO in #general, so slice one implements rungs 1,
3 and 4.

## 7. The follow-up, end to end (slice one)

1. Leo: "@forge it won't let me react with emoji, please put in a PR."
2. Forge's room turn: does what it can now. If work remains, it creates an issue to
   itself (`--status backlog`), checks it out, sets a monitor
   (`--at <ISO> --notes "post the PR link"`), and registers the origin
   (`origin set <issueId> --room <roomId> --thread <messageId>`). It replies: "Filed
   ISOA-320. I'll post the PR link here by 3pm."
3. Teams: the issue shows its Monitor row with the due time and note. Activity logs it.
4. At `nextCheckAt` Paperclip clears the monitor and wakes Forge with
   `PAPERCLIP_WAKE_REASON=issue_monitor_due` and `PAPERCLIP_TASK_ID`.
5. Forge's wake turn: `origin get <issueId>` returns the room and thread. Forge finishes
   or checks the work, posts the result with the `message` tool to `lightbulb:<roomId>`
   with `threadId`, then either closes the issue and clears the origin, or re-arms the
   monitor and posts "still waiting, checking again at 5pm".
6. Room: Forge's message appears in Leo's thread, attributed to Forge. Other members
   are dispatched on it like any message, so the follow-up can start a conversation.

Origin missing at step 5 (registered in a turn that died, or a routine without one) is
not an error: the ladder continues to rung 3, then 4.

## 8. Events and loops

Same ladder, different wake. A routine created by an agent from a room turn gets its
origin registered under the routine id. On a routine wake the agent looks the origin up
by the routine id the wake context names, and by the issue id otherwise; a routine the
context does not name simply falls down the ladder. A routine created by the owner in Automations
has no origin, so its results go to a Listening room if one fits, else to the routine's
creator by DM, else to the issue. A loop is a schedule routine carrying a `goalId`;
each run reads the goal, does one step, and speaks only if the step produced something a
teammate would mention. That judgement is the agent's, inside the run, for now (§11).

## 9. Slice two

- **Room-session execution.** The adapter passes the origin in the wake and runs the turn
  in the agent's room session, so the follow-up remembers the conversation. The channel
  plugin opens a room run (run card, live frames) and delivers the final through the
  durable adapter into the thread. One run, one owner, still Paperclip's run of record.
- **Rung 2.** The rooms read route and the relevance choice.
- **Pending follow-ups in the room.** The `ROOM#/ORIGIN#` rows drive a small "Forge
  will follow up by 3pm" note on the promise message, with cancel from the thread.
- **Other origin kinds.** `email` replies through agent mail; `session` through the
  local-session relay.

## 10. Trust boundaries and failure modes

- Every new route is hook-token scoped to one owner; a container can only register
  origins for rooms its owner has and its agent belongs to, and can only open DMs with
  that workspace's members. No route lets an agent post as a human or into a room it is
  not a member of; `create_agent_message` keeps its membership check.
- Origin rows carry no message text, only ids.
- A fired monitor with an exhausted bound follows Paperclip's `recoveryPolicy`; the
  origin row is left in place so the recovery wake can still find the thread.
- Duplicate registrations are idempotent on `objectId`. A wake that finds no origin
  falls down the ladder. A DM door call for a non-member is a 403 the skill tells the
  agent to treat as "say it on the issue".
- Monitor `notes` and origin ids are visible in Paperclip activity; the skill's existing
  "non-secret notes" rule applies.

## 11. Deferred, deliberately

- **Budgets.** Ruling: not now. Cost note recorded once: under AgentCore every fire
  invokes the runtime, so a schedule routine on a short cadence keeps that workspace
  awake and forfeits the idle stop.
- **Backend-side "worth saying" gate.** Today the Listening gate runs inside the
  container. For ticks and events it should run in the backend so a "no" never wakes
  the microVM. Not needed while proactive volume is a handful of monitors a day.
- **Trial rules.** Out of scope by ruling.
- **A fired monitor nobody handled, in "Needs you" (#2007).** A second-order failure
  we have not observed: the wake itself dying after the monitor fires. Not built until it
  happens. If it does, it is one rule in the fork's attention service beside the `review`
  rule, a `monitor` source kind, a "Follow-up" label and a Check now action on our side.

## 12. Testing and verification

- Unit: origin repo round trip and idempotency; `PUT` origin 403 on non-member agent
  and foreign room; DM door idempotency and 403 on non-member; member resolution from a
  Paperclip user id.
- Skill: `lightbulb-api.sh origin`/`dm` argument parsing tests alongside the script's
  existing tests.
- Dev, end to end: on the dev roster, ask Forge in a room for something it cannot finish
  in one turn; confirm the issue, checkout, monitor and origin row exist; advance
  `nextCheckAt` or wait; confirm the reply lands in the thread and the origin is cleared.
  Then delete the origin row by hand and re-fire to confirm the DM rung.

## 13. Packets

1. **Backend.** Origin registry rows and routes, DM door, member resolution. Tests.
2. **Skill.** `references/follow-ups.md`, routing row, two non-negotiables,
   `lightbulb-api.sh` subcommands, `MAINTENANCE.md` anchors. Ships with an image rebuild.
3. **Slice two**, its own spec section promoted to a plan once packets 1 and 2 are live
   and a Listening room exists somewhere real.

Packets 1 and 2 close the Forge case.
