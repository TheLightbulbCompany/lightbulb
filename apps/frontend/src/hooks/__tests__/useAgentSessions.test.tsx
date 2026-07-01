import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]): unknown => useGatewayRpc(...args),
}));

interface SessionRow {
  key: string;
  derivedTitle?: string;
  title?: string;
  updatedAt?: number;
  totalTokens?: number;
}
function rpcReturn(rows: SessionRow[]): {
  data: { sessions: SessionRow[] };
  error: undefined;
  isLoading: boolean;
  mutate: () => Promise<undefined>;
} {
  return { data: { sessions: rows }, error: undefined, isLoading: false, mutate: async () => undefined };
}

describe("useAgentSessions — filtered per-agent 4-part rows", () => {
  beforeEach(() => {
    vi.resetModules();
    useGatewayRpc.mockReset();
  });

  it("returns only this agent's user-owned 4-part sessions with a label", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", derivedTitle: "Taxes", updatedAt: 200, totalTokens: 5 },
        { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222", updatedAt: 300 },
        // legacy General (3-part) — excluded here, added in Task 12
        { key: "agent:agent-X:user-1", updatedAt: 100 },
        // other agent — excluded
        { key: "agent:agent-Y:user-1:33333333-3333-4333-8333-333333333333" },
        // other member — excluded
        { key: "agent:agent-X:user-2:44444444-4444-4444-8444-444444444444" },
        // malformed trailing segment (not a UUID) — excluded
        { key: "agent:agent-X:user-1:not-a-uuid" },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));

    expect(result.current.sessions.map((s) => s.sessionId)).toEqual([
      "22222222-2222-4222-8222-222222222222", // newer first
      "11111111-1111-4111-8111-111111111111",
    ]);
    expect(result.current.sessions[1].label).toBe("Taxes");
    // Untitled falls back to a short id label, never blank.
    expect(result.current.sessions[0].label).not.toBe("");
  });

  it("returns empty and does not query when agentId is null", async () => {
    useGatewayRpc.mockReturnValue(rpcReturn([]));
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions(null, "user-1"));
    expect(result.current.sessions).toEqual([]);
    expect(useGatewayRpc).toHaveBeenCalledWith(null, expect.anything(), expect.anything());
  });
});
