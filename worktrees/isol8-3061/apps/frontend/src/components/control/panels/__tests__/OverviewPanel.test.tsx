import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OverviewPanel, healthCount } from "../OverviewPanel";

// Both RPCs are held until the runtime is known, so a test has to name one.
let statusLoaded: boolean = true;
let healthPayload: Record<string, unknown> = {};

vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => statusLoaded }));

// useRuntimeRpc serves health and skills.status keyed by method.
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    if (method === "health") {
      return {
        data: healthPayload,
        error: undefined,
        isLoading: false,
        mutate: vi.fn(),
      };
    }
    return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
  },
}));

vi.mock("@/hooks/useSystemHealth", () => ({
  useSystemHealth: () => ({
    state: "HEALTHY",
    reason: "ok",
    canRecover: false,
    actionLabel: null,
    recover: vi.fn(),
    isRecovering: false,
  }),
}));

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({
    container: { status: "running", service_name: "svc", region: "us-east-1", created_at: Date.now() },
    isLoading: false,
    error: undefined,
  }),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { subscription_status: "active" } }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ servers: {} }) }),
}));
vi.mock("swr", () => ({
  default: () => ({ data: undefined, error: undefined, isLoading: false }),
}));

describe("OverviewPanel", () => {
  const NOW = 1_700_000_000_000;
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    statusLoaded = true;
    healthPayload = { ready: true, uptimeS: 60, agents: 0 };
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the health snapshot when the container is online", () => {
    render(<OverviewPanel />);
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("System Healthy")).toBeInTheDocument();
  });

  // The Hermes bridge sends a plain agent count and `uptimeS`, and its payload
  // has no `ok` key at all (`apps/infra/hermes/bridge/server.py::_health`) —
  // only `ready`. A fixture that adds `ok: true` back in would hide a
  // regression of H1.
  it("reads Hermes's numeric count and uptime, and drops the rows it has no answer for", () => {
    healthPayload = { ready: true, uptimeS: 7_200, agents: 6 };
    render(<OverviewPanel />);
    expect(screen.getByText("2h 0m")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
    expect(screen.queryByText("Tick Interval")).toBeNull();
    expect(screen.queryByText("Last Channels")).toBeNull();
  });

  // H1: the bridge's health payload is `{ready, ...}` with no `ok` key. The Snapshot card must
  // still read the workspace as online.
  it("renders online from a Hermes-shaped `{ready: true}` payload with no `ok` key", () => {
    healthPayload = { ready: true, uptimeS: 60, agents: 6 };
    render(<OverviewPanel />);
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.queryByText("Offline")).toBeNull();
  });

  // Ruling M6-R54: the Sessions panel is gone, so its summary card — a count
  // the runtime does not send, deep-linking to a panel that is not there —
  // goes with it. Same for the ECS-only Service row.
  it.each(["Sessions", "Service", "Tick Interval", "Last Channels"])(
    "drops the %s row",
    (label) => {
      healthPayload = { ready: true, uptimeS: 60, agents: 6 };
      render(<OverviewPanel />);
      expect(screen.queryByText(label)).toBeNull();
    },
  );
});

describe("healthCount", () => {
  it("accepts both runtimes' shapes and keeps 'unknown' distinct from zero", () => {
    expect(healthCount(4)).toBe(4);                 // Hermes: a plain agent count
    expect(healthCount({ count: 3 })).toBe(3);      // a {count} object
    expect(healthCount([{}, {}])).toBe(2);          // an agents array
    expect(healthCount(0)).toBe(0);
    expect(healthCount(null)).toBeUndefined();
    expect(healthCount(undefined)).toBeUndefined();
  });
});
