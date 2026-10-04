// @vitest-environment jsdom
//
// These assert the guarantees that must hold REGARDLESS of which host mounts
// the hook: anything it leans on an ancestor provider for silently stops
// working wherever that provider is not an ancestor.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

let emitChatMessage: (msg: Record<string, unknown>) => void = () => {};
let liveConnected = true;
let onChatMessage = (handler: (msg: Record<string, unknown>) => void) => {
  emitChatMessage = handler;
  return () => {
    if (emitChatMessage === handler) emitChatMessage = () => {};
  };
};
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    liveConnected,
    onChatMessage,
  }),
}));

interface TeamsRow {
  id: string;
  status?: string | null;
  role?: string | null;
  seeded?: boolean;
}
let teamsAgents: TeamsRow[] = [];
let lastSwrOpts: Record<string, unknown> | undefined;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (_path: string, opts?: Record<string, unknown>) => {
      lastSwrOpts = opts;
      return { data: { agents: teamsAgents } };
    },
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { useRosterEntries } from "../useRosterEntries";
import type { Agent } from "@/hooks/useAgents";

const agent = (id: string, name: string): Agent => ({ id, name });

beforeEach(() => {
  teamsAgents = [];
  lastSwrOpts = undefined;
  liveConnected = true;
  emitChatMessage = () => {};
  onChatMessage = (handler) => {
    emitChatMessage = handler;
    return () => {
      if (emitChatMessage === handler) emitChatMessage = () => {};
    };
  };
});

describe("useRosterEntries", () => {
  it("owns its own liveness instead of relying on an ancestor provider", () => {
    // SwrProvider disables focus revalidation globally — so without these a
    // host outside TeamsEventsProvider fetched `/teams/agents` once at mount
    // and never again.
    renderHook(() => useRosterEntries([agent("a", "Ada")]));
    expect(lastSwrOpts?.refreshInterval).toBeGreaterThan(0);
    expect(lastSwrOpts?.revalidateOnFocus).toBe(true);
    // Must clear the read layer's 15s dedup window or ticks are swallowed.
    expect(lastSwrOpts?.refreshInterval as number).toBeGreaterThan(15_000);
  });

  it("maps durable BFF status onto roster state", () => {
    teamsAgents = [
      { id: "a", status: "error" },
      { id: "b", status: "terminated" },
      { id: "c", status: "thinking" },
    ];
    const { result } = renderHook(() =>
      useRosterEntries([agent("a", "Ada"), agent("b", "Bo"), agent("c", "Cy")]),
    );
    expect(result.current.map((e) => e.state)).toEqual(["attention", "terminated", "running"]);
  });

  it("derives the CEO from the Paperclip role, never from list position", () => {
    teamsAgents = [
      { id: "a", status: "idle" },
      { id: "b", status: "idle", role: "ceo", seeded: true },
    ];
    const { result } = renderHook(() => useRosterEntries([agent("a", "Ada"), agent("b", "Friday")]));
    expect(result.current.find((e) => e.agent.id === "b")?.isCeo).toBe(true);
    expect(result.current.find((e) => e.agent.id === "a")?.isCeo).toBe(false);
    expect(result.current.find((e) => e.agent.id === "b")?.isSeeded).toBe(true);
  });

  it("claims no CEO at all when the roster read has not landed", () => {
    const { result } = renderHook(() => useRosterEntries([agent("a", "Ada")]));
    expect(result.current[0].isCeo).toBe(false);
    expect(result.current[0].state).toBe("idle");
  });

  it("lights an idle agent from a live run, and clears on run_ended", () => {
    teamsAgents = [{ id: "a", status: "idle" }];
    const { result } = renderHook(() => useRosterEntries([agent("a", "Ada")]));
    expect(result.current[0].state).toBe("idle");

    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    expect(result.current[0].state).toBe("running");

    act(() => emitChatMessage({
      type: "run_ended",
      runId: "run-1",
      outcome: "done",
      agentId: "a",
      sessionId: "s1",
    }));
    expect(result.current[0].state).toBe("idle");
  });

  it("shares a live run with hook instances mounted after it starts", () => {
    teamsAgents = [{ id: "a", status: "idle" }];
    const first = renderHook(() => useRosterEntries([agent("a", "Ada")]));

    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    expect(first.result.current[0].state).toBe("running");

    const late = renderHook(() => useRosterEntries([agent("a", "Ada")]));
    expect(late.result.current[0].state).toBe("running");
  });

  it("clears cached live runs across a delivery disconnect", () => {
    teamsAgents = [{ id: "a", status: "idle" }];
    const mounted = renderHook(() => useRosterEntries([agent("a", "Ada")]));
    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    expect(mounted.result.current[0].state).toBe("running");

    liveConnected = false;
    mounted.rerender();
    expect(mounted.result.current[0].state).toBe("idle");

    liveConnected = true;
    mounted.rerender();
    expect(mounted.result.current[0].state).toBe("idle");
  });

  it("keeps listening while disconnected so an immediate reconnect replay is not missed", () => {
    teamsAgents = [{ id: "a", status: "idle" }];
    const mounted = renderHook(() => useRosterEntries([agent("a", "Ada")]));

    liveConnected = false;
    mounted.rerender();
    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    liveConnected = true;
    mounted.rerender();

    expect(mounted.result.current[0].state).toBe("running");
  });

  it("keeps an agent lit while ANY of its sessions is still running", () => {
    // A flat per-agent boolean cleared the rail when either of two concurrent
    // sessions finished. `run_active` carries sessionId, so key on it.
    teamsAgents = [{ id: "a", status: "idle" }];
    const { result } = renderHook(() => useRosterEntries([agent("a", "Ada")]));

    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s2" }));
    act(() => emitChatMessage({ type: "run_active", active: false, agentId: "a", sessionId: "s1" }));
    expect(result.current[0].state).toBe("running");

    act(() => emitChatMessage({ type: "run_active", active: false, agentId: "a", sessionId: "s2" }));
    expect(result.current[0].state).toBe("idle");
  });

  it("never lets a live run override a state that wants a human", () => {
    teamsAgents = [{ id: "a", status: "error" }];
    const { result } = renderHook(() => useRosterEntries([agent("a", "Ada")]));
    act(() => emitChatMessage({ type: "run_active", active: true, agentId: "a", sessionId: "s1" }));
    expect(result.current[0].state).toBe("attention");
  });

  it("returns a referentially stable array when nothing relevant changed", () => {
    // Host re-renders (dialog state, drawer toggles) must not hand the roster
    // a fresh `entries` identity and defeat its memos.
    teamsAgents = [{ id: "a", status: "idle" }];
    const agents = [agent("a", "Ada")];
    const { result, rerender } = renderHook(() => useRosterEntries(agents));
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
