// apps/frontend/src/__tests__/teams/useAgentMutations.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const patch = vi.fn();
const post = vi.fn();
const del = vi.fn();
const swrMutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: vi.fn(), post, patch, del }),
}));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

beforeEach(() => { patch.mockReset(); post.mockReset(); del.mockReset(); swrMutate.mockReset(); });

describe("useAgentMutations", () => {
  it("setReportsTo PATCHes reportsTo and revalidates list + detail keys", async () => {
    patch.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.setReportsTo("a1", "mgr1"); });
    expect(patch).toHaveBeenCalledWith("/agents/a1", { reportsTo: "mgr1" });
    // Both the list key and the per-agent detail key are invalidated so the
    // AgentDetailPanel/AgentManageSection don't show stale state.
    expect(swrMutate).toHaveBeenCalledWith("/teams/agents");
    expect(swrMutate).toHaveBeenCalledWith("/teams/agents/a1");
  });

  it("setReportsTo with null clears the manager (sends null, not \"\")", async () => {
    patch.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.setReportsTo("a1", null); });
    // Paperclip rejects "" (uuid().nullable()); the clear must send null.
    expect(patch).toHaveBeenCalledWith("/agents/a1", { reportsTo: null });
  });

  it("pause/resume POST to the right routes", async () => {
    post.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.pause("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/pause", {});
    await act(async () => { await result.current.resume("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/resume", {});
  });

  it("resetSession/clearError POST to the recovery routes and revalidate", async () => {
    post.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.resetSession("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/reset-session", {});
    await act(async () => { await result.current.clearError("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/clear-error", {});
    // Both revalidate the list + detail keys so the status dot heals immediately.
    expect(swrMutate).toHaveBeenCalledWith("/teams/agents");
    expect(swrMutate).toHaveBeenCalledWith("/teams/agents/a1");
  });

  it("remove uses the unified lifecycle endpoint and drops the completed agent", async () => {
    del.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.remove("a1"); });
    expect(del).toHaveBeenCalledWith("/agents/a1");

    // The backend returns only after every lifecycle surface is reconciled.
    const listCall = swrMutate.mock.calls.find((c) => c[0] === "/teams/agents");
    expect(listCall).toBeTruthy();
    expect(listCall![2]).toEqual({ revalidate: false });
    const updater = listCall![1] as (cur: unknown) => unknown;
    expect(updater({ agents: [{ id: "a1" }, { id: "a2" }] })).toEqual({ agents: [{ id: "a2" }] });

    // Detail cache cleared, also without revalidation.
    const detailCall = swrMutate.mock.calls.find((c) => c[0] === "/teams/agents/a1");
    expect(detailCall).toBeTruthy();
    expect(detailCall![1]).toBeUndefined();
    expect(detailCall![2]).toEqual({ revalidate: false });
  });
});
