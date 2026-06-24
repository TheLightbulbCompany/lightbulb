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
