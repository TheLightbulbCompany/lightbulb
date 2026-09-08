# Board Liveness and Claim Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make parked Paperclip issues resume themselves and stop agents asserting external state they have not re-read, by teaching the bundled skill three primitives that already ship in the server, then repairing the founder board issue by issue.

**Architecture:** No Paperclip server change. Every mechanism (issue monitors, task watchdogs, external objects, `unblockDescriptor`) is already built and deployed at commit `987520f9`. The whole behaviour change lands in `apps/infra/openclaw/skills/paperclip/`, ships in the extended OpenClaw image, and reaches agents on a container reroll. The board repair then uses the same REST API an agent would use.

**Tech Stack:** Markdown skill files, bash (`paperclip-api.sh`), vitest (`apps/infra/test/`), Paperclip REST API, AWS RDS Data API for read-only verification.

**Spec:** `docs/superpowers/specs/2026-09-08-board-liveness-and-claim-accuracy-design.md`

## Global Constraints

- **The six ISOL8-LOCAL edits in `SKILL.md`'s header comment are incident-driven and MUST survive every edit in this plan.** They are enumerated in the file's `<!-- ISOL8-LOCAL EDITS -->` block: (1) write-path helper mandate, (2) run-id truthfulness, (3) board hygiene / no probe issues, (4) helper script flags, (5) absolute `/app/skills/paperclip/scripts/...` paths, (6) explicit `backlog` on assigned create.
- **Every API write in skill examples goes through `/app/skills/paperclip/scripts/paperclip-api.sh`** — never hand-rolled curl or inline JSON. Use the existing `raw <METHOD> </api/...path>` subcommand for endpoints without a first-class command. A model-emitted malformed body once silently failed a write and stalled a prod run.
- **Helper paths in skill prose are absolute**, `/app/skills/paperclip/scripts/...`. The agent's cwd is its workspace, not the skill root.
- **Delivery lane:** skill files ship in the extended OpenClaw image. Merging to `main` ships nothing. The lane is `build-openclaw-image.yml` → flip `dev.tag` in `openclaw-version.json` → validate on dev → flip `prod.tag` → container reroll **through the admin UI**, never direct ECS.
- Infra tests run from `apps/infra` with `npx vitest run <path>`. `vitest.config.ts` sets `globals: true`, so `describe`/`it`/`expect` are ambient — do not import them.
- Never create probe/test issues on the owner's live board. `paperclip-api.sh` supports `--dry-run`.

---

## Plan-time correction to the spec

**Spec Part 1b (have the Dockerfile pull `skills/paperclip/` from the pinned Paperclip image at build time) is wrong and is replaced below.**

Evidence found while planning: `apps/infra/openclaw/Dockerfile:719-737` and the `SKILL.md` header both document that our copy carries **six deliberate Lightbulb customizations**, each traced to a production incident, and the Dockerfile explicitly instructs "when re-vendoring from upstream, re-apply that helper mandate rather than letting it revert." A build-time pull would silently revert all six on every image build — including the write-path helper mandate that exists because a malformed body stalled a prod run forever.

Our skill tree also has three files the fork does not (`references/attention-decisions-search.md`, `scripts/paperclip-api.sh`, `scripts/paperclip-issue-update.sh`), and `paperclip-api.sh` is what every example depends on.

**Replacement for Part 1b:** the re-vendor protocol already exists in the `ISOL8-LOCAL EDITS` header; it simply was not run since the June vendor. Task 1 records the exact fork commit vendored from, so the next re-vendor is a three-way diff instead of archaeology. No Dockerfile change, no CI guard (owner declined one), no risk to the six edits.

---

## File structure

- Modify `apps/infra/openclaw/skills/paperclip/SKILL.md` — add vendor provenance; merge 9 sections in from the fork; add the liveness rules. **Note: the skill is split (SKILL.md is a routing index + `references/*`), so mechanics land in `references/monitors.md` and `references/heartbeat.md`, not in SKILL.md.** External objects were dropped — see the spec's §1b corrections.
- Create `apps/infra/openclaw/skills/paperclip/references/cases.md` — vendored from the fork; the only reference file we are missing.
- Modify `apps/infra/test/paperclip-skill-primitives.test.ts` — assert the six local edits survive and the new sections exist with correct helper usage.
- Modify `openclaw-version.json` — `dev.tag` then `prod.tag`.
- Create `apps/infra/scripts/board-audit-worksheet.sh` — read-only worksheet generator for Part 3. Deleted at the end of Task 8 (CLAUDE.md: one-shot scripts do not survive their run).

---

### Task 1: Re-vendor the nine drifted sections, preserving the six local edits

The fork's `skills/paperclip/SKILL.md` has nine sections ours lacks. Ours has six sections the fork lacks, which are better and must not be lost. This is a union, not a replacement.

**Files:**
- Modify: `apps/infra/openclaw/skills/paperclip/SKILL.md`
- Create: `apps/infra/openclaw/skills/paperclip/references/cases.md`
- Test: `apps/infra/test/paperclip-skill-primitives.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: a `SKILL.md` containing all fifteen sections and a header line `Vendored from TheLightbulbCompany/paperclip skills/paperclip at <commit>` that Task 2 and future re-vendors read.

- [ ] **Step 1: Write the failing test for the six local edits and the vendor stamp**

Append to `apps/infra/test/paperclip-skill-primitives.test.ts`:

```ts
import { existsSync } from "node:fs";

const skillMd = readFileSync(resolve(skillRoot, "SKILL.md"), "utf8");

describe("vendor merge preserves Lightbulb-local edits", () => {
  // Each string is the load-bearing phrase of one ISOL8-LOCAL edit. If a
  // re-vendor drops one, the incident it prevents comes back.
  it.each([
    ["write-path helper mandate", "## Writing to the Paperclip API (required)"],
    ["run-id truthfulness", "## Webchat and interactive sessions (no run id)"],
    ["create status", "### Creating work for someone else — always say the status"],
    ["monitor section", "## Waiting on an External Thing (Issue Monitors)"],
    ["monitor vs approval", "### A monitor is the wait. An approval is the authority."],
    ["company memory", "## Company Memory, Decisions, and Attention"],
  ])("keeps the %s section", (_label, heading) => {
    expect(skillMd).toContain(heading);
  });

  it("never writes a relative scripts/ path in a command", () => {
    // Local edit 5: the agent's cwd is its workspace, not the skill root.
    const relative = skillMd.match(/(?<![/\w])scripts\/paperclip-[a-z-]+\.sh/g) ?? [];
    expect(relative).toEqual([]);
  });

  it("records the fork commit it was vendored from", () => {
    expect(skillMd).toMatch(
      /Vendored from TheLightbulbCompany\/paperclip skills\/paperclip at [0-9a-f]{7,40}/,
    );
  });
});

describe("sections vendored in from the fork", () => {
  it.each([
    "### Monitors and Watchers",
    "### Delegating review tasks",
    "## Managing A User's Inbox",
    "### Standalone Decisions",
    "## MCP Tool Approval Gates",
    "## Cases",
    "## Proposing Credentials Safely",
    "## Reading Granted Secrets",
  ])("has %s", (heading) => {
    expect(skillMd).toContain(heading);
  });

  it("ships the cases reference", () => {
    expect(existsSync(resolve(skillRoot, "references/cases.md"))).toBe(true);
  });
});
```

- [ ] **Step 2: Run it — expect FAIL**

```bash
cd apps/infra && npx vitest run test/paperclip-skill-primitives.test.ts
```

Expected: the six "keeps the … section" cases PASS (they are already there), and every "sections vendored in from the fork" case FAILS, plus "records the fork commit" FAILS.

- [ ] **Step 3: Capture the fork commit to vendor from**

The pinned server is `987520f9` (`paperclip-version.json` → `upstreamCommit`). Vendor the skill from that same commit so the skill and the server agree.

```bash
cd ~/dev/paperclip-fork && git show 987520f9:skills/paperclip/SKILL.md > /tmp/fork-skill.md
git show 987520f9:skills/paperclip/references/cases.md > /tmp/fork-cases.md
wc -l /tmp/fork-skill.md /tmp/fork-cases.md
```

If either path does not exist at `987520f9`, stop and report — do not vendor from the working tree, which is ahead of what we deploy.

- [ ] **Step 4: Add the vendor stamp to the header**

In `apps/infra/openclaw/skills/paperclip/SKILL.md`, inside the existing `<!-- ISOL8-LOCAL EDITS ... -->` comment, immediately after the line `ISOL8-LOCAL EDITS (deliberate; re-apply on re-vendor).`, insert:

```
  Vendored from TheLightbulbCompany/paperclip skills/paperclip at 987520f9
  (the same commit as paperclip-version.json upstreamCommit — keep them equal).
  To re-vendor: diff that path at the new commit against this file, take
  upstream's changes, and re-apply edits 1-6 below.
```

- [ ] **Step 5: Merge the nine sections in**

Copy each of these sections from `/tmp/fork-skill.md` into `SKILL.md`, placing each next to the section it is topically closest to:

`### Monitors and Watchers`, `### Delegating review tasks`, `## Managing A User's Inbox`, `### Standalone Decisions`, `## MCP Tool Approval Gates`, `## Cases`, `## Proposing Credentials Safely`, `## Reading Granted Secrets`, `## Searching Issues`.

Two merge rules, both non-negotiable:

1. **`### Monitors and Watchers` is a summary of what our `## Waiting on an External Thing (Issue Monitors)` already covers in more depth.** Do NOT replace ours. Merge only the sentence upstream has that we lack — the explicit statement that a run is an ephemeral execution window and nothing keeps watching after it exits — into our section, and drop the rest of upstream's version.
2. **`## Searching Issues` (fork) supersedes our `## Searching Issues (legacy narrow search)`.** Replace ours with the fork's, then re-apply local edit 1: rewrite every write example in it to call `/app/skills/paperclip/scripts/paperclip-api.sh`.

For every section merged, rewrite any hand-rolled `curl` or inline-JSON write to use the helper, and make every `scripts/` path absolute.

- [ ] **Step 6: Vendor the cases reference**

```bash
cp /tmp/fork-cases.md apps/infra/openclaw/skills/paperclip/references/cases.md
```

Then read it and rewrite any write example to use `/app/skills/paperclip/scripts/paperclip-api.sh`.

- [ ] **Step 7: Run the tests — expect PASS**

```bash
cd apps/infra && npx vitest run test/paperclip-skill-primitives.test.ts
```

Expected: all cases PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/infra/openclaw/skills/paperclip/SKILL.md \
        apps/infra/openclaw/skills/paperclip/references/cases.md \
        apps/infra/test/paperclip-skill-primitives.test.ts
git commit -m "fix(skill): re-vendor paperclip skill from the pinned fork commit

The bundled skill was vendored at the June line while the deployed server
is 987520f9 (August). Nine sections were missing. Stamps the vendor commit
so the next re-vendor is a diff, and asserts the six ISOL8-LOCAL edits
survive."
```

---

### Task 2: Write the three sections that exist in neither skill

This is the behavioural fix. `external_objects` and `issue_watchdogs` have **zero rows fleet-wide**; `unblockDescriptor` is set on 3 of our 13 blocked issues. All three endpoints are live.

**Files:**
- Modify: `apps/infra/openclaw/skills/paperclip/SKILL.md`
- Test: `apps/infra/test/paperclip-skill-primitives.test.ts`

**Interfaces:**
- Consumes: Task 1's merged `SKILL.md` and the existing `paperclip-api.sh raw <METHOD> </api/...path>` subcommand (`cmd_raw`, jq-validates a stdin body, requires the path to start with `/api/`).
- Produces: three sections whose headings Task 3's smoke check greps for.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/paperclip-skill-primitives.test.ts`:

```ts
describe("liveness primitives are documented", () => {
  it.each([
    "## Surviving a Dropped Run (Task Watchdogs)",
    "## Tracking a GitHub PR or Issue (External Objects)",
    "## Parking Work Correctly (unblockDescriptor)",
  ])("has %s", (heading) => {
    expect(skillMd).toContain(heading);
  });

  it("states the arm-a-watchdog-with-every-monitor rule", () => {
    expect(skillMd).toContain("arm a watchdog whenever you arm a monitor");
  });

  it("forbids asserting external state from memory", () => {
    expect(skillMd).toContain(
      "never from memory and never from an earlier run",
    );
  });

  it("drives the new endpoints through the helper, not curl", () => {
    for (const path of [
      "/api/issues/$PAPERCLIP_TASK_ID/watchdog",
      "/api/issues/$PAPERCLIP_TASK_ID/external-objects",
      "/api/issues/$PAPERCLIP_TASK_ID/external-objects/refresh",
    ]) {
      expect(skillMd).toContain(path);
    }
    // Every occurrence of those routes must be an argument to the helper.
    for (const line of skillMd.split("\n")) {
      if (line.includes("/external-objects") || line.includes("/watchdog")) {
        if (line.trimStart().startsWith("curl")) {
          throw new Error(`hand-rolled curl for a new endpoint: ${line}`);
        }
      }
    }
  });
});
```

- [ ] **Step 2: Run it — expect FAIL**

```bash
cd apps/infra && npx vitest run test/paperclip-skill-primitives.test.ts
```

Expected: every case in the new `describe` FAILS with the heading not found.

- [ ] **Step 3: Add the watchdog section**

Insert into `SKILL.md` immediately after `### A monitor is the wait. An approval is the authority.`:

````markdown
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
printf '%s' '{"agentId":"'"$PAPERCLIP_AGENT_ID"'","instructions":"Resume the ISOA-168 release check if this stalls with no live monitor."}' \
  | /app/skills/paperclip/scripts/paperclip-api.sh raw POST \
      "/api/issues/$PAPERCLIP_TASK_ID/watchdog"
```

- `agentId` is who gets woken when the subtree goes quiet — normally yourself.
- `instructions` is read by the agent that wakes. Say what to re-check and how
  to resume, not "investigate".
- Arm it once. It is durable and is not consumed by firing, unlike a monitor.

**Do not** arm a watchdog instead of setting a status. It reports that work
stopped; it does not make a parked issue legal.
````

- [ ] **Step 4: Add the external-objects section**

Insert immediately after the watchdog section:

````markdown
## Tracking a GitHub PR or Issue (External Objects)

When an issue waits on a GitHub pull request or issue, **do not describe it in
prose**. Prose is not readable by anything: a description that says "waiting for
PR 1934 to merge" leaves nothing in the system capable of noticing when it does.
Register it as an **external object** instead, and refresh that object whenever
you need its state.

```bash
# Register the PR this issue waits on.
printf '%s' '{"provider":"github","kind":"pull_request","url":"https://github.com/TheLightbulbCompany/lightbulb/pull/1934"}' \
  | /app/skills/paperclip/scripts/paperclip-api.sh raw POST \
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
````

- [ ] **Step 5: Add the unblockDescriptor section**

Insert immediately after the external-objects section:

````markdown
## Parking Work Correctly (unblockDescriptor)

Moving an issue to `blocked` without saying what unblocks it makes it invisible
work. Paperclip acts on an `unblockDescriptor` — it wakes the named agent when
the block is theirs to clear — and does nothing at all without one.

```bash
# Blocked on another agent: name them and the action. They get woken.
/app/skills/paperclip/scripts/paperclip-api.sh raw PATCH \
  "/api/issues/$PAPERCLIP_TASK_ID" <<'JSON'
{"status":"blocked",
 "unblockDescriptor":{"owner":{"agentId":"<agent uuid>"},
                      "action":"Restore the shared Stripe credentials, then rerun this issue."}}
JSON
```

For a human decision, the owner is the board. Paperclip deliberately wakes
nobody in this case — the item surfaces in the owner's queue and waits there
with no timer and no auto-close:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh raw PATCH \
  "/api/issues/$PAPERCLIP_TASK_ID" <<'JSON'
{"status":"blocked",
 "unblockDescriptor":{"owner":"board",
                      "action":"Decide whether Tally owns finance or Friday keeps it."}}
JSON
```

**Pick the owner honestly.** A human decision routed to an agent produces a loop
where nobody can act. A machine-checkable condition routed to the board spends
the owner's attention on something you could have polled — use a monitor and an
external object for that instead.

**If you cannot name an owner and an action, the issue is not blocked — it is
abandoned.** Close it, or reduce it to the part you can actually do. Nine issues
on this board were filed in one sitting with nothing to wait on and sat for a
month. Filing work nobody is waiting on is worse than filing nothing.
````

- [ ] **Step 6: Run the tests — expect PASS**

```bash
cd apps/infra && npx vitest run test/paperclip-skill-primitives.test.ts
```

Expected: all cases PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/infra/openclaw/skills/paperclip/SKILL.md \
        apps/infra/test/paperclip-skill-primitives.test.ts
git commit -m "feat(skill): document watchdogs, external objects and unblockDescriptor

All three ship in the deployed Paperclip server and have never been used:
external_objects and issue_watchdogs have zero rows fleet-wide. Adds the
arm-a-watchdog-with-every-monitor rule and forbids asserting external state
from memory."
```

---

### Task 3: Ship the skill to the fleet

Merging changes nothing. The skill only reaches agents through a new image and a container reroll.

**Files:**
- Modify: `openclaw-version.json` (`dev.tag`, then `prod.tag` in a second commit)

**Interfaces:**
- Consumes: Tasks 1 and 2 merged to `main`.
- Produces: agents running an image whose `/app/skills/paperclip/SKILL.md` contains the three new sections.

- [ ] **Step 1: Watch the image build**

The build triggers on changes under `apps/infra/openclaw/`.

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow build-openclaw-image.yml --limit 3
gh run watch <run-id> --repo TheLightbulbCompany/lightbulb --exit-status
```

- [ ] **Step 2: Flip the dev tag**

Set `dev.tag` in `openclaw-version.json` to the tag the build published, then commit and open a PR. `prod.tag` stays unchanged in this commit.

- [ ] **Step 3: Reroll one dev container through the admin UI**

Use `admin.dev.flickmeon.ai`. Never `aws ecs update-service` directly.

- [ ] **Step 4: Verify the skill actually landed in the running container**

Open a chat with Friday on dev and ask it to print the headings of its Paperclip skill. Expected: the three new headings appear. A green build is not evidence the container is running the new image.

- [ ] **Step 5: Verify the primitive is reachable, on dev only**

Ask Friday on dev to register an external object on a dev issue and arm a watchdog. Then confirm the rows exist:

```sql
SELECT count(*) FROM external_objects;
SELECT count(*) FROM issue_watchdogs;
```

against the **dev** cluster `lightbulb-dev-paperclip-db`. Expected: both non-zero for the first time. If they are still zero, the skill shipped but the agent is not acting on it — stop and report before promoting.

- [ ] **Step 6: Flip the prod tag and reroll**

Only after Step 5 passes. Separate PR moving `prod.tag`, then reroll prod containers through `admin.flickmeon.ai`.

---

### Task 4: Build the board audit worksheet

Part 3 audits every open issue individually. Build the worksheet first so the audit is evidence-driven rather than title-driven.

**Files:**
- Create: `apps/infra/scripts/board-audit-worksheet.sh`

**Interfaces:**
- Produces: `/tmp/board-audit.md`, one section per open issue, consumed by Tasks 5-7.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Read-only worksheet for the founder-board audit (one-shot; delete after Task 7).
set -euo pipefail
CO=1071914e-6b9a-4351-a1d6-58bffe2e30f1
CLUSTER=arn:aws:rds:us-east-1:877352799272:cluster:lightbulb-prod-paperclip-db
SECRET=arn:aws:secretsmanager:us-east-1:877352799272:secret:lightbulb-prod-paperclip-db-credentials

aws rds-data execute-statement --profile isol8-admin --region us-east-1 \
  --resource-arn "$CLUSTER" --secret-arn "$SECRET" --database paperclip \
  --format-records-as JSON --query formattedRecords --output text \
  --sql "SELECT i.identifier AS id, i.status AS st, coalesce(a.name,'-') AS agent,
                (now()::date - i.updated_at::date) AS age, i.origin_kind AS origin,
                (i.unblock_descriptor IS NOT NULL) AS has_unblock,
                (i.monitor_next_check_at IS NOT NULL) AS has_monitor,
                i.title AS title, coalesce(left(i.description,900),'') AS descr
         FROM issues i LEFT JOIN agents a ON a.id=i.assignee_agent_id
         WHERE i.company_id='$CO' AND i.status NOT IN ('done','cancelled')
         ORDER BY i.status, i.updated_at" \
| python3 -c '
import json, sys
rows = json.load(sys.stdin)
print("# Founder board audit\n")
print(f"{len(rows)} open issues. One section each. Answer all four questions before any write.\n")
for r in rows:
    print(f"## {r[\"id\"]} — {r[\"title\"]}")
    print(f"- status `{r[\"st\"]}` · assignee {r[\"agent\"]} · {r[\"age\"]}d stale · origin `{r[\"origin\"]}`")
    print(f"- has unblockDescriptor: {r[\"has_unblock\"]} · has monitor: {r[\"has_monitor\"]}")
    print(f"- description: {\" \".join((r[\"descr\"] or \"(none)\").split())[:700]}")
    print("""
- [ ] Q1 Already done in the world? (check the thing, not the board)
- [ ] Q2 Still useful for what we are doing in September?
- [ ] Q3 Is its premise still true?
- [ ] Q4 Disposition: close / repair-machine / repair-human / leave
- [ ] Evidence:
""")
'
```

- [ ] **Step 2: Run it and check the shape**

```bash
chmod +x apps/infra/scripts/board-audit-worksheet.sh
apps/infra/scripts/board-audit-worksheet.sh > /tmp/board-audit.md
grep -c '^## ISOA-' /tmp/board-audit.md
```

Expected: 50 (or the current open count). If the count is 0, the SQL or the profile is wrong — fix before continuing.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/scripts/board-audit-worksheet.sh
git commit -m "chore(scripts): read-only worksheet for the founder board audit"
```

---

### Task 5: Audit the machine-gated issues and repair them

**Files:** none in the repo. This task writes to the prod board through the API.

**Interfaces:**
- Consumes: `/tmp/board-audit.md` from Task 5; the skill sections from Task 2 as the reference for what a correct repair looks like.
- Produces: worksheet sections with Q1-Q4 answered and evidence recorded.

- [ ] **Step 1: Answer Q1-Q3 for each candidate before touching anything**

Candidates to start from — **each must be re-verified, not trusted from this plan**:
ISOA-168, ISOA-166, ISOA-127, ISOA-113, ISOA-114, ISOA-190, ISOA-75, ISOA-134, ISOA-63, ISOA-44, ISOA-65, ISOA-28.

Evidence commands:

```bash
gh pr view 1934 --repo TheLightbulbCompany/lightbulb --json state,mergedAt
gh issue view 1795 --repo TheLightbulbCompany/lightbulb --json state,title
dig +short www.isol8.co
aws cloudwatch describe-alarms --profile isol8-admin --region us-east-1 \
  --alarm-name-prefix lightbulb-prod-W31 --query 'MetricAlarms[].AlarmName'
```

Known at plan time, to be re-checked: PR 1934 merged 2026-08-23, so ISOA-168's stated condition is already satisfied and its two dependents are unblocked. GitHub issue 1795 is still open, so ISOA-113/114 are live and block Meta App Review. ISOA-190's premise is inverted — `isol8.co` is retired, so we do not want a `www` record; the real work is fixing Meta's dead `isol8.co` URLs. ISOA-134 names `isol8-prod-W31-*` alarms that were renamed in the CloudWatch migration.

Record the answer and the evidence in the worksheet for every one. Do not batch this.

- [ ] **Step 2: Repair each issue that survives Q1-Q3**

For each, in this order — the endpoints and bodies are exactly those documented in Task 2:

1. Register the external object it waits on (`POST /api/issues/{id}/external-objects`).
2. Set the status to `in_progress` or `in_review` and confirm an agent assignee — a monitor is rejected otherwise.
3. Schedule the monitor: `/app/skills/paperclip/scripts/paperclip-api.sh monitor <issueId> --at <ISO> --service <name> --notes "<identifier>" --timeout-at <ISO> --max-attempts 8 --recovery escalate_to_board`.
4. Arm the watchdog (`POST /api/issues/{id}/watchdog`).

For ISOA-168 specifically, the condition is already met, so do not schedule a monitor — close it with the merge evidence and unblock ISOA-166 and ISOA-127 instead.

- [ ] **Step 3: Verify the repairs landed**

```sql
SELECT identifier, monitor_next_check_at IS NOT NULL AS mon
FROM issues WHERE company_id='1071914e-6b9a-4351-a1d6-58bffe2e30f1'
  AND status NOT IN ('done','cancelled');
SELECT count(*) FROM issue_watchdogs
 WHERE company_id='1071914e-6b9a-4351-a1d6-58bffe2e30f1';
SELECT count(*) FROM external_objects
 WHERE company_id='1071914e-6b9a-4351-a1d6-58bffe2e30f1';
```

Expected: watchdogs and external objects both non-zero on prod for the first time; every repaired issue shows a monitor.

---

### Task 6: Audit the human-gated issues and queue them

> **CORRECTED 2026-09-08 — read before executing.** An **agent** cannot set
> `unblockDescriptor` with `owner: "board"` or a `userId`: `issues.ts:8884` throws
> `403 "Agents may only name themselves as an unblock owner"`. This task works only
> because it runs as a **user actor** (owner credentials via the Lightbulb UI or an
> owner-authed API call), never from a heartbeat. Also note the descriptor does NOT
> re-check anything — `routable-blocked.ts` fires one notification at the blocked
> transition and never watches for the block clearing. Queueing here buys legibility
> and an owner-visible list, not automatic resumption. Anything machine-observable
> belongs in Task 5 with a monitor + watchdog instead.


**Files:** none in the repo.

**Interfaces:**
- Consumes: `/tmp/board-audit.md`.
- Produces: every human-gated issue carrying `unblockDescriptor` with `owner: "board"`.

- [ ] **Step 1: Answer Q1-Q3 for each candidate**

Start from ISOA-186, ISOA-121, ISOA-117, ISOA-120, ISOA-89, ISOA-207, ISOA-215, ISOA-208, ISOA-119, ISOA-12. Re-verify each.

**ISOA-186 first.** It names six real customers (Yousef/Huddle, Ben/Lull, David/MyTrainer, Max/MX13, Santi/Airgun), is `todo`, unassigned, and sixteen days stale. Check each against the live `companies` table before deciding anything — some may already be onboarded.

- [ ] **Step 2: Set the descriptor on each survivor**

Use the `owner: "board"` form from Task 2's section. The `action` must be the decision you need from the owner, in one sentence, not "follow up".

- [ ] **Step 3: Verify**

```sql
SELECT identifier, unblock_descriptor->'owner' AS owner
FROM issues WHERE company_id='1071914e-6b9a-4351-a1d6-58bffe2e30f1'
  AND status='blocked';
```

Expected: no blocked issue without a descriptor.

---

### Task 7: Close what the audit rejected, and clean up

**Files:**
- Delete: `apps/infra/scripts/board-audit-worksheet.sh`

**Interfaces:**
- Consumes: the completed worksheet.
- Produces: a board where every open issue has a recorded disposition.

- [ ] **Step 1: Close each issue the audit rejected, with its reason**

Candidates from the audit, each needing its own recorded reason: the nine ISOA-68 children (ISOA-30, 33, 34, 37, 79, 86, 87, 88, 89), plus ISOA-102, ISOA-116, ISOA-126, ISOA-180, ISOA-181, ISOA-188.

Use `cancelled`, not `done` — these were not completed. Post the reason as a comment before the status change so the record survives.

**Leave ISOA-148 and ISOA-150 alone.** Their own text says to keep them parked between demo takes; Task 4 removes the routine rows from view, and these two should be moved to a demo project rather than closed.

- [ ] **Step 2: Verify no issue is left undisposed**

```sql
SELECT identifier, status,
       (unblock_descriptor IS NOT NULL) AS unblock,
       (monitor_next_check_at IS NOT NULL) AS monitor
FROM issues
WHERE company_id='1071914e-6b9a-4351-a1d6-58bffe2e30f1'
  AND status NOT IN ('done','cancelled')
ORDER BY status;
```

Expected: every remaining open row has either a monitor or an `unblockDescriptor`. Any row with neither was missed — go back to Task 6 or 7 for it.

- [ ] **Step 3: Delete the worksheet script and commit**

CLAUDE.md: one-shot scripts do not survive their run; git history is the record.

```bash
git rm apps/infra/scripts/board-audit-worksheet.sh
git commit -m "chore(scripts): drop the board audit worksheet after its run"
```

- [ ] **Step 4: Delete the spec and this plan**

CLAUDE.md: planning docs are disposable and are deleted when the work ships. The PR descriptions are the durable record.

```bash
git rm docs/superpowers/specs/2026-09-08-board-liveness-and-claim-accuracy-design.md \
       docs/superpowers/plans/2026-09-08-board-liveness-and-claim-accuracy.md
git commit -m "docs: remove shipped board-liveness spec and plan"
```
