import { describe, test, expect } from "vitest";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";

// Path-prefix contract: keys returned here are RELATIVE — `useTeamsApi.read`
// (apps/frontend/src/hooks/useTeamsApi.ts) prepends `/teams` to compose the
// final SWR cache key and HTTP URL. So `inbox.list("mine", {})` returns
// `/inbox?tab=mine` and the runtime SWR key is `/teams/inbox?tab=mine`.
describe("teamsQueryKeys", () => {
  test("inbox.list builds a path with tab + filter query string", () => {
    const key = teamsQueryKeys.inbox.list("mine", { status: "todo", search: "fix" });
    expect(key).toBe("/inbox?tab=mine&status=todo&search=fix");
  });

  test("inbox.list with no filters omits empty filter args", () => {
    expect(teamsQueryKeys.inbox.list("all", {})).toBe("/inbox?tab=all");
  });

  test("inbox.list serializes filter values url-encoded", () => {
    expect(teamsQueryKeys.inbox.list("mine", { search: "fix bug" })).toBe(
      "/inbox?tab=mine&search=fix%20bug"
    );
  });

  test("issues.detail and approvals.detail build per-id keys", () => {
    expect(teamsQueryKeys.issues.detail("iss_1")).toBe("/issues/iss_1");
    expect(teamsQueryKeys.approvals.detail("a1")).toBe("/approvals/a1");
  });

  test("inbox.approvals + inbox.runs + inbox.liveRuns return fixed keys", () => {
    expect(teamsQueryKeys.inbox.approvals()).toBe("/inbox/approvals");
    expect(teamsQueryKeys.inbox.runs()).toBe("/inbox/runs");
    expect(teamsQueryKeys.inbox.liveRuns()).toBe("/inbox/live-runs");
  });

  test("issues.comments + runs.detail + members + projects return relative keys", () => {
    expect(teamsQueryKeys.issues.comments("iss_2")).toBe("/issues/iss_2/comments");
    expect(teamsQueryKeys.runs.detail("r1")).toBe("/runs/r1");
    expect(teamsQueryKeys.members()).toBe("/members");
    expect(teamsQueryKeys.projects()).toBe("/projects");
  });

  test("projectDetail + issuesByProject build the Project-detail keys", () => {
    expect(teamsQueryKeys.projectDetail("p_1")).toBe("/projects/p_1");
    expect(teamsQueryKeys.issuesByProject("p_1")).toBe("/issues?project=p_1");
  });
});
