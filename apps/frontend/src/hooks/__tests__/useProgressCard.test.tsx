import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

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

type EventHandler = (event: string, data: unknown) => void;
const eventHandlers: Set<EventHandler> = new Set();

function emit(event: string, payload: unknown) {
  for (const h of eventHandlers) h(event, payload);
}

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    onEvent: (h: EventHandler) => {
      eventHandlers.add(h);
      return () => eventHandlers.delete(h);
    },
  }),
}));

const mockRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null, params?: Record<string, unknown>) =>
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

  it("revalidates on a matching progressCard.changed event", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("progressCard.changed", { sessionKey: "agent:agent_a:user_1", revision: 4 }));

    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("revalidates when the event key differs only in case — the real prod shape", () => {
    // OpenClaw lowercases the whole session key, so the container's
    // progressCard.changed carries `agent:<id>:user_3h3v…` while ours holds
    // the mixed-case Clerk id `user_3H3v…`. Case-sensitive matching meant the
    // card never updated live on prod (progressCard.get itself accepts either
    // casing). Same rule as the backend's _user_owns_session_key.
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_3H3vAbC"));

    act(() => emit("progressCard.changed", { sessionKey: "agent:agent_a:user_3h3vabc", revision: 4 }));

    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("ignores a progressCard.changed event for a different sessionKey", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("progressCard.changed", { sessionKey: "agent:agent_b:user_1", revision: 1 }));

    expect(mutate).not.toHaveBeenCalled();
  });

  it("ignores unrelated gateway events", () => {
    const mutate = vi.fn(async () => undefined);
    mockRpc.mockReturnValue(rpc({ data: { card: CARD }, mutate }));
    renderHook(() => useProgressCard("agent:agent_a:user_1"));

    act(() => emit("status_change", { sessionKey: "agent:agent_a:user_1" }));

    expect(mutate).not.toHaveBeenCalled();
  });
});
