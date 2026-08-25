// @vitest-environment jsdom
//
// Roster freshness repair (the onboarding empty-roster latch). During
// onboarding agents.list settles empty ("No container provisioned." resolves
// to undefined-as-success in the shared RPC fetcher) and nothing ever
// revalidated it — the sidebar showed 0 agents until a manual reload. These
// tests pin the repair: an SWR refreshInterval that keeps the not-ready key
// polling while the roster is empty and self-disables once agents land, plus
// the throttle-safe instant-hydration pushes (status_change HEALTHY,
// onboarding_status_changed, WS reconnect rising edge).
//
// Moved here from apps/frontend when the hook was shared with mobile — mobile
// had five raw agents.list call sites carrying none of this, which is how a
// waking container showed a TestFlight user a failure card.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";

const mutate = vi.fn(async () => undefined);
let rpcData: { agents?: { id: string }[]; defaultId?: string } | undefined;
let lastRpcConfig: { refreshInterval?: unknown } | undefined;
vi.mock("./useGatewayRpc", () => ({
  useGatewayRpc: (
    _gateway: unknown,
    _method: string,
    _params: unknown,
    config: { refreshInterval?: unknown } | undefined,
  ) => {
    lastRpcConfig = config;
    return { data: rpcData, error: undefined, isLoading: false, mutate };
  },
  useGatewayRpcMutation: () => vi.fn(),
  gatewayRpcKey: (method: string) => method,
  pushRevalidate: (_key: string, m: () => unknown) => void m(),
}));

import { useAgents, type AgentsGatewaySource } from "./useAgents";

type EventHandler = (event: string, data: unknown) => void;
const handlers = new Set<EventHandler>();

function gateway(isConnected = true): AgentsGatewaySource {
  return {
    isConnected,
    isParked: false,
    sendReq: vi.fn(),
    onEvent: (h: EventHandler) => {
      handlers.add(h);
      return () => handlers.delete(h);
    },
  } as unknown as AgentsGatewaySource;
}

function emit(event: string, data: unknown): void {
  for (const h of [...handlers]) h(event, data);
}

// This package runs vitest with globals:false, so RTL never registers its
// auto-cleanup — every sibling suite here calls cleanup() explicitly for the
// same reason. Without it each renderHook root stays mounted for the file.
afterEach(() => {
  cleanup();
});

beforeEach(() => {
  mutate.mockClear();
  handlers.clear();
  rpcData = undefined;
  lastRpcConfig = undefined;
});

describe("useAgents roster revalidation", () => {
  it("keeps agents.list polling while the roster is empty and stops once agents arrive", () => {
    renderHook(() => useAgents(gateway()));
    const refreshInterval = lastRpcConfig?.refreshInterval as (
      latest: { agents?: unknown[] } | undefined,
    ) => number;
    // Passed as SWR's functional refreshInterval so SWR re-evaluates it against
    // the latest cached data every cycle: keep polling while empty, stop once
    // agents populate (so steady-state never polls).
    expect(typeof refreshInterval).toBe("function");
    expect(refreshInterval(undefined)).toBeGreaterThan(0);
    expect(refreshInterval({ agents: [] })).toBeGreaterThan(0);
    expect(refreshInterval({ agents: [{ id: "agent-ceo" }] })).toBe(0);
    // Pin the actual cadence, not just "> 0": SWR runs refreshInterval
    // revalidations WITH_DEDUPE, so a period under useGatewayRpc's
    // dedupingInterval (10s) silently drops most of its ticks. A bare
    // `toBeGreaterThan(0)` passed at 3000 while the real cadence was ~10s.
    expect(refreshInterval({ agents: [] })).toBeGreaterThanOrEqual(10_000);
  });

  it("revalidates on a status_change HEALTHY push", () => {
    renderHook(() => useAgents(gateway()));
    mutate.mockClear();
    act(() => emit("status_change", { state: "HEALTHY" }));
    expect(mutate).toHaveBeenCalled();
  });

  it("does not revalidate on a non-HEALTHY status_change", () => {
    renderHook(() => useAgents(gateway()));
    mutate.mockClear();
    act(() => emit("status_change", { state: "GATEWAY_DOWN_DEAD" }));
    expect(mutate).not.toHaveBeenCalled();
  });

  it("revalidates on an onboarding_status_changed push", () => {
    renderHook(() => useAgents(gateway()));
    mutate.mockClear();
    act(() => emit("onboarding_status_changed", {}));
    expect(mutate).toHaveBeenCalled();
  });

  it("revalidates on the WS reconnect rising edge", () => {
    let connected = false;
    const { rerender } = renderHook(() => useAgents(gateway(connected)));
    mutate.mockClear();
    connected = true;
    rerender();
    expect(mutate).toHaveBeenCalled();
  });

  it("reports isSettled only once the RPC has resolved", () => {
    // `isLoading` is false during error-retry cycles and `agents` defaults to
    // [] before the first fetch, so consumers gating "agent no longer exists"
    // need this third state — not either of those alone.
    const first = renderHook(() => useAgents(gateway()));
    expect(first.result.current.isSettled).toBe(false);

    rpcData = { agents: [], defaultId: undefined };
    const second = renderHook(() => useAgents(gateway()));
    expect(second.result.current.isSettled).toBe(true);
    expect(second.result.current.agents).toEqual([]);
  });
});
