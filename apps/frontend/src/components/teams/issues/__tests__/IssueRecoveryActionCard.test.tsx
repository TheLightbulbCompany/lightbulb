import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueRecoveryActionCard } from "@/components/teams/issues/IssueRecoveryActionCard";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { Issue, IssueRecoveryAction } from "@/components/teams/shared/types";

// A stock board-owned recovery: the source task is "blocked" and waits on a human.
const ACTION: IssueRecoveryAction = {
  id: "22222222-2222-2222-2222-222222222222",
  companyId: "co_abc",
  sourceIssueId: "iss_42",
  recoveryIssueId: null,
  kind: "stranded_assigned_issue",
  status: "active",
  ownerType: "board",
  ownerAgentId: null,
  ownerUserId: null,
  previousOwnerAgentId: "agent_1",
  returnOwnerAgentId: null,
  cause: "stranded_assigned_issue",
  fingerprint: "fp",
  evidence: { summary: "Run finished without a disposition." },
  nextAction: "Choose the next step",
  wakePolicy: { type: "board_escalation" },
  monitorPolicy: null,
  attemptCount: 1,
  maxAttempts: null,
  timeoutAt: null,
  lastAttemptAt: null,
  outcome: null,
  resolutionNote: null,
  resolvedAt: null,
  createdAt: "2026-09-24T00:00:00.000Z",
  updatedAt: "2026-09-24T00:00:00.000Z",
};

describe("IssueRecoveryActionCard (ported)", () => {
  it("renders a boarded recovery with its evidence and a Resolve menu", async () => {
    const user = userEvent.setup();
    const onResolve = vi.fn();
    render(<IssueRecoveryActionCard action={ACTION} onResolve={onResolve} />);

    expect(screen.getByRole("status", { name: "Recovery action: needed" })).toBeInTheDocument();
    expect(screen.getByText("RECOVERY NEEDED")).toBeInTheDocument();
    expect(screen.getByText("Run finished without a disposition.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Resolve recovery" }));
    // Board-only false-positive outcomes stay hidden without canFalsePositive.
    expect(screen.queryByText("False positive, done")).not.toBeInTheDocument();
    await user.click(screen.getByText("Mark task done"));
    expect(onResolve).toHaveBeenCalledWith("done");
  });

  it("offers the board-only outcomes when permitted", async () => {
    const user = userEvent.setup();
    render(<IssueRecoveryActionCard action={ACTION} onResolve={vi.fn()} canFalsePositive />);
    await user.click(screen.getByRole("button", { name: "Resolve recovery" }));
    expect(screen.getByText("False positive, done")).toBeInTheDocument();
  });

  it("stands down for causes that need an explicit execution reconciliation", () => {
    const { container } = render(
      <IssueRecoveryActionCard action={{ ...ACTION, cause: "uncertain_provider_action" }} onResolve={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

// --- mounted in the Teams issue detail: resolve goes through the BFF -------------

const { postSpy, mutateSpy } = vi.hoisted(() => ({ postSpy: vi.fn(), mutateSpy: vi.fn() }));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({
      data: path === "/issues/iss_42/recovery-actions" ? { active: ACTION, actions: [ACTION] } : undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
    post: postSpy,
    mutate: mutateSpy,
  }),
}));

const ISSUE: Issue = { id: "iss_42", identifier: "ISO-42", title: "Stuck task", status: "blocked", priority: null };

vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: () => ({ issue: ISSUE, comments: [], isLoading: false, isError: false, error: null }),
}));

beforeEach(() => {
  postSpy.mockReset().mockResolvedValue({});
});

describe("IssueDetailPage recovery card", () => {
  it("resolves the active recovery action through the scoped BFF route", async () => {
    const user = userEvent.setup();
    render(<IssueDetailPage issueId="iss_42" />);

    await user.click(screen.getByRole("button", { name: "Resolve recovery" }));
    await user.click(screen.getByText("Try again"));

    expect(postSpy).toHaveBeenCalledWith("/issues/iss_42/recovery-actions/resolve", {
      actionId: ACTION.id,
      outcome: "restored",
      sourceIssueStatus: "todo",
    });
  });
});
