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

  const NOTICE = "[hermes-gateway] event stream HTTP 404; falling back to polling";

  function runWith(status: string, stderrExcerpt: string) {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status, stderrExcerpt },
      "/agents": { agents: [] },
    });
  }

  it("shows no error box for a succeeded run whose stderr is only adapter notices", () => {
    runWith("succeeded", NOTICE);
    const { container } = render(<RunDetailPanel runId="r1" />);
    expect(screen.queryByText(/falling back to polling/)).toBeNull();
    expect(container.querySelector('section[style*="var(--err)"]')).toBeNull();
  });

  it("shows other stderr of a succeeded run as a neutral log, not an error", () => {
    runWith("succeeded", `${NOTICE}\nwarn: slow tool`);
    const { container } = render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("Log")).toBeInTheDocument();
    expect(container.querySelector('section[style*="var(--err)"]')).toBeNull();
  });

  it("keeps a gateway failure line in the error box of a failed run", () => {
    runWith("failed", "[hermes-gateway] status poll failed: 503");
    const { container } = render(<RunDetailPanel runId="r1" />);
    expect(container.querySelector('section[style*="var(--err)"]')).not.toBeNull();
    expect(screen.getByText("[hermes-gateway] status poll failed: 503")).toBeInTheDocument();
  });

  it("still shows real stderr of a failed run as an error", () => {
    runWith("failed", `${NOTICE}\nTraceback: boom`);
    const { container } = render(<RunDetailPanel runId="r1" />);
    expect(container.querySelector('section[style*="var(--err)"]')).not.toBeNull();
    expect(screen.getByText(/Traceback: boom/)).toBeInTheDocument();
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

  it("shows the events truncation notice when the page is full", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": {
        events: Array.from({ length: 1000 }, (_, i) => ({ seq: i + 1, eventType: "lifecycle", message: `e${i}` })),
      },
      "/heartbeat-runs/r1": { id: "r1", status: "running" },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText("Showing the first 1,000 events.")).toBeInTheDocument();
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

  it("resumes with commentId fallback when wakeCommentId is empty", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "failed",
        errorCode: "process_lost",
        contextSnapshot: { wakeCommentId: "", commentId: "cmt_9", issueId: "iss_7" },
      },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ id: "r2", status: "queued" });
    render(<RunDetailPanel runId="r1" />);
    screen.getByRole("button", { name: "Resume run" }).click();
    await vi.waitFor(() =>
      expect(post).toHaveBeenCalledWith("/agents/agt_1/wakeup", {
        source: "on_demand",
        reason: "resume_process_lost_run",
        payload: { resumeFromRunId: "r1", issueId: "iss_7", commentId: "cmt_9" },
      }),
    );
  });

  it("surfaces a skipped wake instead of treating it as success", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", agentId: "agt_1", status: "failed", errorCode: "process_lost" },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ message: "Agent is already running" });
    render(<RunDetailPanel runId="r1" />);
    screen.getByRole("button", { name: "Resume run" }).click();
    expect(await screen.findByText("Agent is already running")).toBeInTheDocument();
  });

  it("shows the silence watchdog banner and records a snooze", async () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": {
        id: "r1",
        agentId: "agt_1",
        status: "running",
        outputSilence: { level: "critical", silenceAgeMs: 4 * 3600 * 1000 },
      },
      "/agents": { agents: [] },
    });
    post.mockResolvedValue({ id: "wd_1", decision: "snooze" });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/silent for/i)).toBeInTheDocument();
    screen.getByRole("button", { name: "Snooze 1h" }).click();
    await vi.waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/heartbeat-runs/r1/watchdog-decisions",
        expect.objectContaining({ decision: "snooze", snoozedUntil: expect.any(String) }),
      );
    });
  });

  it("hides the watchdog banner when silence level is ok", () => {
    mockReads({
      "/heartbeat-runs/r1/log": { content: "" },
      "/heartbeat-runs/r1/events": { events: [] },
      "/heartbeat-runs/r1": { id: "r1", status: "running", outputSilence: { level: "ok" } },
      "/agents": { agents: [] },
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.queryByText(/silent for/i)).not.toBeInTheDocument();
  });

  // Task 7: run status rows. RunDetailPanel already models the canonical
  // active-vs-terminal vocabulary (isActiveStatus); these pin the LiveRunDot
  // reuse against it rather than a re-derived spinner.
  it.each(["running", "queued", "scheduled_retry"])(
    "shows a live dot next to a %s run's status",
    (status) => {
      mockReads({
        "/heartbeat-runs/r1/log": { content: "" },
        "/heartbeat-runs/r1/events": { events: [] },
        "/heartbeat-runs/r1": { id: "r1", status },
        "/agents": { agents: [] },
      });
      render(<RunDetailPanel runId="r1" />);
      expect(screen.getByTestId("run-detail-live-dot")).toBeInTheDocument();
    },
  );

  it.each(["succeeded", "failed", "timed_out", "cancelled"])(
    "shows no live dot once a run has reached a terminal status (%s)",
    (status) => {
      mockReads({
        "/heartbeat-runs/r1/log": { content: "" },
        "/heartbeat-runs/r1/events": { events: [] },
        "/heartbeat-runs/r1": { id: "r1", status },
        "/agents": { agents: [] },
      });
      render(<RunDetailPanel runId="r1" />);
      expect(screen.queryByTestId("run-detail-live-dot")).not.toBeInTheDocument();
    },
  );
});
