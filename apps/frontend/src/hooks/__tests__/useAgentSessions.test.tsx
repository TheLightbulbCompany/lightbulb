import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const useGatewayRpc = vi.fn();
const callRpc = vi.fn().mockResolvedValue({});
const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyApiError: (...args: unknown[]): unknown => notifyApiError(...args),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]): unknown => useGatewayRpc(...args),
  useGatewayRpcMutation: (): typeof callRpc => callRpc,
}));

interface SessionRow {
  key: string;
  label?: string;
  derivedTitle?: string;
  title?: string;
  updatedAt?: number;
  totalTokens?: number;
  model?: string;
  modelProvider?: string;
  thinkingLevel?: string;
  fastMode?: boolean;
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
    callRpc.mockClear().mockResolvedValue({});
    notifyApiError.mockClear();
  });

  it("returns only this agent's user-owned 4-part sessions with a label", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", derivedTitle: "Taxes", updatedAt: 200, totalTokens: 5 },
        { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222", updatedAt: 300 },
        // legacy General (3-part) — surfaced as the pinned null row
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
      null, // legacy General pinned first
      "22222222-2222-4222-8222-222222222222", // newer first
      "11111111-1111-4111-8111-111111111111",
    ]);
    expect(result.current.sessions[2].label).toBe("Taxes");
    // Untitled falls back to a short id label, never blank.
    expect(result.current.sessions[1].label).not.toBe("");
  });

  it("carries per-session model/effort overrides through on 4-part and General rows", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        {
          key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111",
          model: "openai/gpt-5",
          modelProvider: "openai",
          thinkingLevel: "high",
          fastMode: true,
          updatedAt: 200,
        },
        // legacy General carries its OWN overrides, not the 4-part row's
        { key: "agent:agent-X:user-1", model: "bedrock/claude", thinkingLevel: "low", updatedAt: 100 },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));

    const general = result.current.sessions.find((s) => s.sessionId === null);
    const named = result.current.sessions.find(
      (s) => s.sessionId === "11111111-1111-4111-8111-111111111111",
    );

    expect(named).toMatchObject({
      model: "openai/gpt-5",
      modelProvider: "openai",
      thinkingLevel: "high",
      fastMode: true,
    });
    expect(general).toMatchObject({ model: "bedrock/claude", thinkingLevel: "low" });
    // fastMode absent on General stays undefined (== inherit), never coerced.
    expect(general?.fastMode).toBeUndefined();
  });

  it("returns empty and does not query when agentId is null", async () => {
    useGatewayRpc.mockReturnValue(rpcReturn([]));
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions(null, "user-1"));
    expect(result.current.sessions).toEqual([]);
    expect(useGatewayRpc).toHaveBeenCalledWith(null, expect.anything(), expect.anything());
  });

  it("matches case-insensitively — lowercased sessions.list keys vs a mixed-case Clerk id", async () => {
    // OpenClaw lowercases the whole session key on store/lookup, so sessions.list
    // returns lowercased keys while the Clerk user/agent id passed in is
    // mixed-case. Regression guard: a case-sensitive compare dropped every row,
    // rendering an empty sidebar and never hydrating the pill.
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-x:user_2abcdef:11111111-1111-4111-8111-111111111111", updatedAt: 200 },
        { key: "agent:agent-x:user_2abcdef", updatedAt: 100 }, // legacy General, lowercased
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("Agent-X", "user_2AbcDEF"));

    expect(result.current.sessions.map((s) => s.sessionId)).toEqual([
      null, // General still surfaces
      "11111111-1111-4111-8111-111111111111",
    ]);
  });
  it("carries the untruncated title as rawLabel alongside the truncated display label", async () => {
    const longTitle = "b".repeat(60);
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        {
          key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
          derivedTitle: longTitle,
          updatedAt: 200,
        },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-x", "user-1"));
    expect(result.current.sessions[0].label).toBe(`${longTitle.slice(0, 48)}…`);
    expect(result.current.sessions[0].rawLabel).toBe(longTitle);
  });

  it("prefers a user-set label over derived titles", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        {
          key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
          label: "Taxes 2026",
          derivedTitle: "help me with my taxes",
          updatedAt: 200,
        },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-x", "user-1"));
    expect(result.current.sessions[0].label).toBe("Taxes 2026");
  });

  it("a failed rename surfaces a toast instead of rejecting silently", async () => {
    useGatewayRpc.mockReturnValue(rpcReturn([]));
    callRpc.mockRejectedValueOnce(new Error("gateway dropped"));
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-x", "user-1"));
    // Must resolve (not reject) — the caller fire-and-forgets it.
    await result.current.renameSession("agent:agent-x:user-1:11111111-1111-4111-8111-111111111111", "X");
    expect(notifyApiError).toHaveBeenCalledWith(
      expect.any(Error),
      "Failed to rename session",
      expect.anything(),
    );
  });

  it("renameSession patches the label and revalidates", async () => {
    useGatewayRpc.mockReturnValue(rpcReturn([]));
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-x", "user-1"));
    await result.current.renameSession(
      "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
      "Taxes 2026",
    );
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
      label: "Taxes 2026",
    });
  });
});
