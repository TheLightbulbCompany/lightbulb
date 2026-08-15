import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const skillRoot = resolve(__dirname, "../openclaw/skills/paperclip");
const helper = resolve(skillRoot, "scripts/paperclip-api.sh");

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
      "extract", "company-1", "--contains", "github.com/Isol8AI/isol8/pull",
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
        "?contains=github.com%2FIsol8AI%2Fisol8%2Fpull&kind=url&scope=comments&matchesPerIssue=50",
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
});
