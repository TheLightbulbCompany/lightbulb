# Proactive agents: follow-up wakes

Status: proposed, 2026-09-16, third draft (ponytail pass). Owner rulings in §3. One
implementation plan is written from §6; this file is deleted when it ships.

## 1. Goal

An agent that says "I'll post the link here" comes back and posts the link there.

## 2. Why nothing does this today

On 2026-09-16 Forge answered Leo in #report-bugs, opened GitHub issue #2686, wrote "I'm
tracking it through to the PR and will post the link here", and never ran again (run
ledger: one `user_request` run, 14:55:31Z to 14:56:49Z, completed). An agent runs only
when woken, the wake sources are a room message, a Paperclip run, and a heartbeat with no
delivery target and no process under AgentCore, and nothing gives an agent a turn to keep
a promise in. On AgentCore every turn is `InvokeAgentRuntime` from the backend, so the
question is only who writes the trigger and what the invoke carries.

## 3. Rulings and non-goals

Owner rulings, 2026-09-16:

- The OpenClaw agent is the first-class primitive. Paperclip is how work is tracked.
- Self-directed: agents create their own wakes, no approval gate.
- Governed: a commitment is visible where it was made and cancellable there.
- Paperclip never learns where to speak. Rooms differ per workspace.
- No budgets, no trial rules, no sweeper, and nothing built for a failure not observed.

Non-goals for this draft: routing anything anywhere but the thread the promise was made
in; events and loops (Paperclip routines already run them; where their output goes is a
later spec); a wake table; a new ingress kind in the plugin.

## 4. The primitive

A **follow-up wake** is a one-shot timer, set by an agent from inside a room turn, that
at its due time puts a message in that thread addressed to that agent. The agent then
runs an ordinary room turn and replies in the thread. Every part of that path except the
timer exists today.

## 5. How it works

**Set.** The channel plugin registers `lightbulb_follow_up`, the same way it registers
`lightbulb_local_sessions`, so the tool exists only inside a room turn and already knows
the owner, agent, room and thread root. The agent passes only what it knows:

```
lightbulb_follow_up { action: "set",    at: <ISO>, note: <text> }  -> { wakeId, at }
lightbulb_follow_up { action: "cancel", wakeId }
lightbulb_follow_up { action: "list" }                               -> pending wakes in this room
```

`at` is at least one minute out and at most 30 days. The tool calls
`POST /internal/messaging/wakes/{ownerId}` (hook token). The backend checks the agent is
a current member of the room and creates one **EventBridge Scheduler** one-shot:

```
group:   lightbulb-{env}-wakes
name:    wake-<ownerHash8>-<agentId8>-<threadOrRoomId8>-<dueAtEpoch>
expr:    at(<dueAt>)            ActionAfterCompletion: DELETE
target:  dynamodb:PutItem on the messaging table
item:    ROOM#<room> / WORK#WAKE#<dueAtEpoch>#AGENT#<agent>
         { kind: "wake", ownerId, roomId, agentId, threadRootMessageId?, note,
           messageGroupId: <room+agent>, dueAt, wakeId }
```

The schedule is the record. A second `set` with the same name is a no-op that returns
the existing `wakeId`. `cancel` is `DeleteSchedule`; `list` is `ListSchedules` in the
group filtered by the owner and room prefix. Nothing new is stored in DynamoDB until the
wake fires. No sweeper, no polling.

**Fire.** At `dueAt` the schedule writes the `WORK#WAKE` row. That insert rides the path
every room message rides: the DynamoDB stream, the EventBridge Pipe (`sk` prefix
`WORK#`), `openclaw-work.fifo`, and `openclaw_work.py`. The consumer's pointer parser
already dispatches on `kind` (`room_dispatch`, `runtime_operation`); it gains `wake`.
The `wake` branch re-checks membership and then writes one canonical message into the
thread through `create_message`:

```
author:           { kind: "system", id: "follow-up", displayName: "Follow-up" }
text:             "Follow-up for @forge: <note>"
threadRootMessageId: <from the row>
dispatch_targets: [ { agentId, eventKind: "user_request" } ]
source_agent_id:  null
```

That message is a normal `MSG` row with a normal `WORK#MESSAGE` row, so from here on
nothing is new: the FIFO, the consumer, the plugin's private ingress (which already
accepts a `system` sender on a `user_request`), the agent's room session, the run card,
live frames, abort and steer, the durable reply into the thread, and the usage callback.
Members see the follow-up line in the thread, then the agent working, then its reply.

**Skill.** Two non-negotiables in the `lightbulb` skill plus `references/follow-ups.md`:

- *A promise needs a wake.* Never say you will follow up, check back, track, or post
  later unless you have called `lightbulb_follow_up` in that same turn. If you cannot,
  say what you did instead.
- *A wake ends with a message.* The turn a follow-up wakes ends with a reply in the
  thread: done, or where it stands plus a new wake.

**Forge, end to end.** Leo asks for the PR. Forge opens #2686 and calls
`lightbulb_follow_up {set, at: 15:00Z, note: "post the PR link for #2686; if not merged,
say where it stands"}`, then replies "Opened #2686, I'll post the link here by 3pm." At
15:00Z the schedule writes the row, the consumer posts "Follow-up for @forge: post the
PR link for #2686…" in Leo's thread, Forge runs in that room session, and posts the
link, or where it stands and a new wake.

## 6. Code

- **Infra** (`api-stack.ts`, beside the Pipe): a schedule group per env and one role
  Scheduler assumes with `dynamodb:PutItem` on the messaging table, conditioned to
  `sk` prefix `WORK#WAKE#`. The backend task role gets `scheduler:Create/Delete/Get/
  ListSchedules` on that group.
- **Backend**: `routers/messaging.py` three hook-token routes; `core/services/wakes.py`
  Scheduler CRUD plus the membership check; `openclaw_work.py` the `wake` pointer shape
  and branch (~25 lines); `create_message` accepts the `system` author it already types.
- **Plugin**: the `lightbulb_follow_up` tool, modeled on `local-sessions.ts`. Ships with
  an image rebuild and a pin flip, together with the skill text.
- **Clients**: render a `system` author as a muted line if they do not already.

## 7. Trust and failure

- Routes are hook-token scoped to one owner; a container can only set wakes for rooms
  its agent belongs to. The fired message is authored `system`, never as a human, and the
  agent's reply goes through `create_agent_message` with its membership check intact.
- The row carries the agent's note and ids, never the human's text.
- Delivery is at-least-once, as for messages: the schedule name is the idempotency key
  at set time, the `WORK#WAKE` sort key at fire time, and the plugin's durable adapter
  plus `IDEMP` receipt at reply time.
- A wake whose agent left the room, or whose room is gone, is dropped by the membership
  check with a log line; nothing is posted.
- A wake that fires while the agent is mid-run in that room queues behind it on the same
  FIFO group, as messages do.

## 8. Deferred, deliberately

- **Where unsolicited output goes** (a Listening room, a DM to the item's human, the
  issue): a later spec, once Paperclip routine output needs a home. The ladder and the
  DM door from the second draft live in git history.
- **Provenance for filed work** (the message permalink in an issue, linked by Paperclip's
  external-object detector behind `enableExternalObjects`): add when an agent first files
  work from a room and needs to report back on a Paperclip wake.
- **"Forge will follow up at 3pm" on the promise message, cancel from the thread, and
  pending wakes in the control panel**: add when someone asks to see or cancel one;
  `ListSchedules` already answers it.
- **Budgets**; a backend-side "worth saying" gate; fired-but-unhandled monitors in
  "Needs you" (#2007). Not observed, not built.

## 9. Tests

- Backend: wake create validates bounds and membership (403 on non-member and foreign
  room); duplicate `set` returns the existing id; the consumer's `wake` branch writes
  the system message with exactly one `user_request` target and drops a wake whose agent
  left; pointer parser accepts `wake` and rejects a malformed one.
- Plugin: tool argument validation and the request it sends, beside the existing tests.
- Dev, end to end: ask Forge for something slow, confirm the schedule exists, wait for it,
  confirm the follow-up line, the run card and the reply in the thread; cancel a second
  wake and confirm it never fires.
