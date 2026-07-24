// @vitest-environment jsdom
//
// Roster freshness repair (the onboarding empty-roster latch). During
// onboarding agents.list settles empty ("No container provisioned." resolves
// to undefined-as-success in the shared RPC fetcher) and nothing ever
// revalidated it — the sidebar showed 0 agents until a manual reload. These
// tests pin the repair triggers: status_change HEALTHY push,
// onboarding_status_changed push, browser-WS reconnect rising edge, and the
// raw-interval empty-roster backstop (which self-stops once agents arrive).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";

const mutate = vi.fn(async () => undefined);
let rpcData: { agents?: { id: string }[]; defaultId?: string } | undefined = undefined;
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: rpcData, error: undefined, isLoading: false, mutate }),
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
  gatewayState = { isConnected: true, isParked: false };
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useAgents roster revalidation", () => {
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

  it("empty-roster backstop re-asks on an interval and stops once agents arrive", () => {
    vi.useFakeTimers();
    const { rerender } = render(<Probe />);
    mutate.mockClear();

    act(() => {
      vi.advanceTimersByTime(10_500);
    });
    expect(mutate).toHaveBeenCalled();

    // Roster arrives — the backstop interval must be torn down.
    rpcData = { agents: [{ id: "agent-ceo" }], defaultId: "agent-ceo" };
    rerender(<Probe />);
    mutate.mockClear();
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(mutate).not.toHaveBeenCalled();
  });

  it("backstop stays quiet while parked", () => {
    vi.useFakeTimers();
    gatewayState = { isConnected: false, isParked: true };
    render(<Probe />);
    mutate.mockClear();
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(mutate).not.toHaveBeenCalled();
  });
});
