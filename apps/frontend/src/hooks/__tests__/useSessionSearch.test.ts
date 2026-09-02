import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";

// useSessionSearch is a thin debounce/gate layer over useGatewayRpc — mock the
// RPC hook itself so these tests assert the gating/params contract, not SWR.
const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]) => useGatewayRpc(...args),
}));

import { useSessionSearch } from "../useSessionSearch";

const KEYS = ["agent:agent-1:user_1", "agent:agent-1:user_1:11111111-1111-4111-8111-111111111111"];

function renderSearch(agentId: string | null, query: string, sessionKeys: string[] = KEYS) {
  return renderHook(
    ({ agentId, query, sessionKeys }: { agentId: string | null; query: string; sessionKeys: string[] }) =>
      useSessionSearch(agentId, query, sessionKeys),
    {
      initialProps: { agentId, query, sessionKeys },
    },
  );
}

describe("useSessionSearch", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useGatewayRpc.mockReset().mockReturnValue({ data: undefined, error: undefined, isLoading: false });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not call the RPC under the 2-character minimum, even after the debounce window", () => {
    renderSearch("agent-1", "a");
    act(() => { vi.advanceTimersByTime(500); });
    expect(useGatewayRpc).toHaveBeenLastCalledWith(null, undefined);
  });

  it("does not call the RPC with no agent selected", () => {
    renderSearch(null, "hello");
    act(() => { vi.advanceTimersByTime(500); });
    expect(useGatewayRpc).toHaveBeenLastCalledWith(null, undefined);
  });

  it("debounces rapid keystrokes to a single trailing query", () => {
    const { rerender } = renderSearch("agent-1", "h");
    for (const q of ["he", "hel", "hell", "hello"]) {
      act(() => { vi.advanceTimersByTime(50); }); // well under the 300ms window
      rerender({ agentId: "agent-1", query: q, sessionKeys: KEYS });
    }
    // Before the debounce settles, no search call has fired yet.
    expect(useGatewayRpc).toHaveBeenLastCalledWith(null, undefined);

    act(() => { vi.advanceTimersByTime(300); });

    expect(useGatewayRpc).toHaveBeenLastCalledWith(
      "sessions.search",
      { agentId: "agent-1", sessionKeys: KEYS, query: "hello", limit: expect.any(Number) },
    );
    // Only the trailing value was ever searched.
    const searchCalls = useGatewayRpc.mock.calls.filter(([method]) => method === "sessions.search");
    expect(searchCalls.every(([, params]) => (params as { query: string }).query === "hello")).toBe(true);
  });

  it("passes agentId/query/limit once the debounce settles past the minimum", () => {
    renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    expect(useGatewayRpc).toHaveBeenLastCalledWith(
      "sessions.search",
      { agentId: "agent-1", sessionKeys: KEYS, query: "taxes", limit: expect.any(Number) },
    );
  });

  it("trims whitespace and does not count padding toward the minimum length", () => {
    renderSearch("agent-1", "  a  ");
    act(() => { vi.advanceTimersByTime(300); });
    expect(useGatewayRpc).toHaveBeenLastCalledWith(null, undefined);
  });

  it("surfaces results, indexing, and truncated from the RPC result", () => {
    useGatewayRpc.mockReturnValue({
      data: {
        results: [
          {
            sessionKey: "agent:a:u:11111111-1111-4111-8111-111111111111",
            sessionId: "11111111-1111-4111-8111-111111111111",
            messageId: "msg-1",
            role: "user",
            timestamp: 1000,
            snippet: "about my taxes",
            score: 0.9,
          },
        ],
        indexing: true,
        truncated: true,
      },
      error: undefined,
      isLoading: false,
    });
    const { result } = renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    expect(result.current.results).toHaveLength(1);
    expect(result.current.indexing).toBe(true);
    expect(result.current.truncated).toBe(true);
    expect(result.current.isActive).toBe(true);
  });

  it("reports isLoading only while a search is actually active", () => {
    useGatewayRpc.mockReturnValue({ data: undefined, error: undefined, isLoading: true });
    const { result: idleResult } = renderSearch("agent-1", "a");
    act(() => { vi.advanceTimersByTime(300); });
    expect(idleResult.current.isLoading).toBe(false);

    const { result: activeResult } = renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    expect(activeResult.current.isLoading).toBe(true);
  });

  it("surfaces an RPC error", () => {
    const err = new Error("gateway unavailable");
    useGatewayRpc.mockReturnValue({ data: undefined, error: err, isLoading: false });
    const { result } = renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    expect(result.current.error).toBe(err);
  });

  it("sends sessionKeys alongside agentId — the gateway refuses one without the other", () => {
    // OpenClaw 8.1 sessions-read.ts: for a CONFIGURED agent, `agentId` with no
    // `sessionKeys` is INVALID_REQUEST "agentId requires sessionKeys" (and
    // dropping agentId would search the "main" agent instead). Prod answered
    // exactly that on every keystroke during the 8.1 walk.
    renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    const [, params] = useGatewayRpc.mock.calls.at(-1) as [string, Record<string, unknown>];
    expect(params.sessionKeys).toEqual(KEYS);
    expect(params.agentId).toBe("agent-1");
  });

  it("does not call the RPC when the agent has no sessions to scope the search to", () => {
    renderSearch("agent-1", "taxes", []);
    act(() => { vi.advanceTimersByTime(300); });
    expect(useGatewayRpc).toHaveBeenLastCalledWith(null, undefined);
  });

  it("caps the key list so a long-lived agent cannot blow the frame", () => {
    const many = Array.from({ length: 250 }, (_, i) => `agent:agent-1:user_1:s${i}`);
    renderSearch("agent-1", "taxes", many);
    act(() => { vi.advanceTimersByTime(300); });
    const [, params] = useGatewayRpc.mock.calls.at(-1) as [string, Record<string, unknown>];
    expect((params.sessionKeys as string[]).length).toBe(200);
    expect((params.sessionKeys as string[])[0]).toBe("agent:agent-1:user_1:s0");
  });

  it("clears results once the query drops back under the minimum", () => {
    useGatewayRpc.mockReturnValue({
      data: { results: [{ sessionKey: "k", sessionId: "s", messageId: "m", role: "user", timestamp: 1, snippet: "x", score: 1 }] },
      error: undefined,
      isLoading: false,
    });
    const { result, rerender } = renderSearch("agent-1", "taxes");
    act(() => { vi.advanceTimersByTime(300); });
    expect(result.current.results).toHaveLength(1);

    useGatewayRpc.mockReturnValue({ data: undefined, error: undefined, isLoading: false });
    rerender({ agentId: "agent-1", query: "t", sessionKeys: KEYS });
    act(() => { vi.advanceTimersByTime(300); });
    expect(result.current.isActive).toBe(false);
    expect(result.current.results).toHaveLength(0);
  });
});
