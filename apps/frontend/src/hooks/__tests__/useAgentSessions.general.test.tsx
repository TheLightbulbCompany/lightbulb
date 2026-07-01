import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]): unknown => useGatewayRpc(...args),
}));

interface SessionRow { key: string; updatedAt?: number }
function rpcReturn(rows: SessionRow[]): {
  data: { sessions: SessionRow[] };
  error: undefined;
  isLoading: boolean;
  mutate: () => Promise<undefined>;
} {
  return { data: { sessions: rows }, error: undefined, isLoading: false, mutate: async () => undefined };
}

describe("useAgentSessions — legacy General row", () => {
  beforeEach(() => {
    vi.resetModules();
    useGatewayRpc.mockReset();
  });

  it("pins a General (null) row first when the 3-part key exists", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1", updatedAt: 50 },
        { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 500 },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));
    expect(result.current.sessions[0].sessionId).toBeNull();
    expect(result.current.sessions[0].label).toBe("General");
    expect(result.current.sessions).toHaveLength(2);
  });

  it("omits the General row when no legacy key exists", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222" },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));
    expect(result.current.sessions.some((s) => s.sessionId === null)).toBe(false);
  });
});
