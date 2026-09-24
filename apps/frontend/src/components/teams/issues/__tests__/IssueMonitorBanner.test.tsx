import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  IssueMonitorBanner,
  IssueMonitorComposerStrip,
} from "@/components/teams/issues/IssueMonitorBanner";
import type { Issue } from "@/components/teams/shared/types";

const inTwoHours = () => new Date(Date.now() + 2 * 60 * 60 * 1000 + 5 * 60 * 1000).toISOString();

const waiting = (): Issue => ({
  id: "iss_1",
  title: "Watch the deploy",
  status: "in_progress",
  executionState: {
    monitor: { status: "scheduled", nextCheckAt: inTwoHours(), attemptCount: 1, serviceName: "Vercel" },
  },
});

describe("IssueMonitorBanner (ported)", () => {
  it("renders the waiting monitor with its countdown and Check now", async () => {
    const user = userEvent.setup();
    const onCheckNow = vi.fn();
    render(<IssueMonitorBanner issue={waiting()} onCheckNow={onCheckNow} />);

    expect(screen.getByText(/^Waiting on monitor — resumes in 2h/)).toBeInTheDocument();
    expect(screen.getByText(/Attempt 1 · Watching: Vercel/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check now" }));
    expect(onCheckNow).toHaveBeenCalledOnce();
  });

  it("reads a workspace_busy scheduled retry as a workspace wait, without Check now", () => {
    const issue: Issue = {
      id: "iss_1",
      title: "Queued",
      status: "todo",
      scheduledRetry: {
        runId: "run_1",
        status: "scheduled_retry",
        agentId: "a1",
        agentName: null,
        retryOfRunId: null,
        scheduledRetryAt: inTwoHours(),
        scheduledRetryAttempt: 1,
        scheduledRetryReason: "workspace_busy",
      },
    };
    render(<IssueMonitorBanner issue={issue} onCheckNow={vi.fn()} />);
    expect(screen.getByText("Waiting for workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check now" })).not.toBeInTheDocument();
  });

  it("renders nothing without a waiting monitor, or once the task is done", () => {
    const { container, rerender } = render(
      <IssueMonitorBanner issue={{ id: "iss_1", title: "Plain", status: "todo" }} />,
    );
    expect(container).toBeEmptyDOMElement();
    rerender(<IssueMonitorBanner issue={{ ...waiting(), status: "done" }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("the composer strip mirrors the state and says a reply wakes the agent", () => {
    render(<IssueMonitorComposerStrip issue={waiting()} />);
    expect(screen.getByTestId("issue-monitor-composer-strip")).toHaveTextContent(/Resumes in 2h/);
    expect(
      screen.getByText("Sending a reply wakes the agent now — before the scheduled check."),
    ).toBeInTheDocument();
  });
});
