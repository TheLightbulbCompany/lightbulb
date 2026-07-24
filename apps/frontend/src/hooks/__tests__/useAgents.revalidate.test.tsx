// @vitest-environment jsdom
//
// Roster freshness repair (the onboarding empty-roster latch). During
// onboarding agents.list settles empty ("No container provisioned." resolves
// to undefined-as-success in the shared RPC fetcher) and nothing ever
// revalidated it — the sidebar showed 0 agents until a manual reload. These
// tests pin the repair: an SWR refreshInterval that keeps the not-ready key
// polling while the roster is empty and self-disables once agents land, plus
// the throttle-safe instant-hydration pushes (status_change HEALTHY,
// onboarding_status_changed, browser-WS reconnect rising edge).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";

const mutate = vi.fn(async () => undefined);
let rpcData: { agents?: { id: string }[]; defaultId?: string } | undefined = undefined;
let lastRpcConfig: { refreshInterval?: unknown } | undefined;
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (
    _method: string,
    _params: unknown,
    config: { refreshInterval?: unknown } | undefined,
  ) => {
    lastRpcConfig = config;
    return { data: rpcData, error: undefined, isLoading: false, mutate };
  },
  useGatewayRpcMutation: () => vi.fn(),
}));

type EventHandler = (event: string, data: unknown) => void;
const handlers = new Set<EventHandler>();
let gatewayState = { isConnected: true, isParked: false };
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    ...gatewayState,
    onEvent: (h: EventHandler) => {
      handlers.add(h);
      return () => handlers.delete(h);
    },
  }),
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { useAgents } from "@/hooks/useAgents";

function Probe(): null {
  useAgents();
  return null;
}

function emit(event: string, data: unknown): void {
  for (const h of [...handlers]) h(event, data);
}

beforeEach(() => {
  mutate.mockClear();
  handlers.clear();
  rpcData = undefined;
  lastRpcConfig = undefined;
  gatewayState = { isConnected: true, isParked: false };
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useAgents roster revalidation", () => {
  it("keeps agents.list polling while the roster is empty and stops once agents arrive", () => {
    render(<Probe />);
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
  });

  it("revalidates on a status_change HEALTHY push", () => {
    render(<Probe />);
    mutate.mockClear();
    act(() => emit("status_change", { state: "HEALTHY" }));
    expect(mutate).toHaveBeenCalled();
  });

  it("does not revalidate on a non-HEALTHY status_change", () => {
    render(<Probe />);
    mutate.mockClear();
    act(() => emit("status_change", { state: "GATEWAY_DOWN_DEAD" }));
    expect(mutate).not.toHaveBeenCalled();
  });

  it("revalidates on an onboarding_status_changed push", () => {
    render(<Probe />);
    mutate.mockClear();
    act(() => emit("onboarding_status_changed", {}));
    expect(mutate).toHaveBeenCalled();
  });

  it("revalidates on the browser-WS reconnect rising edge", () => {
    gatewayState = { isConnected: false, isParked: false };
    const { rerender } = render(<Probe />);
    mutate.mockClear();
    gatewayState = { isConnected: true, isParked: false };
    rerender(<Probe />);
    expect(mutate).toHaveBeenCalled();
  });
});
