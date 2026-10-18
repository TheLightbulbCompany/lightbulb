import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { UseIssueMutationsResult } from "@/components/teams/issues/hooks/useIssueMutations";
import type { UseIssueDetailResult } from "@/components/teams/issues/hooks/useIssueDetail";
import type { Issue, IssueRecoveryAction } from "@/components/teams/shared/types";

const { state } = vi.hoisted(() => ({
  state: { issue: null as unknown as Issue, active: null as IssueRecoveryAction | null },
}));

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: (): UseIssueMutationsResult => ({
    create: vi.fn(),
    createChild: vi.fn(),
    updateInList: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    addComment: vi.fn(),
    monitorCheckNow: vi.fn(),
  }),
}));

vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: (): UseIssueDetailResult => ({
    issue: state.issue,
    comments: [],
    isLoading: false,
    isError: false,
    error: null,
  }),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({
      data: path.endsWith("/recovery-actions")
        ? { active: state.active, actions: state.active ? [state.active] : [] }
        : undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    mutate: vi.fn(),
  }),
}));

const HANDOFF_ISSUE: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Ship the report",
  status: "in_progress",
  priority: null,
  successfulRunHandoff: {
    state: "required",
    required: true,
    hasLiveContinuation: false,
    sourceRunId: "run_1",
    correctiveRunId: null,
    assigneeAgentId: null,
    detectedProgressSummary: null,
    createdAt: null,
  },
};

beforeEach(() => {
  state.issue = HANDOFF_ISSUE;
  state.active = null;
});

describe("IssueDetailPage blocked notice", () => {
  it("shows the blocked notice with the task's blockers above the description", () => {
    state.issue = {
      ...HANDOFF_ISSUE,
      status: "blocked",
      successfulRunHandoff: null,
      blockedBy: [
        { id: "iss_7", identifier: "ISO-7", title: "Get the data", status: "todo" },
        { id: "iss_8", identifier: "ISO-8", title: "Done already", status: "done" },
      ],
    };
    render(<IssueDetailPage issueId="iss_42" />);
    const notice = screen.getByTestId("issue-blocked-notice");
    expect(within(notice).getByRole("link", { name: /ISO-7/ })).toBeInTheDocument();
    expect(within(notice).queryByRole("link", { name: /ISO-8/ })).not.toBeInTheDocument();
  });

  it("shows the handoff alarm when no recovery action is active", () => {
    render(<IssueDetailPage issueId="iss_42" />);
    expect(screen.getByText("This task still needs a next step.")).toBeInTheDocument();
  });

  it("handoff hidden when a recovery action is active", () => {
    state.active = {
      id: "ra_1",
      kind: "missing_disposition",
      status: "active",
      ownerType: "agent",
      cause: "missing_disposition",
      nextAction: "Choose a next step.",
      evidence: {},
      wakePolicy: null,
    } as unknown as IssueRecoveryAction;
    render(<IssueDetailPage issueId="iss_42" />);
    expect(screen.queryByText("This task still needs a next step.")).not.toBeInTheDocument();
  });
});
