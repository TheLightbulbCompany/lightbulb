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

import { useAgentSessions } from "../useAgentSessions";

describe("useAgentSessions — legacy General row", () => {
  beforeEach(() => {
    callRpc.mockReset().mockResolvedValue({});
  });

  it("pins a General (null) row first when the 3-part key exists", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1", updatedAt: 50 },
      { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 500 },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    expect(result.current.sessions[0].sessionId).toBeNull();
    expect(result.current.sessions[0].label).toBe("General");
  });

  it("omits the General row when no legacy key exists", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222" },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions.some((s) => s.sessionId === null)).toBe(false);
  });
});
