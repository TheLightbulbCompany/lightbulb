# Waiting on an External Thing (Issue Monitors)

Some work is not blocked on a person or another issue — it is blocked on a **thing that will happen on its own**: a PR gets merged, CI goes green, a deploy finishes, a vendor replies, a domain propagates. For those, schedule a **monitor**. Paperclip re-checks the issue at `nextCheckAt` and wakes you, so nobody has to sit in the loop just to tell you the thing happened.

```bash
# "Wake me when PR acme/api#1979 has landed; if it stalls, take it to the board."
/app/skills/paperclip/scripts/paperclip-api.sh monitor "$PAPERCLIP_TASK_ID" \
  --at 2026-08-25T09:00:00Z \
  --service GitHub \
  --notes "acme/api#1979 — resume the release checklist once it merges" \
  --timeout-at 2026-08-27T09:00:00Z \
  --max-attempts 8 \
  --recovery escalate_to_board
```

- `--at` (required) is the next check — an ISO-8601 instant, not a duration.
- `--service` and `--notes` say what you are watching, so a human reading the board can see the wait is real and pointed at something. Put the identifier (PR number, build id, ticket) in `--notes`: it is stored verbatim. There is **no `--ref` flag** on purpose — upstream redacts `externalRef` at write time, so anything sent there is replaced with `[redacted]` before it is stored and can never be read back.
- `--timeout-at` / `--max-attempts` bound the wait. **Always bound it.** An unbounded monitor is just a stall with better manners.
- `--recovery` is what happens when that bound is hit (default `wake_owner` if you omit it):
  - `wake_owner` — you get another heartbeat carrying the clear reason, and you decide what next.
  - `create_recovery_issue` — a high-priority child issue is filed against the source issue and its assignee is woken.
  - `escalate_to_board` — **the timeout path.** A comment is posted on the issue telling the board the wait did not pay off. Note that **no agent is woken**: this deliberately hands the problem to humans and stops. Pick it when a stalled wait is the owner's problem to solve, not yours to keep retrying.
- Clear a monitor when you no longer need it: `monitor <issueId> --clear`.
- Force an immediate re-check instead of waiting: `POST /api/issues/{issueId}/monitor/check-now`.

**Preconditions — upstream 422s otherwise.** A monitor can only be scheduled on an issue that is `in_progress` or `in_review` **and** assigned to an agent (not to a human user). So check the issue out, or set the status, *before* scheduling. Scheduling with bounds that are already in the past is also rejected.

**A monitor is one-shot.** When it fires, it is consumed — `nextCheckAt` is cleared and the monitor is stripped from the policy. If the thing you were waiting for still has not happened, schedule a new one on that wake. Do not assume it repeats.

Read the live state from `GET /api/issues/{issueId}` at `executionState.monitor` — `status` (`scheduled` / `triggered` / `cleared`), `nextCheckAt`, `lastTriggeredAt`, `attemptCount`, `clearReason`. Use it to tell "still waiting, checked twice" apart from "the wait ended and I was woken". Note the issues **list** route always returns `executionPolicy` and `executionState` as `null`; fetch the issue by id when you need monitor detail.

While a monitor is live the issue has a real liveness path, so `in_progress` (or `in_review`) is a valid posture — this is one of the explicit continuation paths in the final-disposition checklist. Do **not** additionally leave the issue `blocked` on a phantom child.

**Use a monitor instead of a `request_confirmation` card whenever the human would only be confirming a fact you could observe yourself.** Asking someone "did the PR merge yet?" spends their attention on something Paperclip can poll. A confirmation card is for a **decision** or for information only they hold. This distinction has real cost: an unnecessary confirmation card once sat for 38 hours and spawned duplicate blocker issues while everyone waited on everyone else.

## A monitor is the wait. An approval is the authority.

These are different things and you need both when both apply:

| | What it gets you | What it does NOT get you |
| - | - | - |
| **Monitor** | You are woken when the external thing happens, with no human round-trip | Permission to do anything about it |
| **Approval** (`request_board_approval`) | Authority to take a named, gated action | Any notification that the moment has arrived |

So: watching a PR with a monitor does not entitle you to merge it. If the action you intend to take on the other side of the wait is gated — by your contract, by a `ruleKey`, by company policy — you still need the approved `request_board_approval` before you take it. Schedule the monitor so you are not idling, and request the approval so you are allowed to act. Scheduling a monitor never widens what you are allowed to do.

## Surviving a Dropped Run (Task Watchdogs)

A monitor is **one-shot**: when it fires, `nextCheckAt` is cleared and the
monitor is stripped from the policy. Re-arming is your job, and it can only
happen inside a run. So if the run that was supposed to re-arm it fails, times
out, or loses its gateway connection, the wait ends silently — no error, no
notice, and the issue sits untouched until a human notices weeks later. This is
not hypothetical: it is the single largest source of dead work on this board.

A **watchdog** is the backstop that survives that. It is durable state on the
issue, independent of any run. It watches the issue's subtree and, when work
stops with no live path, files a review and wakes the owner.

**The rule: arm a watchdog whenever you arm a monitor.** They are not
alternatives. The monitor is the wait; the watchdog is what notices the wait
died.

```bash
# Arm the watchdog on the issue you just scheduled a monitor for.
jq -n --arg agentId "$PAPERCLIP_AGENT_ID" '{
  agentId: $agentId,
  instructions: "Resume the ISOA-168 release check if this stalls with no live monitor."
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST \
      "/api/issues/$PAPERCLIP_TASK_ID/watchdog"
```

- `agentId` is who gets woken when the subtree goes quiet — normally yourself.
- `instructions` is read by the agent that wakes. Say what to re-check and how
  to resume, not "investigate".
- Arm it once. It is durable and is not consumed by firing, unlike a monitor.

**Do not** arm a watchdog instead of setting a status. It reports that work
stopped; it does not make a parked issue legal.

## Tracking a GitHub PR or Issue (External Objects)

When an issue waits on a GitHub pull request or issue, **do not describe it in
prose**. Prose is not readable by anything: a description that says "waiting for
PR 1934 to merge" leaves nothing in the system capable of noticing when it does.
Register it as an **external object** instead, and refresh that object whenever
you need its state.

```bash
# Register the PR this issue waits on.
jq -n '{
  provider: "github",
  kind: "pull_request",
  url: "https://github.com/TheLightbulbCompany/lightbulb/pull/1934"
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST \
      "/api/issues/$PAPERCLIP_TASK_ID/external-objects"

# Re-read live state before you say anything about it.
/app/skills/paperclip/scripts/paperclip-api.sh raw POST \
  "/api/issues/$PAPERCLIP_TASK_ID/external-objects/refresh"
```

**State a PR, issue, CI, or deploy fact only from a refresh you ran in this
run — never from memory and never from an earlier run.** Cite the refresh in
your comment. Every review rejection recorded on this board so far was an agent
asserting external state it had not re-read: "the plan says ISOA-120 was
blocked, but live search shows it is still backlog"; "Backend CI is still
in_progress"; "PR 1934 is still open" — written after it had merged.

If the refresh fails or the provider is unreachable, **say the refresh failed**.
Do not fall back to what you remember, and do not report an unverified claim as
verified. A tool failure narrows what you may assert; it never lowers the
evidence bar.

Registering an external object does not replace the monitor and watchdog. Use
all three: the object holds the truth, the monitor wakes you to look, the
watchdog notices if the waking stops.

