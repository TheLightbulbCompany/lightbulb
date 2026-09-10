import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { LiveEvent } from "@lightbulb/shared";

interface RpcResult<T> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

const rpc = <T,>(over: Partial<RpcResult<T>>): RpcResult<T> => ({
  data: undefined,
  error: undefined,
  isLoading: false,
  mutate: async () => undefined,
  ...over,
});

type EventHandler = (event: LiveEvent) => void;
const eventHandlers: Set<EventHandler> = new Set();

function emit(type: LiveEvent["type"], payload: unknown) {
  const event = {
    schemaVersion: 1,
    eventId: `event-${type}`,
    type,
    occurredAt: "2026-09-09T00:00:00.000Z",
    payload,
  } as LiveEvent;
  for (const handler of eventHandlers) handler(event);
}

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: EventHandler) => {
      eventHandlers.add(handler);
      return () => eventHandlers.delete(handler);
    },
  }),
}));

const mockRpc = vi.fn();
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null, params?: Record<string, unknown>) =>
    mockRpc(method, params) as RpcResult<unknown>,
}));

import { useProgressCard } from "@/hooks/useProgressCard";

const CARD = {
  sessionKey: "agent:agent_a:user_1",
  revision: 3,
  updatedAt: 1000,
  steps: [
    { step: "Research", status: "completed" as const },
    { step: "Draft", status: "in_progress" as const },
  ],
};

beforeEach(() => {
  eventHandlers.clear();
  mockRpc.mockReset();
  mockRpc.mockReturnValue(rpc({ data: { card: null } }));
});

describe("useProgressCard", () => {
  it("fetches progressCard.get for the active session on open", () => {
    mockRpc.mockReturnValue(rpc({ data: { card: CARD } }));
    const { result } = renderHook(() => useProgressCard("agent:agent_a:user_1"));
    expect(mockRpc).toHaveBeenCalledWith("progressCard.get", {
      sessionKey: "agent:agent_a:user_1",
    });
    expect(result.current.card).toEqual(CARD);
  });

  it("does not fetch when there is no active session", () => {
    renderHook(() => useProgressCard(null));
    expect(mockRpc).toHaveBeenCalledWith(null, undefined);
  });

  it("returns null card when the RPC resolves null", () => {
    const { result } = renderHook(() => useProgressCard("agent:agent_a:user_1"));
    expect(result.current.card).toBeNull();
  });

  it("revalidates on a matching runtime.progress.changed event", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("runtime.progress.changed", { sessionKey: "agent:agent_a:user_1", revision: 4 }));

    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("revalidates when the event key differs only in case — the real prod shape", () => {
    // OpenClaw lowercases the whole session key, so the container's
    // runtime.progress.changed carries `agent:<id>:user_3h3v…` while ours holds
    // the mixed-case Clerk id `user_3H3v…`. Case-sensitive matching meant the
    // card never updated live on prod (progressCard.get itself accepts either
    // casing). Same rule as the backend's _user_owns_session_key.
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_3H3vAbC"));

    act(() => emit("runtime.progress.changed", { sessionKey: "agent:agent_a:user_3h3vabc", revision: 4 }));

    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("ignores a runtime.progress.changed event for a different sessionKey", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("runtime.progress.changed", { sessionKey: "agent:agent_b:user_1", revision: 1 }));

    expect(mutate).not.toHaveBeenCalled();
  });

  it("ignores unrelated gateway events", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("runtime.resource.invalidated", { resource: "agents" }));

    expect(mutate).not.toHaveBeenCalled();
  });
});
