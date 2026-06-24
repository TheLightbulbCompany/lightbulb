import { describe, it, expect } from "vitest";
import { patchExecutionPolicy } from "../executionPolicy";
import type { IssueExecutionPolicy } from "@/components/teams/shared/types";

// The rail's monitor editor only carries nextCheckAt / notes / serviceName.
// patchExecutionPolicy must MERGE that partial edit onto the existing monitor
// so upstream-managed fields survive a reschedule — and must still preserve
// reviewer/approver stages, and vice-versa.
describe("patchExecutionPolicy", () => {
  const withMonitor: IssueExecutionPolicy = {
    mode: "normal",
    stages: [],
    monitor: {
      nextCheckAt: "2026-06-24T00:00:00.000Z",
      notes: "first",
      serviceName: "GitHub Actions",
      // Upstream-managed fields the rail never edits:
      scheduledBy: "assignee",
      kind: "external_service",
      externalRef: "run-123",
    },
  };

  it("preserves upstream-managed monitor fields when only the rail fields are edited", () => {
    const next = patchExecutionPolicy(withMonitor, {
      monitor: {
        nextCheckAt: "2026-07-01T12:00:00.000Z",
        notes: "rescheduled",
        serviceName: "GitHub Actions",
      },
    });
    expect(next?.monitor).toEqual({
      nextCheckAt: "2026-07-01T12:00:00.000Z",
      notes: "rescheduled",
      serviceName: "GitHub Actions",
      scheduledBy: "assignee",
      kind: "external_service",
      externalRef: "run-123",
    });
  });

  it("clears the monitor entirely when the edit is monitor:null", () => {
    const next = patchExecutionPolicy(withMonitor, { monitor: null });
    expect(next).toBeNull(); // no monitor and no stages → whole policy cleared
  });

  it("preserves unmanaged top-level policy fields (e.g. commentRequired) on a reviewer edit", () => {
    // commentRequired is a real upstream executionPolicy field the rail never
    // edits; it must survive a stage edit instead of resetting to its default.
    const policy = {
      mode: "normal",
      stages: [],
      commentRequired: false,
    } as unknown as IssueExecutionPolicy;
    const next = patchExecutionPolicy(policy, { reviewers: ["agent:agt_1"] });
    expect((next as Record<string, unknown>)?.commentRequired).toBe(false);
  });

  it("keeps a policy carrying only unmanaged fields instead of nulling it", () => {
    const policy = {
      mode: "normal",
      stages: [],
      commentRequired: false,
    } as unknown as IssueExecutionPolicy;
    // Clearing the (absent) monitor leaves no monitor and no stages, but the
    // unmanaged commentRequired must not be silently dropped.
    const next = patchExecutionPolicy(policy, { monitor: null });
    expect(next).not.toBeNull();
    expect((next as Record<string, unknown>)?.commentRequired).toBe(false);
  });

  it("editing reviewers preserves the existing monitor", () => {
    const next = patchExecutionPolicy(withMonitor, {
      reviewers: ["agent:agt_1"],
    });
    expect(next?.monitor?.externalRef).toBe("run-123");
    expect(
      next?.stages?.find((s) => s.type === "review")?.participants,
    ).toEqual([
      expect.objectContaining({ type: "agent", agentId: "agt_1", userId: null }),
    ]);
  });
});
