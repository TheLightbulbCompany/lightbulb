// apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
const post = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read, post }) }));

import { RunDetailPanel } from "@/components/teams/panels/RunDetailPanel";

const swrIdle = { data: undefined, isLoading: false, error: null, mutate: vi.fn() };

function mockReads(overrides: Record<string, unknown>) {
  read.mockImplementation((path: string) => {
    for (const [prefix, data] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) {
        return { data, isLoading: false, error: null, mutate: vi.fn() };
      }
    }
    return swrIdle;
  });
}

beforeEach(() => {
  read.mockReset();
  post.mockReset();
});

describe("RunDetailPanel", () => {
  it("renders status, agent, failure block, usage and transcript", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: '{"ts":"t","stream":"stdout","chunk":"doing work"}\n' },
      "/heartbeat-runs/r1/events": { events: [{ seq: 1, eventType: "lifecycle", level: "info", message: "run started" }] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "timed_out",
        startedAt: "2026-07-09T02:09:40Z",
        finishedAt: "2026-07-09T02:11:47Z",
        error: "gateway wait gave up",
        retryExhaustedReason: "max retries reached",
        usageJson: { inputTokens: 1200, outputTokens: 300, provider: "bedrock" },
        logBytes: 64,
      },
      "/agents": { agents: [{ id: "agt_1", name: "Lens" }] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("timed out")).toBeInTheDocument();
    expect(screen.getAllByText("Lens").length).toBeGreaterThan(0);
    expect(screen.getByText("gateway wait gave up")).toBeInTheDocument();
    expect(screen.getByText("max retries reached")).toBeInTheDocument();
    expect(screen.getByText("doing work")).toBeInTheDocument();
    expect(screen.getByText("run started")).toBeInTheDocument();
    expect(screen.getByText(/1,200 in/)).toBeInTheDocument();
  });

  it("falls back to the legacy generic run when the heartbeat-run 404s", () => {
    read.mockImplementation((path: string) => {
      if (path.startsWith("/heartbeat-runs/r1") ) {
        return { data: undefined, isLoading: false, error: new Error("404"), mutate: vi.fn() };
      }
      if (path === "/runs/r1") {
        return { data: { id: "r1", status: "succeeded" }, isLoading: false, error: null, mutate: vi.fn() };
      }
      return swrIdle;
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("shows the truncation notice for oversized logs", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", status: "running", logBytes: 5_000_000 },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/showing the last 256 KiB/i)).toBeInTheDocument();
  });

  it("cancels a running run", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status: "running" },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ id: "r1", status: "cancelled" });
    render(<RunDetailPanel runId="r1" />);
    const btn = screen.getByRole("button", { name: "Cancel run" });
    btn.click();
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/heartbeat-runs/r1/cancel", {}),
    );
  });

  it("retries a timed-out run against its source issue", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "timed_out",
        contextSnapshot: { issueId: "iss_7", taskKey: "ADM-42", other: "ignore" },
      },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({});
    render(<RunDetailPanel runId="r1" />);
    screen.getByRole("button", { name: "Retry" }).click();
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/agents/agt_1/wakeup", {
        source: "on_demand",
        reason: "retry_failed_run",
        payload: { issueId: "iss_7", taskKey: "ADM-42" },
      }),
    );
  });

  it("offers Resume only for process-lost failures", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status: "failed", errorCode: "process_lost" },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByRole("button", { name: "Resume run" })).toBeInTheDocument();
  });
});
