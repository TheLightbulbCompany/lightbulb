import { describe, expect, it } from "vitest";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { HeartbeatRun } from "@/components/teams/shared/types";

const run = (over: Partial<HeartbeatRun>): HeartbeatRun =>
  ({
    id: "r1",
    status: "failed",
    agentId: "a1",
    failureReason: "timeout",
    startedAt: "2026-09-08T10:00:00Z",
    ...over,
  }) as HeartbeatRun;

describe("groupFailedRuns", () => {
  it("collapses identical agent+reason failures and counts them", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2" })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
  });

  it("keeps distinct reasons apart", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2", failureReason: "oom" })]);
    expect(groups).toHaveLength(2);
  });

  it("keeps the same reason from different agents apart", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2", agentId: "a2" })]);
    expect(groups).toHaveLength(2);
  });

  it("represents a group by its most recent occurrence", () => {
    const groups = groupFailedRuns([
      run({ id: "old", startedAt: "2026-09-08T09:00:00Z" }),
      run({ id: "new", startedAt: "2026-09-08T11:00:00Z" }),
    ]);
    expect(groups[0].representative.id).toBe("new");
  });

  it("keeps every member so a dismiss can ack them all", () => {
    const groups = groupFailedRuns([run({ id: "r1" }), run({ id: "r2" }), run({ id: "r3" })]);
    expect(groups[0].runs.map((r) => r.id)).toEqual(["r1", "r2", "r3"]);
  });
});
