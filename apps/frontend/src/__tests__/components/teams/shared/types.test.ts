import { test, expect } from "vitest";
import type {
  Issue,
  Approval,
  HeartbeatRun,
  IssueComment,
  IssueCreateInput,
  IssueUpdateInput,
  DashboardRunActivityDay,
  DashboardSummary,
  DashboardResponse,
} from "@/components/teams/shared/types";
import { ISSUE_STATUSES } from "@/components/teams/shared/types";

test("types module exports the runtime status list", () => {
  expect(ISSUE_STATUSES).toContain("todo");
  expect(ISSUE_STATUSES.length).toBeGreaterThan(5);
});

test("Issue type allows minimum-shape construction", () => {
  const issue: Issue = { id: "i1", title: "x", status: "todo" };
  expect(issue.id).toBe("i1");
});

test("Approval and HeartbeatRun discriminator unions compile", () => {
  const a: Approval = { id: "a1", status: "pending" };
  const r: HeartbeatRun = { id: "r1", status: "queued" };
  expect(a.status).toBe("pending");
  expect(r.status).toBe("queued");
});

test("IssueComment type allows required + optional fields", () => {
  const c: IssueComment = { id: "c1", body: "Hello", createdAt: "2026-05-05T00:00:00Z" };
  expect(c.id).toBe("c1");
});

test("IssueCreateInput requires only title", () => {
  const input: IssueCreateInput = { title: "Fix bug" };
  expect(input.title).toBe("Fix bug");
});

test("IssueUpdateInput allows all-optional fields", () => {
  const input: IssueUpdateInput = {};
  expect(Object.keys(input).length).toBe(0);
});

test("DashboardRunActivityDay shape compiles", () => {
  const d: DashboardRunActivityDay = {
    date: "2026-05-06",
    succeeded: 4,
    failed: 1,
    other: 0,
    total: 5,
  };
  expect(d.date).toBe("2026-05-06");
  expect(d.succeeded).toBe(4);
});

test("DashboardSummary minimum-shape construction compiles", () => {
  const s: DashboardSummary = {
    agents: {},
    tasks: {},
    runActivity: [],
    costs: { monthSpendCents: 0, monthBudgetCents: 0, monthUtilizationPercent: 0 },
    pendingApprovals: 0,
    budgets: { pendingApprovals: 0 },
  };
  expect(s.runActivity).toEqual([]);
});

test("DashboardResponse wraps summary + optional sidebar_badges", () => {
  const r: DashboardResponse = {
    dashboard: {
      agents: { active: 1 },
      tasks: { open: 2 },
      runActivity: [{ date: "2026-05-06", succeeded: 1, failed: 0, other: 0, total: 1 }],
      costs: { monthSpendCents: 100, monthBudgetCents: 0, monthUtilizationPercent: 0 },
      pendingApprovals: 0,
      budgets: { pendingApprovals: 0 },
    },
    sidebar_badges: { inbox: 3 },
  };
  expect(r.dashboard.agents.active).toBe(1);
  expect(r.sidebar_badges?.inbox).toBe(3);
});
