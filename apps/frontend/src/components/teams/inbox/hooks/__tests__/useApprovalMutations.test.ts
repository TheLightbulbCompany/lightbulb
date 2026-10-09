import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const post = vi.fn(async () => ({}));
const mutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post, mutate }) }));

import { useApprovalMutations } from "../useApprovalMutations";

describe("useApprovalMutations", () => {
  it("refreshes the single-approval read, so a card in a message stops showing Pending", async () => {
    const { result } = renderHook(() => useApprovalMutations());
    await result.current.approve("a1");
    const predicates = mutate.mock.calls.map(([key]) => key).filter((key) => typeof key === "function");
    expect(predicates.some((matches) => matches("/teams/approvals/a1"))).toBe(true);
    expect(predicates.some((matches) => matches("/teams/agents/a1"))).toBe(false);
  });
});
