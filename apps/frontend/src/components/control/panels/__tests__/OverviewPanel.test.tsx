import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OverviewPanel } from "../OverviewPanel";

// useGatewayRpc serves health and skills.status keyed by method.
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string) => {
    if (method === "health") {
      return {
        data: { ok: true, ts: Date.now(), sessions: { count: 0 }, agents: [] },
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
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the health snapshot when the container is online", () => {
    render(<OverviewPanel />);
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("System Healthy")).toBeInTheDocument();
  });
});
