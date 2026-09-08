import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

const skillRoot = resolve(__dirname, "../openclaw/skills/paperclip");
const helper = resolve(skillRoot, "scripts/paperclip-api.sh");
const skillMd = readFileSync(resolve(skillRoot, "SKILL.md"), "utf8");
const monitorsMd = readFileSync(resolve(skillRoot, "references/monitors.md"), "utf8");
const heartbeatRefMd = readFileSync(resolve(skillRoot, "references/heartbeat.md"), "utf8");

function runHelper(args: string[], input = "") {
  return spawnSync("bash", [helper, ...args], {
    encoding: "utf8",
    input,
    env: {
      ...process.env,
      PAPERCLIP_API_URL: "https://paperclip.test",
      PAPERCLIP_API_KEY: "test-key",
    },
  });
}

/** Run the helper against a stubbed `curl` so read-modify-write subcommands can
 * be exercised for real. The stub answers a GET with `issue` and echoes back a
 * write's request body, so the assertion sees the exact bytes that would hit
 * Paperclip. `--dry-run` cannot cover this: it deliberately skips the read, and
 * the read IS the behaviour under test. */
function runHelperWithIssue(args: string[], issue: unknown) {
  const stubDir = mkdtempSync(join(tmpdir(), "paperclip-curl-"));
  const stub = join(stubDir, "curl");
  writeFileSync(
    stub,
    [
      "#!/usr/bin/env bash",
      'method=GET; out=""; data=""',
      'while [[ $# -gt 0 ]]; do',
      '  case "$1" in',
      '    -X) method="$2"; shift 2 ;;',
      '    -o) out="$2"; shift 2 ;;',
      '    --data-binary) data="$2"; shift 2 ;;',
      "    *) shift ;;",
      "  esac",
      "done",
      'if [[ "$method" == "GET" ]]; then',
      '  printf \'%s\' "$MOCK_ISSUE_JSON" > "$out"',
      "else",
      '  printf \'%s\' "$data" > "$out"',
      "fi",
      "printf '200'",
      "",
    ].join("\n"),
  );
  chmodSync(stub, 0o755);

  return spawnSync("bash", [helper, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${stubDir}:${process.env.PATH}`,
      PAPERCLIP_API_URL: "https://paperclip.test",
      PAPERCLIP_API_KEY: "test-key",
      MOCK_ISSUE_JSON: JSON.stringify(issue),
    },
  });
}

/** An issue already carrying a reviewer stage — the thing a naive
 * `raw PATCH {executionPolicy:{monitor}}` would silently destroy. */
const ISSUE_WITH_STAGES = {
  id: "issue-1",
  executionPolicy: {
    mode: "normal",
    commentRequired: true,
    stages: [
      {
        id: "stage-review",
        type: "review",
        approvalsNeeded: 1,
        participants: [{ id: "p-1", type: "agent", agentId: "reviewer-1" }],
      },
    ],
  },
};

describe("bundled Paperclip operating primitives", () => {
  it("builds run-scoped decisions with a stable ruleKey and idempotency key", () => {
    const result = runHelper(
      [
        "decision",
        "company-1",
        "--title",
        "Authorize refund?",
        "--rule-key",
        "support.refund",
        "--idempotency-key",
        "support-refund:issue-1:v1",
        "--dry-run",
      ],
      JSON.stringify({
        body: "Stripe evidence is linked on the issue.",
        options: [
          { id: "approve", label: "Approve", effects: [] },
          { id: "decline", label: "Decline", effects: [] },
        ],
      }),
    );

    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({
      body: "Stripe evidence is linked on the issue.",
      options: [
        { id: "approve", label: "Approve", effects: [] },
        { id: "decline", label: "Decline", effects: [] },
      ],
      title: "Authorize refund?",
      ruleKey: "support.refund",
      idempotencyKey: "support-refund:issue-1:v1",
      continuationPolicy: "wake_origin_agent",
    });
  });

  it("rejects dynamic-looking or missing decision rule keys", () => {
    const spec = JSON.stringify({ body: "Evidence", options: [{ id: "ok", label: "OK", effects: [] }] });
    const missing = runHelper([
      "decision", "company-1", "--title", "Act?", "--idempotency-key", "attempt-1", "--dry-run",
    ], spec);
    const malformed = runHelper([
      "decision", "company-1", "--title", "Act?", "--rule-key", "2026-08-15",
      "--idempotency-key", "attempt-1", "--dry-run",
    ], spec);

    expect(missing.status).not.toBe(0);
    expect(missing.stderr).toContain("--rule-key is required");
    expect(malformed.status).not.toBe(0);
    expect(malformed.stderr).toContain("stable lowercase dotted/dashed key");
  });

  it("encodes company search and extraction queries without inline URL construction", () => {
    const search = runHelper([
      "search", "company-1", "--query", "provider wall & checkout", "--scope", "documents",
      "--status", "todo,blocked", "--updated-within", "30d", "--limit", "25", "--dry-run",
    ]);
    const extract = runHelper([
      "extract", "company-1", "--contains", "github.com/TheLightbulbCompany/lightbulb/pull",
      "--kind", "url", "--scope", "comments", "--matches-per-issue", "50", "--dry-run",
    ]);
    const stats = runHelper([
      "decision-stats", "company-1", "--since", "2026-08-01T00:00:00-04:00", "--dry-run",
    ]);
    const outcome = runHelper(["decision-get", "decision-1", "--dry-run"]);

    expect(search.status, search.stderr).toBe(0);
    expect(search.stdout.trim()).toBe(
      "GET https://paperclip.test/api/companies/company-1/search" +
        "?q=provider%20wall%20%26%20checkout&scope=documents&status=todo%2Cblocked&updatedWithin=30d&limit=25",
    );
    expect(extract.status, extract.stderr).toBe(0);
    expect(extract.stdout.trim()).toBe(
      "GET https://paperclip.test/api/companies/company-1/search/extract" +
        "?contains=github.com%2FTheLightbulbCompany%2Flightbulb%2Fpull&kind=url&scope=comments&matchesPerIssue=50",
    );
    expect(stats.status, stats.stderr).toBe(0);
    expect(stats.stdout.trim()).toBe(
      "GET https://paperclip.test/api/companies/company-1/decisions/stats" +
        "?groupBy=ruleKey&since=2026-08-01T00%3A00%3A00-04%3A00",
    );
    expect(outcome.status, outcome.stderr).toBe(0);
    expect(outcome.stdout.trim()).toBe("GET https://paperclip.test/api/decisions/decision-1");
  });

  it("builds queue and triage mutations with typed JSON values", () => {
    const queue = runHelper([
      "queue-create", "company-1", "--key", "customer-authority", "--title", "Customer authority",
      "--retention-days", "30", "--dry-run",
    ]);
    const triage = runHelper([
      "triage", "company-1", "--source-kind", "decision", "--source-id", "decision-1",
      "--clear-decide-by", "--snoozed-until", "2026-08-16T09:00:00-04:00", "--dry-run",
    ]);
    const queueItem = runHelper([
      "queue-add", "company-1", "--key", "customer-authority", "--source-kind", "decision",
      "--source-id", "decision-1", "--dry-run",
    ]);

    expect(queue.status, queue.stderr).toBe(0);
    expect(JSON.parse(queue.stdout)).toEqual({
      key: "customer-authority",
      title: "Customer authority",
      retentionDays: 30,
    });
    expect(triage.status, triage.stderr).toBe(0);
    expect(JSON.parse(triage.stdout)).toEqual({
      decideBy: null,
      snoozedUntil: "2026-08-16T09:00:00-04:00",
    });
    expect(queueItem.status, queueItem.stderr).toBe(0);
    expect(JSON.parse(queueItem.stdout)).toEqual({ sourceKind: "decision", sourceId: "decision-1" });
  });

  it("documents the board-only attention boundary and agent-scoped stats", () => {
    const skill = readFileSync(resolve(skillRoot, "SKILL.md"), "utf8");
    const reference = readFileSync(resolve(skillRoot, "references/attention-decisions-search.md"), "utf8");

    expect(skill).toContain("The unified attention feed is **board-only**");
    expect(reference).toContain("Agents are always server-scoped to the caller's own decisions");
    expect(reference).toContain("Approval does **not** prove application");
    expect(reference).toMatch(/`executed`, `skipped`,\s+or `failed`/);
  });

  it("merges a scheduled monitor onto the issue's existing execution policy", () => {
    const result = runHelperWithIssue(
      [
        "monitor",
        "issue-1",
        "--at",
        "2026-08-25T09:00:00Z",
        "--service",
        "GitHub",
        "--notes",
        "acme/api#1979 — resume once merged",
        "--recovery",
        "escalate_to_board",
        "--max-attempts",
        "8",
      ],
      ISSUE_WITH_STAGES,
    );

    expect(result.status, result.stderr).toBe(0);
    const sent = JSON.parse(result.stdout);
    // The reviewer stage and commentRequired survive the monitor write — this
    // is the whole reason the subcommand reads before it writes.
    expect(sent.executionPolicy.stages).toEqual(ISSUE_WITH_STAGES.executionPolicy.stages);
    expect(sent.executionPolicy.commentRequired).toBe(true);
    expect(sent.executionPolicy.monitor).toEqual({
      nextCheckAt: "2026-08-25T09:00:00Z",
      kind: "external_service",
      serviceName: "GitHub",
      notes: "acme/api#1979 — resume once merged",
      recoveryPolicy: "escalate_to_board",
      maxAttempts: 8,
    });
  });

  it("offers no --ref flag, since upstream redacts externalRef at write time", () => {
    // A flag whose value is silently replaced with "[redacted]" before storage
    // is exactly the quiet data loss this helper exists to prevent.
    const result = runHelper([
      "monitor", "issue-1", "--at", "2026-08-25T09:00:00Z", "--ref", "acme/api#1979", "--dry-run",
    ]);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("unknown argument: --ref");
  });

  it("clears only the monitor, leaving review gates in place", () => {
    const result = runHelperWithIssue(["monitor", "issue-1", "--clear"], ISSUE_WITH_STAGES);

    expect(result.status, result.stderr).toBe(0);
    const sent = JSON.parse(result.stdout);
    expect(sent.executionPolicy.monitor).toBeNull();
    expect(sent.executionPolicy.stages).toEqual(ISSUE_WITH_STAGES.executionPolicy.stages);
  });

  it("rejects a recovery policy outside Paperclip's enum", () => {
    const result = runHelper([
      "monitor", "issue-1", "--at", "2026-08-25T09:00:00Z", "--recovery", "page_the_ceo", "--dry-run",
    ]);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("wake_owner,create_recovery_issue,escalate_to_board");
  });

  it("teaches agents that a monitor is the wait and an approval is the authority", () => {
    const skill = readFileSync(resolve(skillRoot, "SKILL.md"), "utf8");

    expect(skill).toContain("A monitor is the wait. An approval is the authority.");
    expect(skill).toContain("escalate_to_board");
  });
});

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

// SKILL.md is a routing index (see MAINTENANCE.md "SHAPE"): it carries only
// the always-loaded rules, and the mechanics for each primitive live in the
// reference file the routing table points at. So this heading lives in
// references/monitors.md (watchdogs) and references/heartbeat.md
// (unblockDescriptor) — never in SKILL.md itself.
//
// external_objects has no assertions here: fix round 2 found POST
// /issues/:id/external-objects does not exist upstream (only GET, the
// summary route, and POST .../refresh are registered), and the whole
// feature is gated off (`experimental.enableExternalObjects` is `{}` in
// both envs) — documenting it would have manufactured the exact
// false-claim failure this change exists to prevent. The tool-agnostic
// accuracy rule survives in SKILL.md; the fabricated endpoint does not.
describe("liveness primitives are documented", () => {
  it("references/monitors.md has ## Surviving a Dropped Run (Task Watchdogs)", () => {
    expect(monitorsMd).toContain("## Surviving a Dropped Run (Task Watchdogs)");
  });

  it("references/heartbeat.md has ## Parking Work Correctly (unblockDescriptor)", () => {
    expect(heartbeatRefMd).toContain("## Parking Work Correctly (unblockDescriptor)");
  });

  it("states the arm-a-watchdog-with-every-monitor rule in SKILL.md", () => {
    expect(skillMd).toContain("arm a watchdog whenever you arm a monitor");
  });

  it("forbids asserting external state from memory (tool-agnostic, not tied to external objects)", () => {
    expect(skillMd).toContain("never from memory and never from an earlier run");
    // Fix round 2: the mechanism this rule pointed at (external objects) is a
    // disabled/nonexistent endpoint. The rule text itself must no longer
    // claim Paperclip tracks this state.
    expect(skillMd).not.toContain("external object");
  });

  it("forbids parking an issue in blocked with no named owner and action", () => {
    expect(skillMd).toContain(
      "Never park an issue in `blocked` without a named owner and a concrete unblock action.",
    );
  });

  it("arms the watchdog with PUT, not POST — a copied POST 404s", () => {
    // Fix round 2: only get/put/delete are registered for
    // /issues/:id/watchdog upstream. Assert the verb, not just the path —
    // a path-only check passed straight over this bug once already.
    expect(monitorsMd).toContain(
      '}\' | /app/skills/paperclip/scripts/paperclip-api.sh raw PUT \\\n' +
        '      "/api/issues/$PAPERCLIP_TASK_ID/watchdog"',
    );
    for (const line of monitorsMd.split("\n")) {
      if (line.includes("/watchdog") && line.trimStart().startsWith("curl")) {
        throw new Error(`hand-rolled curl for the watchdog endpoint: ${line}`);
      }
    }
  });

  it("has no trace of the disabled/nonexistent external-objects endpoints", () => {
    expect(monitorsMd).not.toContain("external-objects");
    expect(monitorsMd).not.toContain("External Objects");
  });

  it("states the unblockDescriptor+blocked-status precondition", () => {
    expect(heartbeatRefMd).toContain(
      "`unblockDescriptor` is only accepted together with\n`status: \"blocked\"`",
    );
    expect(heartbeatRefMd).toContain("422 unblockDescriptor requires");
  });

  it("drives the unblockDescriptor write through the helper, not curl", () => {
    expect(heartbeatRefMd).toContain("unblockDescriptor");
    for (const line of heartbeatRefMd.split("\n")) {
      if (line.includes("unblockDescriptor") && line.trimStart().startsWith("curl")) {
        throw new Error(`hand-rolled curl for unblockDescriptor: ${line}`);
      }
    }
  });

  it("flags Cases as disabled on this instance in the routing table", () => {
    // Fix round 2: enableCases is false in both envs — say so before an
    // agent burns a run on a 403.
    expect(skillMd).toContain("references/cases.md");
    expect(skillMd).toContain("**Disabled on this instance** (`enableCases` is false in both envs)");
  });
});
