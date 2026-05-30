// apps/frontend/src/__tests__/teams/useApprovalMutations.issueKey.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({}); swrMutate.mockReset(); });

describe("useApprovalMutations issue-approvals invalidation", () => {
  it("approve invalidates the per-issue approvals predicate", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.approve("ap1"); });
    const predicate = swrMutate.mock.calls.map((c) => c[0]).find((a) => typeof a === "function");
    expect(predicate).toBeTruthy();
    expect(predicate("/teams/issues/iss1/approvals")).toBe(true);
    expect(predicate("/teams/approvals")).toBe(false);
    expect(predicate("/teams/issues/iss1")).toBe(false);
  });
});
