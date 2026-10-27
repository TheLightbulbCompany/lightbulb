# Approval and review policy (14 October 2026)

Goal: nothing waits on a founder except a production deploy and the few calls only a founder can make. Based on how the board actually works (`22-paperclip-work-model.md`).

## Who decides what

| Decision | Who | How |
|---|---|---|
| Merge a pull request | The coordinator, or the agent that opened it | When Codex's review of the head commit is clean and CI is green. Merge through nitpicks. Three review rounds at most, then a person looks. |
| Production deploy | The coordinator, as Prasiddha | One train at a time, when nothing is in flight. At most one train every two hours. |
| Roll agent runtimes after an image pin flip | The coordinator | One-off ECS task, after the deploy lands. |
| Board approvals (hire, CEO strategy, budget override, board approval) | Any founder, or the coordinator as Prasiddha | Decide within the hour. A rejection must carry a comment so the agent wakes (the system does not wake it on reject). |
| "Ask first" tool cards | Whoever is in the room | Within 60 minutes, or the card expires. |
| Questions an agent asks on an issue | The coordinator first | Answer from the spec. Escalate to a founder only when the spec does not settle it. |
| Public posts, customer messages, store and directory submissions | A named founder | Agents draft. A founder posts. |
| Money, IAM, security groups, production Clerk settings, secret rotation | Prasiddha | One-line ask. |

## Rules for agents

- Decide from the spec. Ask only when the spec does not settle it, and ask once.
- Never file an issue asking a founder to merge or deploy. Say on the issue that the PR is ready.
- Hand work back with `in_review` and a comment saying what is done and what is left.
- Close an issue with one PATCH that carries the status and the comment together.
- An issue assigned to a person gets no reminder from the system. If you need that person, say so in the founders' room.

## Changes to make in the product (from the work model, ranked)

1. Wake the requesting agent on reject, as approve already does.
2. Batch "approve all" in "Needs you" for low-risk kinds.
3. Mark read-only tools as allowed, not "ask first".
4. Let the CEO move specialist backlog to todo on its timer.
5. Default review policy to `anyone`; keep `human_only` for money and public posts.
6. Remind people of issues assigned to them after 24 hours.
7. Show board-owned blocked issues in "Needs you".
8. A bulk close that sends one PATCH with status and comment.
9. "Mine" shows only issues assigned to me by default.
