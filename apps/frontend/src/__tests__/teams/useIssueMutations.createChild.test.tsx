// apps/frontend/src/__tests__/teams/useIssueMutations.createChild.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const patch = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post, patch }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({ id: "child1" }); swrMutate.mockReset(); });

describe("useIssueMutations.createChild", () => {
  it("POSTs the children endpoint and revalidates the children key", async () => {
    const { result } = renderHook(() => useIssueMutations());
    let created;
    await act(async () => { created = await result.current.createChild("parent1", { title: "Sub" }); });
    expect(post).toHaveBeenCalledWith("/issues/parent1/children", { title: "Sub" });
    expect(created).toEqual({ id: "child1" });
    expect(swrMutate).toHaveBeenCalledWith("/teams/issues/parent1/children");
  });
});
