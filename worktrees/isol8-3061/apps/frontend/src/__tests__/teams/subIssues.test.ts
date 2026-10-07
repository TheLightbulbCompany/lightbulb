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

describe("settledCount — finished is more than literally done", () => {
  const at = (status: string, id: string) =>
    ({ id, status, createdAt: "2026-09-08T10:00:00Z" }) as never;

  it("counts closed and won't-do as finished, so a goal can reach its own total", () => {
    const s = buildSubIssueProgressSummary([at("closed", "a"), at("won_t_do", "b")]);
    expect(s.totalCount).toBe(2);
    expect(s.settledCount).toBe(2);
    // The sub-issue strip's stricter meaning is deliberately untouched.
    expect(s.doneCount).toBe(0);
  });

  it("still drops cancelled from the denominator", () => {
    const s = buildSubIssueProgressSummary([at("done", "a"), at("cancelled", "b")]);
    expect(s.totalCount).toBe(1);
    expect(s.settledCount).toBe(1);
  });

  it("leaves open work outstanding", () => {
    const s = buildSubIssueProgressSummary([at("closed", "a"), at("todo", "b")]);
    expect(s.settledCount).toBe(1);
    expect(s.totalCount).toBe(2);
  });
});
