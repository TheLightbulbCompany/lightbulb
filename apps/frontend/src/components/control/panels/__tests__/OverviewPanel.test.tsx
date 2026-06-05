import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OverviewPanel } from "../OverviewPanel";

// useGatewayRpc serves health, cron.list, and skills.status keyed by method.
const cronListData = vi.fn();
// Capture the params passed to the cron.list RPC so a test can assert the panel
// requests disabled jobs the same way CronPanel does (includeDisabled:true).
const cronListParams = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string, params?: unknown) => {
    if (method === "health") {
      return {
        data: { ok: true, ts: Date.now(), sessions: { count: 0 }, agents: [] },
        error: undefined,
        isLoading: false,
        mutate: vi.fn(),
      };
    }
    if (method === "cron.list") {
      cronListParams(params);
      return { data: cronListData(), error: undefined, isLoading: false, mutate: vi.fn() };
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

describe("OverviewPanel cron card", () => {
  const NOW = 1_700_000_000_000;
  beforeEach(() => {
    cronListData.mockReset();
    cronListParams.mockClear();
    vi.spyOn(Date, "now").mockReturnValue(NOW);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a failed sub-label when an enabled job is erroring", () => {
    cronListData.mockReturnValue({
      jobs: [
        { id: "j1", name: "J", enabled: true, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: { lastRunStatus: "error", consecutiveErrors: 3 } },
      ],
    });
    render(<OverviewPanel />);
    expect(screen.getByText(/1 failing/i)).toBeInTheDocument();
  });

  it("shows an overdue sub-label when an enabled job's nextRunAtMs is in the past", () => {
    cronListData.mockReturnValue({
      jobs: [
        { id: "j2", name: "J2", enabled: true, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: { nextRunAtMs: NOW - 5000 } },
      ],
    });
    render(<OverviewPanel />);
    expect(screen.getByText(/1 overdue/i)).toBeInTheDocument();
  });

  it("shows no failed/overdue sub-label for healthy cron jobs", () => {
    cronListData.mockReturnValue({
      jobs: [
        { id: "j3", name: "J3", enabled: true, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: { lastRunStatus: "ok", nextRunAtMs: NOW + 5000, consecutiveErrors: 0 } },
      ],
    });
    render(<OverviewPanel />);
    expect(screen.queryByText(/failing/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/overdue/i)).not.toBeInTheDocument();
  });

  it("accepts a bare array cron.list response (older containers)", () => {
    cronListData.mockReturnValue([
      { id: "j4", name: "J4", enabled: true, createdAtMs: 0, updatedAtMs: 0,
        schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
        wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
        state: { lastRunStatus: "error", consecutiveErrors: 1 } },
    ]);
    render(<OverviewPanel />);
    expect(screen.getByText(/1 failing/i)).toBeInTheDocument();
  });

  it("counts disabled jobs in the total (enabled/total) to match CronPanel", () => {
    // CronPanel requests cron.list with includeDisabled:true, so disabled jobs
    // appear in its list. The Overview card must request the same way and count
    // them in the denominator: 1 enabled + 2 disabled -> "1/3", not "1/1".
    cronListData.mockReturnValue({
      jobs: [
        { id: "e1", name: "Enabled", enabled: true, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: { lastRunStatus: "ok", nextRunAtMs: NOW + 5000, consecutiveErrors: 0 } },
        { id: "d1", name: "Disabled1", enabled: false, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: {} },
        { id: "d2", name: "Disabled2", enabled: false, createdAtMs: 0, updatedAtMs: 0,
          schedule: { kind: "every", everyMs: 60000 }, sessionTarget: "main",
          wakeMode: "next-heartbeat", payload: { kind: "agentTurn", message: "x" },
          state: {} },
      ],
    });
    render(<OverviewPanel />);
    expect(screen.getByText("1/3")).toBeInTheDocument();
  });

  it("requests cron.list with includeDisabled:true so totals match CronPanel", () => {
    cronListData.mockReturnValue({ jobs: [] });
    render(<OverviewPanel />);
    expect(cronListParams).toHaveBeenCalledWith({ includeDisabled: true });
  });
});
