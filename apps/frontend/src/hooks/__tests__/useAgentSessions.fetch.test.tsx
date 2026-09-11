import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

// useAgentSessions reaches the gateway only through useRuntimeRpcMutation, so
// callRpc is the whole surface to stub.
const callRpc = vi.fn().mockResolvedValue({});
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: vi.fn(),
  useRuntimeRpcMutation: (): typeof callRpc => callRpc,
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

import { useAgentSessions } from "../useAgentSessions";

function stubSessionsList(rows: { key: string; updatedAt?: number }[]): void {
  callRpc.mockImplementation(async (method: string) =>
    method === "sessions.list" ? { sessions: rows, hasMore: false } : {},
  );
}

function renderAgentSessions(agentId: string | null, userId: string | null) {
  return renderHook(() => useAgentSessions(agentId, userId), {
    wrapper: ({ children }: { children: React.ReactNode }) => (
      <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
    ),
  });
}

function listCalls(): [string, Record<string, unknown>][] {
  return callRpc.mock.calls.filter(([m]) => m === "sessions.list") as [
    string,
    Record<string, unknown>,
  ][];
}

describe("useAgentSessions — the fetch is scoped to one agent", () => {
  beforeEach(() => {
    callRpc.mockReset().mockResolvedValue({});
  });

  it("requests one page scoped to the agent, not a walk of every agent's store", async () => {
    stubSessionsList([]);
    renderAgentSessions("agent_a", "user_1");
    await waitFor(() => expect(listCalls()).toHaveLength(1));
    expect(listCalls()[0][1]).toMatchObject({ agentId: "agent_a" });
    // The walk is gone: no cursor is sent, so nothing can page.
    expect(listCalls()[0][1].offset).toBeUndefined();
  });

  it("still asks for the preview it now renders", async () => {
    // includeDerivedTitles already forces the transcript projection; the same
    // single batched read produces lastMessagePreview, so asking for it is free.
    stubSessionsList([]);
    renderAgentSessions("agent_a", "user_1");
    await waitFor(() => expect(listCalls()).toHaveLength(1));
    expect(listCalls()[0][1]).toMatchObject({
      includeDerivedTitles: true,
      includeLastMessage: true,
    });
  });

  it("keeps a separate cache per agent", async () => {
    stubSessionsList([]);
    renderHook(
      () => [useAgentSessions("agent_a", "user_1"), useAgentSessions("agent_b", "user_1")] as const,
      {
        wrapper: ({ children }: { children: React.ReactNode }) => (
          <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
        ),
      },
    );
    await waitFor(() => {
      const scoped = listCalls().map(([, p]) => p.agentId);
      expect(new Set(scoped)).toEqual(new Set(["agent_a", "agent_b"]));
    });
  });

  it("re-fetches for a different user — the key is per user AND agent", async () => {
    stubSessionsList([]);
    const { rerender } = renderHook(
      ({ userId }: { userId: string }) => useAgentSessions("friday", userId),
      {
        initialProps: { userId: "user_1" },
        wrapper: ({ children }: { children: React.ReactNode }) => (
          <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
        ),
      },
    );
    await waitFor(() => expect(listCalls()).toHaveLength(1));
    rerender({ userId: "user_2" });
    await waitFor(() => expect(listCalls()).toHaveLength(2));
  });
});
