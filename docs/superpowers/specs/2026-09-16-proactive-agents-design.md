# Proactive agents: commitments, work, and where agents speak

Status: proposed, 2026-09-16, second draft. Owner rulings in §3. Implementation plans are
written per packet (§13); this file is deleted when the last packet ships.

## 1. Goal

An agent that says "I'll post the link here" comes back and posts the link there. An
agent that notices something worth saying says it to the person who cares, or in a room
that asked to hear from it. Agents feel like teammates because they keep promises and
speak up, not because they answer faster.

## 2. Why nothing does this today

On 2026-09-16 Forge answered Leo in #report-bugs, opened GitHub issue #2686, wrote "I'm
tracking it through to the PR and will post the link here", and never ran again. The run
ledger shows one `user_request` run (14:55:31Z to 14:56:49Z, completed). Nothing could
have brought it back:

- An agent runs only when woken. The wake sources are a room message that targets it,
  a Paperclip run, and OpenClaw's 30-minute maintenance heartbeat.
- The heartbeat is a process-local timer with no delivery target, and under AgentCore
  there is no resident process, so it can never be the answer.
- The `cron` tool is denied by design.
- The `message` tool already reaches any room the agent belongs to, out of turn and with
  no run id. Nothing gives the agent a turn to use it in.

On AgentCore every turn is `InvokeAgentRuntime` from the backend, so "the agent posts at
will" is not a runtime property. It is: who writes the trigger, and what context the
invoke carries.

## 3. Rulings and non-goals

Owner rulings, 2026-09-16:

- **The OpenClaw agent is the first-class primitive.** Paperclip is how work is tracked.
- **Self-directed.** Agents create their own wakes. No approval gate on a wake.
- **Governed.** A commitment is visible where it was made and cancellable there; work is
  visible on the board. Nothing runs where nobody can see it.
- **Paperclip may know where work came from, never where to speak.** Provenance yes,
  routing no. Rooms are laid out differently in every workspace and work comes from
  anywhere.
- **No budgets for now.** Cost note recorded once in §11.
- **Trial behaviour is out of scope.**
- **Build nothing for a failure that has not been observed.**

Non-goals: a second event bus, a cron-like scheduler inside the container, a per-room
routing table or any new routing setting, a backend-side "worth saying" gate (§11), and
origin kinds other than a room (email and terminal sessions are reserved names only).

## 4. Concepts

**A commitment is not work.** "I'll post the link here by 3pm" is a promise made in a
conversation. The PR behind it is work. Work is tracked in Paperclip (or GitHub) exactly
as today. The commitment belongs to the agent and its conversation, and this spec gives
it a primitive of its own: the **follow-up wake**, owned by the messaging plane.

**Origin.** Where a work item came from, as a reference Paperclip can hold without
understanding: the room message permalink,
`https://flickmeon.ai/chat?panel=chat&room=<roomId>&message=<messageId>`. A commitment
never needs a stored origin; it is created inside the conversation and carries the room
and thread itself.

**Routing ladder.** Where an agent voices something. Stop at the first rung that applies:

1. **Origin.** The item came from a conversation, so reply there, in the thread. Always
   allowed: it is a reply. Every follow-up wake lands here by construction.
2. **A relevant Listening room.** The agent is `active` in a room that fits. Chosen by
   the agent from name, topic, members and recent conversation. Listening is the
   broadcast upgrade a human opts into per room; it is never required.
3. **DM the item's human.** The issue's creator, the routine's creator, the approval's
   requester, or the workspace owner as the last resort. Addressed by nature, mutable.
4. **Issue only.** Nothing fits, or there is nothing worth saying. Today's behaviour.

`Mentions` rooms are never posted into unprompted. `Off` rooms are never posted into.

**Governance.** A commitment is governed by the conversation: it shows on the message it
was made on, can be cancelled from the thread, and every firing is a run in the ledger
with a run card. Work is governed by the board: routines and issues are Paperclip
objects with Paperclip's activity log, bounds and recovery.

## 5. Commitments: the follow-up wake

**The tool.** The channel plugin registers `lightbulb_follow_up` the same way it registers
`lightbulb_local_sessions`, so it is only offered inside a room turn and already knows
the owner, agent, room and thread. The agent passes what only it knows:

```
lightbulb_follow_up { action: "set",    at: <ISO>, note: <text> }   -> { wakeId, at }
lightbulb_follow_up { action: "cancel", wakeId }
lightbulb_follow_up { action: "list" }                                -> pending wakes in this room
```

`at` must be at least one minute out and at most 30 days. `note` is what the future turn
is told, so it is written for the agent's future self, not the human.

**The row.** The plugin calls `PUT /internal/messaging/wakes/{ownerId}` (hook token) and
the backend writes one item, mirrored under the room for listing:

```
OWNER#<owner>   WAKE#<wakeId>     {agentId, roomId, threadRootMessageId?, promiseMessageId?, note, dueAt, status, createdAt, ttl}
ROOM#<room>     WAKE#<wakeId>     same item
```

The backend verifies the agent is a current member of the room and that the room belongs
to the owner. `status` is `pending`, `fired` or `cancelled`. Writes are idempotent on
`(agentId, threadRootMessageId ?? roomId, dueAt)`: a repeat with the same key replaces
the note and returns the same `wakeId`.

**Firing.** A wake sweeper beside the existing run-ledger sweeper runs once a minute,
claims due `pending` rows with a conditional update to `fired`, and for each writes one
`ROOM#<room> / WORK#WAKE#<dueAt>#AGENT#<agent>` row carrying `kind: "wake"`, the
`wakeId`, and the room+agent `messageGroupId`. That row rides the existing path: the
DynamoDB stream, the EventBridge Pipe (`SK begins WORK#`), `openclaw-work.fifo`, and
`openclaw_work.py`, whose pointer contract already carries `kind`. The consumer adds a
`wake` branch: re-read the wake row, re-check membership, wake the container, and POST
the plugin's private ingress with:

```
eventKind: "follow_up"
sender:    { kind: "system", id: "follow-up", displayName: "Follow-up" }
message:   { text: <note>, threadRootMessageId: <from the wake> }
run:       { ...the normal async-turn contract }
```

**The turn.** The plugin accepts `follow_up` as a third `eventKind`. It runs in the
agent's **room session** (`agent:<id>:lightbulb:channel:<room>`), so the follow-up
remembers the conversation it was promised in, and it is delivered like a `user_request`:
the final goes through the durable adapter into the room, in the thread, with reply
routing intact. The consumer opens the ledger run with `triggerMessageId` set to the wake
id and `triggerKind: "wake"`, so the room shows the run card ("Forge is working"), live
frames, and abort/steer exactly as for a human-triggered turn. Usage reports through the
same `run.ended` callback.

**Bounds.** A wake that fires while the agent is already mid-run in that room queues
behind it on the same FIFO group, as messages do. A wake whose agent was removed from the
room is cancelled at fire time with a `LIVE` notice to the room. TTL on the row is
`dueAt + 30 days`, so history is queryable for a month and then gone.

**Skill rules** (`lightbulb` skill, two non-negotiables plus `references/follow-ups.md`):

- *A promise needs a wake.* Never say you will follow up, check back, track, or post
  later unless you have called `lightbulb_follow_up` in that same turn. If you cannot,
  say what you did instead.
- *A wake ends with a message.* A `follow_up` turn ends with a message in the thread,
  even if it is "still waiting, checking again at 5pm" with a new wake, or "done, the PR
  merged" with none.

## 6. Work: Paperclip issues and routines, with provenance

Nothing changes in Paperclip's code. Two of its existing features carry provenance:

- **External objects.** Paperclip's `generic-url` detector turns any URL in an issue's
  description, comments or documents into an `external_object` linked to the issue, and
  `GET /issues/:id/external-objects` lists them. The feature sits behind the instance
  setting `enableExternalObjects`; the backend flips it once per instance with the
  admin client, as a provisioning step.
- **Routine descriptions travel.** A routine's description is written into each fire's
  issue as its "Routine description" document, so a permalink in the routine reaches
  every run's issue and is detected there.

So an agent that files work from a conversation writes the message permalink into the
issue or routine description, the way it already writes "filed from #report-bugs". At
wake time it reads the issue's external objects, finds the permalink, and rung 1 of the
ladder applies. One write, in the object the agent creates anyway.

Later, the fork's bundled plugin can declare a `lightbulb` object provider so the object
is typed `room_thread` with a display title and Teams renders it as a chip. That is a
manifest addition in the fork's plugin, not a fork patch, and it is slice two.

## 7. Routing on our side

Rung 1 needs nothing new: the `message` tool with `lightbulb:<roomId>` and a `threadId`
already reaches the thread, and a follow-up wake replies through the plugin itself.

Rung 3 needs one door. `POST /internal/messaging/dm/{ownerId}` body
`{agentId, humanUserId}` ensures a `dm` room between the agent (`active`) and that
workspace member and returns `roomId`, reusing the DM branch of `create_room` and the
idempotent shape of `ensure_vendor_room`. A non-member is a 403. The agent resolves the
human from the Paperclip user id on the item through
`GET /internal/messaging/members/{ownerId}?paperclipUserId=`, which the backend answers
from the `paperclip-companies` rows (`paperclip_user_id` → `user_id`). Both routes are
hook-token scoped to the owner.

Rung 2 needs a room list with listen modes and topics
(`GET /internal/messaging/rooms/{ownerId}?agentId=`). No workspace has a Listening room
today except a seeded CEO in #general, so it is slice two; slice one implements rungs 1,
3 and 4.

## 8. Forge, end to end

**Kept as a commitment.**

1. Leo: "@forge it won't let me react with emoji, please put in a PR."
2. Forge's turn: opens the GitHub issue as today, and calls
   `lightbulb_follow_up {set, at: 15:00Z, note: "post the PR link for #2686 in this
   thread; if not merged, say where it stands"}`. It replies: "Opened #2686. I'll post
   the PR link here by 3pm."
3. 15:00Z: the sweeper fires the wake; the consumer wakes Forge's container with a
   `follow_up` ingress; the room shows Forge working in Leo's thread.
4. Forge, in the room session, remembers the ask, checks the PR, posts the link in the
   thread. If the PR is not up, it posts where it stands and sets a new wake.

**Kept as work.** The same ask, but Forge files a Paperclip issue for the PR with the
message permalink in the description. Paperclip links the permalink as an external
object. When Paperclip wakes Forge on that issue (assignment, comment, monitor), Forge
reads the issue's external objects, finds the thread, and posts there. If no permalink
was written, the ladder continues: the issue's creator is Forge itself, so rung 3 DMs the
workspace owner, and if the run has nothing worth saying, rung 4 leaves it on the issue.

## 9. Slice two

- **The promise on the message.** `ROOM#/WAKE#` rows drive a small line under the
  promise message, "Forge will follow up at 3:00 PM", with cancel from the thread for
  room members, published through the existing `room.resources.changed` live event.
- **Pending wakes in the agent's control panel**, with cancel.
- **Rung 2**: the rooms read route and the relevance choice.
- **Typed provenance**: the `lightbulb` object provider in the fork's bundled plugin and
  a "from #report-bugs" chip on the Teams issue.
- **Other origin kinds**: `email` replies through agent mail; `session` through the
  local-session relay.

## 10. Trust boundaries and failure modes

- Every new route is hook-token scoped to one owner. A container can only set wakes for
  rooms its agent belongs to, and only open DMs with that workspace's members. No route
  lets an agent post as a human. `create_agent_message` keeps its membership check.
- Wake rows carry the note the agent wrote and ids, never the human's message text.
- The sweeper claims with a conditional update, so two backend tasks never fire one wake
  twice. Delivery stays at-least-once end to end, as for messages; the plugin's durable
  adapter and the `IDEMP` receipt keep a retried final from posting twice.
- A wake for an agent no longer in the room, or a room that no longer exists, is
  cancelled at fire time and noted in the room.
- A `follow_up` turn that produces no final is a normal ended run; the room shows the
  existing "finished · no reply" line. The skill rule makes that rare, not impossible.
- Permalinks in Paperclip descriptions are visible to everyone who can read the issue.
  The skill's existing non-secret rule applies; a permalink is an address, not content.

## 11. Deferred, deliberately

- **Budgets.** Ruling: not now. Under AgentCore every fire invokes the runtime, so a
  schedule routine on a short cadence keeps that workspace awake and forfeits the idle
  stop. Follow-up wakes are one invoke each at their due time.
- **Backend-side "worth saying" gate.** Today's Listening gate runs inside the
  container. For ticks and events it should run in the backend so a "no" never wakes the
  microVM. Not needed while proactive volume is a handful of wakes a day.
- **A fired monitor nobody handled, in "Needs you" (#2007).** Not observed. If it
  happens: one rule in the fork's attention service beside `review`, a `monitor` source
  kind, a "Follow-up" label and a Check now action.
- **Trial rules.** Out of scope by ruling.

## 12. Testing and verification

- Backend unit: wake row idempotency and bounds; 403 on non-member agent and foreign
  room; sweeper claims exactly once under two concurrent sweeps; the consumer's `wake`
  branch builds the `follow_up` ingress and opens the ledger run with `triggerKind:
  "wake"`; DM door idempotency and 403; member resolution from a Paperclip user id.
- Plugin unit: `lightbulb_follow_up` argument validation and the `follow_up` ingress
  admission, session key, and durable delivery, beside the existing `channel.test.ts`.
- Skill: the two rules and `references/follow-ups.md` read against the tool's real
  schema.
- Dev, end to end: on the dev roster, ask Forge in a room for something that takes
  time; confirm the wake row, wait for the sweeper, confirm the run card and the reply
  in the thread; cancel a second wake from the tool and confirm it never fires. Then
  file an issue with a permalink, trigger a Paperclip wake, and confirm the reply lands
  in the thread.

## 13. Packets

1. **Backend.** Wake rows and routes, the sweeper, the consumer's `wake` branch and
   ledger run, the DM door, member lookup, `enableExternalObjects` at provisioning.
   Tests.
2. **Plugin.** `lightbulb_follow_up` tool, `follow_up` ingress kind, room-session run
   and durable delivery. Tests. Ships with an image rebuild and a pin flip.
3. **Skill.** Two non-negotiables, `references/follow-ups.md`, the provenance rule for
   filed work, `MAINTENANCE.md` anchors. Ships with the same image.
4. **Slice two**, promoted to a plan once packets 1–3 are live.

Packets 1–3 close the Forge case both ways.
