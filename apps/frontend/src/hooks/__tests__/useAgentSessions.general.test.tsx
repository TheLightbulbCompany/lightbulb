import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

// Task 5 moved useAgentSessions' fetch onto a paging useSWR fetcher built on
// useGatewayRpcMutation — useGatewayRpc itself is no longer called by the
// hook, so callRpc is the whole surface to stub.
const callRpc = vi.fn().mockResolvedValue({});
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: vi.fn(),
  useGatewayRpcMutation: (): typeof callRpc => callRpc,
}));

interface SessionRow { key: string; updatedAt?: number }

function stubSessionsList(rows: SessionRow[]): void {
  callRpc.mockImplementation(async (method: string) =>
    method === "sessions.list" ? { sessions: rows, hasMore: false } : {},
  );
}

// SWR's cache is module-global; a fresh Map provider per render keeps one
// test's cached page from bleeding into the next.
function renderAgentSessions(agentId: string | null, userId: string | null) {
  return renderHook(() => useAgentSessions(agentId, userId), {
    wrapper: ({ children }: { children: React.ReactNode }) => (
      <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
    ),
  });
}

import { useAgentSessions, LEGACY_ROW_LABEL } from "../useAgentSessions";

describe("useAgentSessions — legacy 3-part row", () => {
  beforeEach(() => {
    callRpc.mockReset().mockResolvedValue({});
  });

  it("labels the legacy row for what it actually is, not 'General'", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1", updatedAt: 50 },
      { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 500 },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    const legacy = result.current.sessions.find((s) => s.sessionId === null)!;
    expect(legacy.label).toBe(LEGACY_ROW_LABEL);
    expect(legacy.label).not.toBe("General");
  });

  it("does not pin the legacy row above newer sessions", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1", updatedAt: 1 },
      { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 999 },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    expect(result.current.sessions[0].sessionId).not.toBeNull();
  });

  it("does not pin the legacy row to the BOTTOM either — it sorts by recency", async () => {
    // Guards the opposite bug: the projected legacy row carried no updatedAt
    // and the sort ran before it was added, so flipping unshift->push alone
    // would pin it last forever.
    stubSessionsList([
      { key: "agent:agent-X:user-1", updatedAt: 999 },
      { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 1 },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    expect(result.current.sessions[0].sessionId).toBeNull();
    expect(result.current.sessions[0].updatedAt).toBe(999);
  });

  it("omits the legacy row when no legacy key exists", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222" },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions.some((s) => s.sessionId === null)).toBe(false);
  });
});
