# Proactive agents: promises are tasks that know where they came from

Status: proposed, 2026-09-16, fourth draft. Owner rulings in §3. One implementation plan
is written from §6; this file is deleted when it ships.

## 1. Goal

An agent that says "I'll post the link here" comes back and posts the link there.

## 2. Why nothing does this today

On 2026-09-16 Forge answered Leo in #report-bugs, opened GitHub issue #2686, wrote "I'm
tracking it through to the PR and will post the link here", and never ran again (run
ledger: one `user_request` run, 14:55:31Z to 14:56:49Z, completed). Two things went
wrong, and neither is a platform gap:

- Forge filed the work where nothing wakes it. A GitHub issue is inert. A Paperclip task
  assigned to itself in `todo` would have started a run immediately, and every later
  change to that task (a comment, a monitor firing, a blocker clearing) is a wake.
- Nothing told Forge to report where the promise was made. A Paperclip run's output goes
  to the task's comments. The `message` tool already reaches any room the agent belongs
  to, from any session, with only a membership check behind it. Forge did not know to
  use it, and the task did not know which thread it came from.

## 3. Rulings and non-goals

Owner rulings, 2026-09-16:

- The OpenClaw agent is the first-class primitive. Paperclip is how work is tracked, and
  a promise about work is work.
- Self-directed: agents create their own tasks and monitors, no approval gate.
- Governed: the promise is a task on the board, in the activity log, cancellable there.
- Paperclip may know where work came from, never where to speak.
- No timers that are not attached to work. No sweeper, no scheduler, no wake table.
- No budgets, no trial rules, nothing built for a failure not observed.

Non-goals for this draft: routing unsolicited output (a Listening room, a DM); a
follow-up primitive of our own; any backend or infrastructure change.

## 4. The rule

**A promise made in a conversation becomes a Paperclip task that carries the
conversation's permalink. The task's own lifecycle wakes the agent. The run that
finishes or parks the task reports in that thread. Waiting on something external is a
monitor on the task.**

Every mechanism in that sentence exists. What is missing is the agent knowing it, plus
one identifier the room turn does not hand over yet.

## 5. How it works

**File.** In the room turn, the agent files the work as a Paperclip task assigned to
itself, `--status todo` if it should start now or `backlog` if not, and puts the message
permalink in the description:

```
Filed from https://flickmeon.ai/chat?panel=chat&room=<roomId>&message=<messageId>
```

The plugin's "Lightbulb room" structured context already gives the agent `room.id`; it
gains `message.id` and `message.threadRootMessageId` so the permalink is thread-precise
(one line in `channel.ts`, beside `room`). The agent replies in the room with the task
identifier and what it will do: "Filed ISOA-320. I'll post the PR link here."

**Wake.** Paperclip does what it does today. A `todo` task starts a run at once. A
comment, a reassignment, a resolved blocker, a fired monitor, or a routine tick starts
another. The run reads the task, sees the permalink, and knows where it came from.

**Report.** The run ends by posting its outcome where the promise was made: the
`message` tool, `action: send`, target `lightbulb:<roomId>`, thread set to the
permalink's message id, which the plugin maps onto `threadRootMessageId`
(`channel.ts:217`). Done, or where it stands. Then the task comment, as today.

**Wait.** When the outcome depends on something that will happen on its own, a PR
merging, CI finishing, a deploy landing, the run sets a **monitor** on the task
(`paperclip-api.sh monitor <id> --at <ISO> --notes … --timeout-at … --recovery …`) and
reports "waiting on X, I'll check at T" in the thread. The monitor fires, Paperclip wakes
the agent, the run reports again. An unbounded wait is forbidden by the existing
monitors reference; a bounded one that times out follows its recovery policy, and that
too is reported in the thread.

**Cancel and see.** The task is on the board with its permalink, its monitor row, and its
activity. Closing it is cancelling the promise. Nothing is added to the room until the
agent speaks.

**Forge, end to end.** Leo asks for the PR. Forge files ISOA-320 to itself in `todo` with
Leo's permalink and replies "Filed ISOA-320, I'll post the PR link here." Paperclip
starts the run. Forge writes the PR, opens it, and posts the link in Leo's thread. If the
PR needs CI, Forge sets a monitor for an hour out, posts "PR #2700 is up, waiting on CI,
I'll confirm here", and the monitor's wake posts the result.

## 6. Changes

- **`lightbulb` skill.** `references/follow-ups.md` with the four steps above and the
  permalink format; a routing-table row in `SKILL.md`: *You are about to tell a human you
  will follow up, check back, track, or post later → `references/follow-ups.md`*; two
  non-negotiables:
  - *A promise is a task.* Never say you will follow up, track, or post later unless
    you have filed the Paperclip task with the permalink in that same turn. If you
    cannot, say what you did instead.
  - *A run that changes a task with a permalink ends in that thread.* Done, or where it
    stands and what you are waiting on.
- **`paperclip` skill.** One sentence at the top of `references/monitors.md`: *A
  promise you made in chat that depends on an external thing is a monitor on the task,
  not a sentence.* The `SKILL.md` "Wait on an external thing" row already routes there.
- **Plugin.** `message: { id, threadRootMessageId }` added to the "Lightbulb room"
  structured context payload in `channel.ts`.
- **`MAINTENANCE.md`** anchors for both skills.

Ships as one image rebuild and pin flip. No backend, no infrastructure, no Paperclip
change.

## 7. Trust and failure

- Nothing new is exposed. The `message` tool goes through `create_agent_message` with
  its membership check; a permalink is an address in a task description, visible to
  whoever can read the task, never the human's text.
- The wake is guaranteed by Paperclip. The report depends on the agent following the
  skill at two moments: filing with the permalink and speaking at the end. That is the
  same trust the skill already places in every other non-negotiable.
- A task with no permalink behaves exactly as today: output stays on the board.
- A task filed to another agent carries the permalink too, so the courier pattern reports
  back the same way.

## 8. Deferred, deliberately

- **Automatic report-back.** The fork already forwards `agent.run.finished` to
  `routers/paperclip_events.py`. If the skill rule proves flaky, that hook can read the
  task's permalink and post the run's final comment into the thread itself. Not built
  until the flakiness is observed.
- **Typed provenance.** Paperclip's external-object detector (behind
  `enableExternalObjects`) would link the permalink as an object and Teams could show a
  "from #report-bugs" chip. Cosmetic.
- **Routing unsolicited output** (Listening rooms, DMs to the item's human): a later
  spec, once routine output needs a home. The ladder and DM door from the second draft
  live in git history.
- **Timers not tied to work**, budgets, the backend-side "worth saying" gate, and fired
  monitors nobody handled in "Needs you" (#2007). Not observed, not built.

## 9. Verification

- Skill: the existing skill tests read the new reference and rows; `MAINTENANCE.md`
  lists the anchors.
- Plugin: one unit test that the structured payload carries `message.id` and
  `message.threadRootMessageId`, beside the existing context tests.
- Dev, end to end: in a dev room, ask Forge for something that takes a few minutes.
  Confirm the task exists with the permalink and a run started; confirm the reply lands
  in the thread. Then ask for something that waits on CI; confirm the monitor row on the
  task, the "waiting" reply, and the post-monitor reply in the thread.
