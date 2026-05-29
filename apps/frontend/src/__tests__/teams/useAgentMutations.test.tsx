// apps/frontend/src/__tests__/teams/useAgentMutations.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const patch = vi.fn();
const post = vi.fn();
const callRpc = vi.fn();
const swrMutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: vi.fn(), post, patch, del: vi.fn() }),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpcMutation: () => callRpc,
}));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

beforeEach(() => { patch.mockReset(); post.mockReset(); callRpc.mockReset(); swrMutate.mockReset(); });

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

  it("setReportsTo with null sends empty string (no-manager)", async () => {
    patch.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.setReportsTo("a1", null); });
    expect(patch).toHaveBeenCalledWith("/agents/a1", { reportsTo: "" });
  });

  it("pause/resume POST to the right routes", async () => {
    post.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.pause("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/pause", {});
    await act(async () => { await result.current.resume("a1"); });
    expect(post).toHaveBeenCalledWith("/agents/a1/resume", {});
  });

  it("remove deletes via gateway agents.delete RPC", async () => {
    callRpc.mockResolvedValue({});
    const { result } = renderHook(() => useAgentMutations());
    await act(async () => { await result.current.remove("a1"); });
    expect(callRpc).toHaveBeenCalledWith("agents.delete", { agentId: "a1", deleteFiles: true });
    expect(swrMutate).toHaveBeenCalled();
  });
});
