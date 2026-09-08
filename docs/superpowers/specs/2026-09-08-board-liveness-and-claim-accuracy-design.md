# Board liveness and claim accuracy — design

**Status:** approved for planning · **Date:** 2026-09-08 · **Scope:** fleet-wide

Two symptoms on the founder board (Paperclip company `Isol8`,
`1071914e-6b9a-4351-a1d6-58bffe2e30f1`) share one root cause: work parks and is
never resumed, and agents assert external state they did not re-read.

---

## 1. Evidence

All figures read from the prod Paperclip Aurora cluster on 2026-09-08 and from
the deployed fork commit `987520f9` (`paperclip-version.json`, both envs).

### The board

230 issues, 50 open. **42 of the 50 have not been touched in ≥12 days.**

| status | n | age |
|---|---|---|
| backlog | 20 | all 16–31 days |
| in_review | 14 | 12 of 14 ≥12 days |
| blocked | 13 | 7 ≥14 days |
| todo | 3 | 16–23 days |

Composition of the 50: ~12 real current work, 10 dead, 7 routine-execution rows
(`origin_kind = routine_execution`, not tasks), 9 items of an abandoned August
program (all children of ISOA-68; every routine that program created was paused
on 2026-08-20), plus 12 human-gated items.

83 of 230 issues (36%) are unassigned.

### Run outcomes

367 runs: 263 succeeded, 82 failed, 12 cancelled, 10 timed out (**71.7%**).

Of the 72 last-30-day failures carrying an error code, **61 are transport**:
`openclaw_gateway_wait_error` 41, `timeout` 9, `openclaw_gateway_timeout` 7,
`openclaw_gateway_request_failed` 3, `process_lost` 1. This is uptime, not
reasoning — but see §2, where it becomes load-bearing.

### Claim accuracy

`issue_execution_decisions`: 19 `approved`, 8 `changes_requested` (70% first
pass). Only 27 decisions exist across 230 issues; `review_policy` is NULL on all
230.

Every one of the 8 `changes_requested` bodies is the same class of defect — an
agent asserting external state it had not re-read:

- "the plan document says the decision was created, which contradicts the recorded caveat"
- "the plan says ISOA-120 was blocked, but live search at 03:08 UTC shows it is still `backlog`"
- "X was not actually searched — `xurl` returned 402 — but the disposition claims a complete no-signal across all required sources"
- "Backend CI is still `in_progress`" / "Infra CI failed"

None is a reasoning failure. All are grounding failures.

### Primitive inventory (verified against deployed commit `987520f9`)

| primitive | server | API route | documented to agents | prod rows |
|---|---|---|---|---|
| issue monitor | ✅ `tickDueIssueMonitors` | `POST /issues/{id}/monitor/check-now` | ✅ **well** (shipped skill §"Waiting on an External Thing") | 3 fleet-wide |
| `unblockDescriptor` + agent wake | ✅ `routable-blocked.ts` | `PATCH /issues/{id}` | ❌ neither skill | 3 of our 13 blocked |
| external objects | ✅ + `github-external-object-provider.ts` | `POST /issues/{id}/external-objects`, `…/refresh` | ❌ neither skill | **0 fleet-wide** |
| task watchdog | ✅ `task-watchdogs.ts` | `POST /issues/{id}/watchdog` | one line of `api-reference.md` | **0 fleet-wide** |
| recovery / liveness classifiers | ✅ `recovery/issue-graph-liveness.ts` | — | partial | 26 actions fired |

**Nothing in this design requires a Paperclip server patch.** Every mechanism is
built, deployed, and reachable by API today.

---

## 1b. Corrections — verified during implementation (2026-09-08)

Three claims in §1 were wrong. All were checked against the deployed fork commit
`987520f9` and the live prod database; the corrections, not the originals, are
what shipped.

**External objects are NOT reachable, and their 0 rows prove nothing.**
`POST /issues/{id}/external-objects` does not exist — only `GET .../external-objects`,
`GET .../external-object-summary` and `POST .../external-objects/refresh` are
registered. Objects are created server-side by `syncIssue`/`syncComment` scanning
URLs out of issue text. The whole service is gated on
`experimental.enableExternalObjects`, and `instance_settings.experimental` is `{}`
in **both prod and dev** — so the feature is off and 0 rows is the flag, not agent
ignorance. The primitive was dropped from the design entirely. The accuracy rule it
was meant to carry survives, tool-agnostic: an agent re-checks with `gh`, the GitHub
MCP, or the CI API. Cases (`enableCases`) is off for the same reason.

**`unblockDescriptor` is not a staleness fix, and agents cannot use it as designed.**
Two independent problems. First, actor: `issues.ts:8884` throws
`403 "Agents may only name themselves as an unblock owner"` for `owner: "board"`
or any `userId`, and `:8894` for another agent's id. The design's
"human decision → `owner: board` → owner queue" row is a **user-actor** action,
available from the Lightbulb UI, not something an agent can do. Second, mechanism:
`routable-blocked.ts:36-53` fires **once**, at the blocked transition, guarded by
`blockedOwnerNotifiedAt`, with no watch for the block clearing. It is a one-shot
notification, not a re-check. For a self-owned block it notifies the agent that
just blocked the issue — a no-op.

**Therefore the monitor + watchdog pair is the ONLY real re-check mechanism**, and
it is the whole of what §3 Part 2 delivers. Watchdogs are ungated
(`task-watchdogs.ts` has no `isEnabled`, the route has no experimental guard), so
their 0 rows fleet-wide IS genuine agent ignorance — the one place the original
reasoning held.

**Systemic lesson.** The fork's skill is written for adapters that mint a run-bound
agent JWT. We run `openclaw_gateway`, registered `supportsLocalAgentJwt: false`, so
every agent holds a long-lived `agent_key`. Any vendored section whose route requires
`source === "agent_jwt"` is dead for the whole fleet — that is how upstream's
credential-proposal guidance shipped broken. **Re-vendoring needs an actor-and-flag
capability filter, not a heading diff.**

---

## 2. Five whys

**1. Why is the board 26 days stale?** The waiting-on condition of each parked
issue is never re-checked.

**2. Why?** The monitor that re-checks it is **one-shot**. The shipped skill
states this plainly: "When it fires, it is consumed — `nextCheckAt` is cleared
and the monitor is stripped from the policy." Re-arming is the agent's job on
each wake.

**3. Why did agents stop re-arming?** Re-arming can only happen inside a run. If
that run fails, times out, or the agent judges wrong, the chain ends silently
with no error anywhere. The data shows exactly this: ISOA-190 re-armed twice
(`monitor_attempt_count` 3), missed the third, and has been dead 15 days.
ISOA-198 re-armed nine times and is the only non-routine open issue that is not
stale. **This is where the 28% run-failure rate becomes load-bearing** — a
dropped run is a dropped chain, and 85% of those drops are gateway transport.

**4. Why does nothing catch a dropped chain?** The backstop exists —
`task-watchdogs.ts` detects stopped subtrees and files a review — and it has
**zero rows fleet-wide**. Nothing has ever armed one.

**5. Why has nothing ever armed one?** The watchdog is documented in a single
line of `references/api-reference.md` and appears in the body of neither the
shipped skill nor the fork's. Contrast monitors, which are documented well and
therefore *are* used — by three issues, all of them ours.

**Terminal cause: issue liveness is agent-voluntary and single-threaded through
a run type that fails 28% of the time, and the designed backstop is never
armed.**

The same cause produces the claim-accuracy defects. An agent that has never
created an `external_object` for PR 1934 can only assert its state from memory,
which is exactly what the `changes_requested` verdicts caught. `external_objects`
has never been written once by anyone.

### Worked case: ISOA-168 → ISOA-166 → ISOA-127

ISOA-168 reads "wait for `Isol8AI/isol8#1934` to go green and reach a deployed
backend build." **PR 1934 merged 2026-08-23.** Three issues have been unblocked
for 15 days. No external object was ever created for PR 1934; no monitor
survived; no watchdog was armed. Nothing in the system was capable of noticing.

---

## 3. Design

### Part 1 — close the skill drift and write the missing sections

`apps/infra/openclaw/skills/paperclip/` is a hand-maintained copy of the fork's
`skills/paperclip/`. Section-level drift today:

Present in the fork, missing from ours:
`### Monitors and Watchers`, `### Delegating review tasks`,
`## Managing A User's Inbox`, `### Standalone Decisions`,
`## MCP Tool Approval Gates`, `## Cases`, `## Proposing Credentials Safely`,
`## Reading Granted Secrets`, `## Searching Issues`.

Present in ours, missing from the fork (**ours is better here — do not lose
these**): `## Writing to the Paperclip API (required)`,
`### Creating work for someone else — always say the status`,
`## Webchat and interactive sessions (no run id)`,
`## Waiting on an External Thing (Issue Monitors)`,
`### A monitor is the wait. An approval is the authority.`,
`## Company Memory, Decisions, and Attention`.

The merge is a union, not a replacement. Then author the three sections that
exist in **neither** skill:

1. **Task watchdogs.** What `POST /issues/{id}/watchdog` does, and the rule:
   **arm a watchdog whenever you arm a monitor.** The monitor is the wait; the
   watchdog is what survives a dropped run. This is the fix for why-4.
2. **External objects.** `POST /issues/{id}/external-objects` and `…/refresh`
   for GitHub PRs and issues. The rule: **a claim about a PR, issue, or CI run
   must cite a refreshed external object, never memory.** This is the fix for
   the claim-accuracy defects.
3. **`unblockDescriptor`.** Required whenever an issue enters `blocked`, naming
   the owner and the action. An agent owner is woken by `routable-blocked.ts`;
   a `board` owner deliberately wakes nothing and lands in the owner queue.

**No drift guard** (owner decision, 2026-09-08). See Part 1b instead.

**Delivery lane:** skill files live in the extended OpenClaw image →
`build-openclaw-image.yml` → version pin bump → container reroll via the admin
UI. Merging alone ships nothing.

### Part 1b — make the drift structurally impossible

The copy exists because nothing pulls the real thing. Change
`apps/infra/openclaw/Dockerfile` to copy `skills/paperclip/` out of the pinned
Paperclip image at build time, layering our own additions on top rather than
maintaining a whole fork of the file. Then the skill cannot drift from the
server, because it is the server's, and no CI guard is needed.

The sections listed above as "ours is better" must be preserved as an overlay,
not discarded.

### Part 2 — arm the primitives

No server change. The behaviour change lands entirely in Part 1's skill content:

| condition type | primitive | who is woken |
|---|---|---|
| machine, pollable (CI, deploy, DNS) | monitor **+ watchdog** | assignee agent |
| machine, GitHub-shaped (PR, issue) | external object + refresh, **+ watchdog** | assignee agent |
| human decision or action | `unblockDescriptor` with `owner: board` | nobody — owner queue |
| no condition nameable | none — the issue must close | — |

Human-gated items get **no expiry and no auto-close** (owner decision): they
surface as one honest count on the board and one line in the existing Weekly
operations digest routine. `routable-blocked.ts` already implements exactly this
by returning `false` for a `board` owner.

**New-tenant behaviour.** A fresh company has nothing connected, so it has no
machine conditions: no monitors, no external objects, no polling, no cost. Its
work is almost entirely human-gated, which is a queue with an honest count and
no nagging. The "must name a condition" rule is where a new tenant gains most —
it prevents the ISOA-68 pathology, which was nine aspirational items filed in
one sitting with nothing to wait on. That is precisely what Friday does to a new
owner in week one.

### Part 3 — audit and repair the founder board

**Every one of the 50 open issues is audited individually before any write.**
No bulk rule, no status-based sweep. For each issue, in order:

1. **Is it already done?** Check the world, not the board. (Example: ISOA-180
   "Setting up company bookkeeping software" — QuickBooks shipped to prod
   2026-09-07.)
2. **Is it still useful?** Does it serve something we are doing in September?
3. **Is its premise still true?** (Example: ISOA-190 asks for a
   `www.isol8.co` DNS record; that brand is retired and the domain is being
   decommissioned, so the request is inverted — the real work is fixing Meta's
   dead `isol8.co` URLs.)
4. **Then, and only then**, classify: close / repair with a typed condition /
   leave alone.

Repair means writing the field the agent should have written — an external
object, a monitor plus watchdog, or an `unblockDescriptor` — through the same
API an agent would use. Not direct SQL.

Known starting points, all to be re-verified during the audit rather than
trusted from this document:

- **Machine-gated, repair:** ISOA-168 / 166 / 127 (PR 1934 merged 2026-08-23),
  ISOA-113 / 114 (GitHub issue 1795 still open, blocks Meta App Review),
  ISOA-190 (premise inverted), ISOA-75, ISOA-134 (alarm names predate the
  CloudWatch rename), ISOA-63 (likely the same defect as the 41 gateway errors).
- **Human-gated, queue:** ISOA-186 (six named customers, `todo`, unassigned,
  16 days), ISOA-121, ISOA-117, ISOA-120, ISOA-89, ISOA-207 / 215 / 208.
- **Likely close:** the nine ISOA-68 children (30, 33, 34, 37, 79, 86, 87, 88,
  89), ISOA-102, ISOA-116, ISOA-126, ISOA-12, ISOA-180, ISOA-181, ISOA-188.
- **Not tasks:** ISOA-139, 144, 160, 199, 206, 221, 223 are routine executions.
  ISOA-148 and ISOA-150 are demo fixtures whose own text says to leave them
  parked. Neither set should be closed; both should be filtered out of the board
  via `excludeRoutineExecutions`, which `GET /teams/issues` already supports and
  the Tasks panel does not pass.

---

## 4. What this design does not do

- No Paperclip server patch, no base bump, no fork release.
- No review-policy gate. The `changes_requested` verdicts show the reviewer was
  polling GitHub and CI, not judging quality — that is a machine's job, and
  Part 1's external-object rule does it without a serial human step.
- No expiry engine or auto-close for human-gated work.
- No CI drift guard (Part 1b removes the need).
- No knowledge-graph or TrustGraph work. Evaluated separately and rejected:
  TrustGraph needs Cassandra, Qdrant, Pulsar and Garage per deployment against
  our 1 vCPU / 4 GB per-tenant budget, and OpenClaw's `memory-wiki` already
  provides a per-tenant typed graph with provenance. Of the eight recorded
  quality failures, one is graph-shaped and seven are grounding-shaped, so a
  graph is not the accuracy fix.

## 5. Verification

- Part 1: the merged skill contains every section from both sides plus the three
  new ones; `paperclip-api.sh` examples in the new sections execute against dev.
- Part 1b: image build produces a skill tree whose monitor section matches the
  pinned Paperclip image, with our overlay sections intact.
- Part 2: after a dev container reroll, an agent asked to wait on a real PR
  creates an external object **and** arms a watchdog; `issue_watchdogs` goes
  non-zero for the first time.
- Part 3: every open issue ends the audit either closed, or carrying a typed
  condition, or explicitly recorded as intentionally untouched. Zero open issues
  with no recorded disposition.
