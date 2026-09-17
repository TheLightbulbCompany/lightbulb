import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OverviewPanel, healthCount } from "../OverviewPanel";

// Both RPCs are held until the runtime is known, so a test has to name one.
let runtimeHost: "ecs" | "agentcore" | undefined = "ecs";
let healthPayload: Record<string, unknown> = {};

vi.mock("@lightbulb/shared", () => ({ useRuntimeHost: () => runtimeHost }));

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
    runtimeHost = "ecs";
    healthPayload = { ok: true, ts: NOW, sessions: { count: 0 }, agents: [] };
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the health snapshot when the container is online", () => {
    render(<OverviewPanel />);
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("System Healthy")).toBeInTheDocument();
  });

  // M6 task 10-E, row 7. The Hermes bridge sends plain counts and `uptimeS`.
  it("reads Hermes's numeric counts and uptime, and drops the rows Hermes has no answer for", () => {
    runtimeHost = "agentcore";
    healthPayload = { ok: true, ready: true, uptimeS: 7_200, agents: 6, sessions: 4 };
    render(<OverviewPanel />);
    expect(screen.getByText("2h 0m")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.queryByText("Tick Interval")).toBeNull();
    expect(screen.queryByText("Last Channels")).toBeNull();
  });

  it("keeps the tick/channels rows on ECS", () => {
    render(<OverviewPanel />);
    expect(screen.getByText("Tick Interval")).toBeInTheDocument();
    expect(screen.getByText("Last Channels")).toBeInTheDocument();
  });
});

describe("healthCount", () => {
  it("accepts both runtimes' shapes and keeps 'unknown' distinct from zero", () => {
    expect(healthCount(4)).toBe(4);                 // Hermes: a plain count
    expect(healthCount({ count: 3 })).toBe(3);      // OpenClaw: sessions
    expect(healthCount([{}, {}])).toBe(2);          // OpenClaw: agents
    expect(healthCount(0)).toBe(0);
    // `null` is the bridge saying "no profile restored yet" — an em dash, not a 0.
    expect(healthCount(null)).toBeUndefined();
    expect(healthCount(undefined)).toBeUndefined();
  });
});
