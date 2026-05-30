// apps/frontend/src/__tests__/teams/subIssues.test.ts
import { describe, it, expect } from "vitest";
import { sortSubIssues, buildSubIssueProgressSummary } from "@/components/teams/shared/lib/subIssues";
import type { Issue } from "@/components/teams/shared/types";

function mk(id: string, status: Issue["status"], createdAt: string): Issue {
  return { id, title: id, status, createdAt } as Issue;
}

describe("sortSubIssues", () => {
  it("orders by createdAt ascending, then id", () => {
    const out = sortSubIssues([
      mk("b", "todo", "2026-05-02T00:00:00Z"),
      mk("a", "todo", "2026-05-01T00:00:00Z"),
      mk("c", "todo", "2026-05-01T00:00:00Z"),
    ]);
    expect(out.map((i) => i.id)).toEqual(["a", "c", "b"]);
  });
});

describe("buildSubIssueProgressSummary", () => {
  it("counts by bucket, ignores cancelled, and picks the first actionable as next", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "in_progress", "2026-05-02T00:00:00Z"),
      mk("c", "todo", "2026-05-03T00:00:00Z"),
      mk("d", "cancelled", "2026-05-04T00:00:00Z"),
    ]);
    expect(s.totalCount).toBe(3); // cancelled excluded
    expect(s.doneCount).toBe(1);
    expect(s.inProgressCount).toBe(1);
    expect(s.target?.kind).toBe("next");
    expect(s.target?.issue.id).toBe("b"); // earliest actionable by sort
  });

  it("returns a blocked target when nothing is actionable but blocked remain", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "blocked", "2026-05-02T00:00:00Z"),
    ]);
    expect(s.target?.kind).toBe("blocked");
    expect(s.target?.issue.id).toBe("b");
    expect(s.blockedCount).toBe(1);
  });

  it("treats won_t_do/duplicate/closed as terminal (no target)", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "won_t_do", "2026-05-02T00:00:00Z"),
    ]);
    expect(s.target).toBeNull();
  });
});
