import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueContinuationHandoff } from "@/components/teams/issues/IssueContinuationHandoff";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { Issue, IssueDocument } from "@/components/teams/shared/types";

const HANDOFF: IssueDocument = {
  id: "doc_1",
  key: "continuation-summary",
  title: "Where I left off",
  body: "Deployed **staging**; prod still pending.",
  updatedAt: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  latestRevisionNumber: 3,
};

describe("IssueContinuationHandoff (ported)", () => {
  it("renders the handoff collapsed and expands to its markdown body", async () => {
    const user = userEvent.setup();
    render(<IssueContinuationHandoff document={HANDOFF} />);

    expect(screen.getByText("Where I left off")).toBeInTheDocument();
    expect(screen.getByText("handoff")).toBeInTheDocument();
    expect(screen.getByText("Updated 5m ago - revision 3")).toBeInTheDocument();
    expect(screen.queryByText("staging")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand continuation handoff" }));
    expect(screen.getByText("staging")).toBeInTheDocument();
  });

  it("renders nothing without a handoff document", () => {
    const { container } = render(<IssueContinuationHandoff document={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

// --- mounted in the Teams issue detail --------------------------------------

const DOC_PATH = "/issues/iss_9/documents/continuation-summary";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({
      data: path === DOC_PATH ? HANDOFF : undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
    post: vi.fn(),
    mutate: vi.fn(),
  }),
}));

const ISSUE: Issue = {
  id: "iss_9",
  identifier: "ISO-9",
  title: "Long task",
  status: "in_progress",
  priority: null,
  executionState: {
    monitor: {
      status: "scheduled",
      nextCheckAt: new Date(Date.now() + 60 * 60 * 1000 + 5 * 60 * 1000).toISOString(),
      attemptCount: 1,
    },
  },
  scheduledRetry: {
    runId: "run_9",
    status: "scheduled_retry",
    agentId: "agent_1",
    agentName: null,
    retryOfRunId: null,
    scheduledRetryAt: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
    scheduledRetryAttempt: 1,
    scheduledRetryReason: "transient_failure",
  },
};

vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: () => ({ issue: ISSUE, comments: [], isLoading: false, isError: false, error: null }),
}));

describe("IssueDetailPage status cards", () => {
  it("mounts the monitor banner + composer strip, continuation handoff and scheduled-retry card", () => {
    render(<IssueDetailPage issueId="iss_9" />);
    expect(screen.getByText(/^Waiting on monitor — resumes in 1h/)).toBeInTheDocument();
    expect(screen.getByTestId("issue-monitor-composer-strip")).toBeInTheDocument();
    expect(screen.getByText("Where I left off")).toBeInTheDocument();
    expect(screen.getByTestId("issue-scheduled-retry-card")).toBeInTheDocument();
  });
});
