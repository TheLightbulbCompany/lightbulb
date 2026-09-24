import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueScheduledRetryCard } from "@/components/teams/issues/IssueScheduledRetryCard";
import type { IssueScheduledRetry } from "@/components/teams/shared/types";

const { postSpy, mutateSpy } = vi.hoisted(() => ({ postSpy: vi.fn(), mutateSpy: vi.fn() }));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: postSpy, mutate: mutateSpy }),
}));

const RETRY: IssueScheduledRetry = {
  runId: "run_2",
  status: "scheduled_retry",
  agentId: "agent_1",
  agentName: "CEO",
  retryOfRunId: "abcdef1234567890",
  scheduledRetryAt: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
  scheduledRetryAttempt: 2,
  scheduledRetryReason: "transient_failure",
  error: "upstream timeout",
};

beforeEach(() => {
  postSpy.mockReset().mockResolvedValue({ outcome: "promoted", message: "Run starting", scheduledRetry: null });
  mutateSpy.mockReset();
});

describe("IssueScheduledRetryCard (ported)", () => {
  it("renders the parked retry and promotes it through the scoped BFF route", async () => {
    const user = userEvent.setup();
    render(<IssueScheduledRetryCard issueId="iss_7" scheduledRetry={RETRY} />);

    expect(screen.getByText("Retry scheduled")).toBeInTheDocument();
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    expect(screen.getByText("Transient failure")).toBeInTheDocument();
    expect(screen.getByText(/^Automatic retry in (9|10)m$/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "abcdef12" })).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=abcdef1234567890",
    );
    expect(screen.getByText(/Last attempt failed: upstream timeout/)).toBeInTheDocument();

    await user.click(screen.getByTestId("issue-scheduled-retry-card-retry-now"));
    expect(postSpy).toHaveBeenCalledWith("/issues/iss_7/scheduled-retry/retry-now", {});
    expect(await screen.findByText("Promoted — run starting")).toBeInTheDocument();
  });

  it("labels a max-turn continuation as a continuation", () => {
    render(
      <IssueScheduledRetryCard
        issueId="iss_7"
        scheduledRetry={{ ...RETRY, scheduledRetryReason: "max_turns_continuation" }}
      />,
    );
    expect(screen.getByText("Continuation scheduled")).toBeInTheDocument();
  });

  it("shows the error band when the gate suppresses the retry", async () => {
    const user = userEvent.setup();
    postSpy.mockResolvedValue({ outcome: "gate_suppressed", message: "Budget paused", scheduledRetry: null });
    render(<IssueScheduledRetryCard issueId="iss_7" scheduledRetry={RETRY} />);
    await user.click(screen.getByTestId("issue-scheduled-retry-card-retry-now"));
    expect(await screen.findByTestId("issue-scheduled-retry-error-band")).toHaveTextContent("Budget paused");
  });

  it("renders nothing once the retry is no longer parked", () => {
    const { container, rerender } = render(<IssueScheduledRetryCard issueId="iss_7" scheduledRetry={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<IssueScheduledRetryCard issueId="iss_7" scheduledRetry={{ ...RETRY, status: "queued" }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
