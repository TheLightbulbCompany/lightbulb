import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueBlockedNotice } from "@/components/teams/issues/IssueBlockedNotice";
import type {
  IssueBlockerAttention,
  IssueRelationIssueSummary,
  IssueScheduledRetry,
  SuccessfulRunHandoffState,
} from "@/components/teams/shared/types";

const { postSpy, mutateSpy, readErrors, readSpy } = vi.hoisted(() => ({
  readSpy: vi.fn(),
  postSpy: vi.fn(),
  mutateSpy: vi.fn(),
  readErrors: new Map<string, { status: number }>(),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string, opts?: unknown) => (readSpy(path, opts), { data: undefined, error: readErrors.get(path), isLoading: false }),
    post: postSpy,
    mutate: mutateSpy,
  }),
}));

const blocker = (id: string, extra: Partial<IssueRelationIssueSummary> = {}): IssueRelationIssueSummary => ({
  id: `iss_${id}`,
  identifier: `ISO-${id}`,
  title: `Task ${id}`,
  status: "todo",
  ...extra,
});

const ATTENTION_COVERED: IssueBlockerAttention = {
  state: "covered",
  reason: "active_dependency",
  unresolvedBlockerCount: 2,
  coveredBlockerCount: 2,
  stalledBlockerCount: 0,
  attentionBlockerCount: 0,
  sampleBlockerIdentifier: null,
  sampleStalledBlockerIdentifier: null,
};

const HANDOFF: SuccessfulRunHandoffState = {
  state: "required",
  required: true,
  hasLiveContinuation: false,
  sourceRunId: "run_abcdef123456",
  correctiveRunId: null,
  assigneeAgentId: "agent_1",
  detectedProgressSummary: null,
  createdAt: null,
};

const RETRY: IssueScheduledRetry = {
  runId: "run_2",
  status: "scheduled_retry",
  agentId: "agent_1",
  agentName: "CEO",
  retryOfRunId: null,
  scheduledRetryAt: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
  scheduledRetryAttempt: 1,
  scheduledRetryReason: null,
};

beforeEach(() => {
  readErrors.clear();
  readSpy.mockReset();
  postSpy.mockReset().mockResolvedValue({ outcome: "promoted", message: "Run starting", scheduledRetry: null });
  mutateSpy.mockReset();
});

describe("IssueBlockedNotice (ported)", () => {
  it("amber notice lists unresolved blockers as links", () => {
    render(
      <IssueBlockedNotice issueId="iss_1" issueStatus="blocked" blockers={[blocker("2"), blocker("3")]} />,
    );
    expect(screen.getByTestId("issue-blocked-notice")).toBeInTheDocument();
    expect(screen.getByText(/stays blocked by the linked tasks until they are done/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ISO-2/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=ISO-2",
    );
    expect(screen.getByRole("link", { name: /ISO-3/ })).toBeInTheDocument();
    expect(screen.getByTestId("issue-blocked-notice-reopen-suppressed")).toHaveTextContent(
      "Still blocked by ISO-2 (todo) and 1 other task.",
    );
  });

  it('shows "Ultimately waiting on" for a chain', () => {
    render(
      <IssueBlockedNotice
        issueId="iss_1"
        issueStatus="blocked"
        blockers={[blocker("2", { status: "blocked", terminalBlockers: [blocker("9", { status: "in_progress" })] })]}
      />,
    );
    const row = screen.getByText("Ultimately waiting on").parentElement as HTMLElement;
    expect(within(row).getByRole("link", { name: /ISO-9/ })).toBeInTheDocument();
  });

  it('blue "Waiting on live work" when blockerAttention.state is "covered", with "X of N done"', () => {
    const all = [blocker("2", { status: "done" }), blocker("3", { status: "in_progress" })];
    render(
      <IssueBlockedNotice
        issueId="iss_1"
        issueStatus="blocked"
        blockers={[all[1]]}
        allBlockers={all}
        liveIssueIds={new Set(["iss_3"])}
        blockerAttention={ATTENTION_COVERED}
      />,
    );
    expect(screen.getByTestId("issue-blocked-notice-live")).toBeInTheDocument();
    expect(screen.getByText("Waiting on live work")).toBeInTheDocument();
    expect(screen.getByTestId("issue-blocked-notice-progress")).toHaveTextContent("1 of 2 done · 1 running");
    expect(screen.queryByTestId("issue-blocked-notice")).not.toBeInTheDocument();
  });

  it('handoff shows "This task still needs a next step" and Retry now', async () => {
    const user = userEvent.setup();
    render(
      <IssueBlockedNotice
        issueId="iss_1"
        issueStatus="in_progress"
        blockers={[]}
        successfulRunHandoff={HANDOFF}
        scheduledRetry={RETRY}
        agentName="CEO"
      />,
    );
    expect(screen.getByText("This task still needs a next step.")).toBeInTheDocument();
    expect(screen.getByText("Asked CEO to choose the next step")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "run run_abcd" })).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=run_abcdef123456",
    );
    await user.click(screen.getByTestId("issue-next-step-retry-now"));
    expect(postSpy).toHaveBeenCalledWith("/issues/iss_1/scheduled-retry/retry-now", {});
    expect(await screen.findByText("Promoted")).toBeInTheDocument();
  });

  it("a blocker whose detail 404s renders its identifier unlinked", () => {
    readErrors.set("/issues/ISO-7", { status: 404 });
    render(
      <IssueBlockedNotice issueId="iss_1" issueStatus="blocked" blockers={[blocker("7"), blocker("8")]} />,
    );
    const locked = screen.getByTestId("issue-blocked-notice-unreadable");
    expect(locked).toHaveTextContent("ISO-7");
    expect(locked.closest("a")).toBeNull();
    expect(screen.queryByRole("link", { name: /ISO-7/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ISO-8/ })).toBeInTheDocument();
    // The chip's detail read must not retry a 404 forever.
    expect(readSpy).toHaveBeenCalledWith("/issues/ISO-7", { shouldRetryOnError: false });
  });

  it("renders nothing when status is not blocked and there is no handoff", () => {
    const { container } = render(
      <IssueBlockedNotice issueId="iss_1" issueStatus="in_progress" blockers={[]} successfulRunHandoff={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
