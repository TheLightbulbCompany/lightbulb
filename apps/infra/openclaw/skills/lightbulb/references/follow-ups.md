# Keeping a promise made in chat (follow-ups)

You only run when something wakes you. A sentence like "I'll post the link here"
wakes nothing. The thing that wakes you is a **Paperclip task** that changes state,
and the thing that tells the future you *where* to report is the **message
permalink** written into that task. So a promise is four steps, all in the turn
where you make it.

## 1. File the work as a task that carries the permalink

The room context (`Lightbulb room` structured context) gives you `room.id` and
`message.id` / `message.threadRootMessageId`. Build the permalink of the message you
are answering:

```
https://flickmeon.ai/chat?panel=chat&room=<room.id>&message=<message.id>
```

(on dev, where `$ISOL8_API_URL` contains `api-dev`, the host is `dev.flickmeon.ai`).

File the task to yourself, and put the permalink in the description:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" \
  --title "Allow emoji reactions on teammate messages" \
  --assignee "$PAPERCLIP_AGENT_ID" --status todo <<'EOF'
Filed from https://flickmeon.ai/chat?panel=chat&room=<room.id>&message=<message.id>

Leo cannot react to a teammate's message; the API returns 400. Ship a PR.
EOF
```

`--status todo` starts a run immediately. Use `backlog` only when the work should
wait. A GitHub issue alone is NOT a task: nothing wakes you from it.

## 2. Reply in the room with what you filed

"Filed ISOA-320. I'll post the PR link here." The task identifier is the receipt.

## 3. When the run finishes or the plan changes, reply in the thread

Every later wake on that task (the run starting, a comment, a monitor firing, a
blocker clearing) reads the task, and the task's description holds the permalink.
Post the outcome **in the thread under that message, never top-level**, with the
`message` tool:

```
message  action: "send"
         channel: "lightbulb"
         target: "lightbulb:<room.id>"
         threadId: "<message.threadRootMessageId if set, otherwise message.id>"
         message: "PR is up: https://github.com/…/pull/2700 — merges once CI passes."
```

Then comment on the task as usual. The thread is for humans; the task comment is
the record.

## 4. Waiting on something external is a monitor, not a sentence

If the outcome depends on a thing that will happen on its own (a PR merging, CI
finishing, a deploy landing, a vendor replying), set a **bounded monitor** on the
task and say so once in the thread:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh monitor "$PAPERCLIP_TASK_ID" \
  --at 2026-09-16T16:00:00Z --service GitHub \
  --notes "PR #2700 — post the merge result in the thread" \
  --timeout-at 2026-09-17T16:00:00Z --max-attempts 8 --recovery escalate_to_board
```

The monitor wakes you at `--at`. Full rules: the `paperclip` skill,
`references/monitors.md`.

## When to speak in the thread, and when not

- **Speak** when the promised thing exists (the link, the answer, the deploy), when
  the promise cannot be kept as stated (a different approach, a timeout, an
  escalation), or when a human asked in the thread.
- **Stay silent** on a wake that changed nothing: CI still running at a monitor tick,
  a heartbeat with no progress, a re-armed wait. Say "waiting on CI" once, when the
  plan changes from now to later, not every hour.
- **Never repeat what "Needs you" already shows.** An approval, a question, or a
  blocker on a human is an interaction on the task; the Teams feed and the phone
  push already carry it. Raising it in the thread too is the same ask twice.
