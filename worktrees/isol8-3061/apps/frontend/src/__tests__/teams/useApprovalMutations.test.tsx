// apps/frontend/src/__tests__/teams/useApprovalMutations.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({}); swrMutate.mockReset(); });

describe("useApprovalMutations", () => {
  it("approve posts {} and revalidates approvals + dashboard", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.approve("a1"); });
    expect(post).toHaveBeenCalledWith("/approvals/a1/approve", {});
    expect(swrMutate).toHaveBeenCalledWith("/teams/approvals");
    expect(swrMutate).toHaveBeenCalledWith("/teams/dashboard");
  });

  it("reject posts {reason} and revalidates", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await act(async () => { await result.current.reject("a1", "no good"); });
    expect(post).toHaveBeenCalledWith("/approvals/a1/reject", { reason: "no good" });
    expect(swrMutate).toHaveBeenCalledWith("/teams/approvals");
  });
});
