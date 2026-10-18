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

  // --- @ v2026.1012.0-isol8.4: the native_run lane ---------------------------

  const NATIVE: IssueRecoveryAction = {
    ...ACTION,
    kind: "active_run_watchdog",
    ownerType: "agent",
    ownerAgentId: "agent_1",
    cause: "native_finalization_invalid",
    nextAction: "Finish the existing run.",
  };

  it.each(["running", "scheduled", "missed"] as const)(
    "native_run lane headline: %s finalization names the existing run",
    (state) => {
      const { container } = render(
        <IssueRecoveryActionCard
          action={{
            ...NATIVE,
            wakePolicy: {
              kind: "resume_native_run",
              runId: "native-run",
              notBefore: state === "scheduled" ? "2099-01-01T00:00:00Z" : "2020-01-01T00:00:00Z",
            },
            nativeRunActivity:
              state === "running"
                ? { runId: "native-run", status: "running", workspaceOperationId: "export-operation" }
                : null,
          }}
        />,
      );
      expect(container.querySelector("section")?.getAttribute("data-recovery-state")).toBe(
        state === "missed" ? "needed" : "in_progress",
      );
      expect(container.textContent).toContain("existing run");
      expect(container.textContent).not.toContain("retrying itself");
      expect(container.textContent).not.toContain("stopped to wait");
      expect(
        container.querySelector('[data-testid="recovery-retry-progress"]')?.getAttribute("data-recovery-lane"),
      ).toBe("native_run");
    },
  );

  it("native_run lane headline: live progress wins over an escalated board retry", () => {
    const { container } = render(
      <IssueRecoveryActionCard
        action={{
          ...NATIVE,
          ownerType: "board",
          status: "escalated",
          attemptCount: 3,
          wakePolicy: { kind: "resume_native_run", runId: "native-run" },
          nativeRunActivity: { runId: "native-run", status: "running", workspaceOperationId: "export-operation" },
        }}
      />,
    );
    expect(container.querySelector("section")?.getAttribute("data-recovery-state")).toBe("in_progress");
    expect(container.textContent).toContain("Paperclip is recovering the existing run");
    expect(container.querySelector('[data-testid="recovery-recovery-owner"]')?.textContent).toContain("Paperclip");
    expect(container.textContent).not.toContain("Automatic retries are finished");
  });

  it("workspace-export repair causes hide the matching resolve options", async () => {
    const user = userEvent.setup();
    render(
      <IssueRecoveryActionCard
        action={{ ...NATIVE, ownerType: "board", cause: "native_workspace_sync_out_retry_exhausted" }}
        onResolve={vi.fn()}
        canFalsePositive
      />,
    );
    await user.click(screen.getByRole("button", { name: "Resolve recovery" }));
    expect(screen.queryByText("Try again")).not.toBeInTheDocument();
    expect(screen.queryByText("Mark task done")).not.toBeInTheDocument();
    expect(screen.queryByText("Send for review")).not.toBeInTheDocument();
    expect(screen.getByText("False positive, done")).toBeInTheDocument();
  });

  it("workspace-export repair causes drop the Resolve menu when no option is left", () => {
    render(
      <IssueRecoveryActionCard
        action={{ ...NATIVE, ownerType: "board", cause: "native_workspace_sync_out_retry_exhausted" }}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Resolve recovery" })).not.toBeInTheDocument();
  });

  it.each(["active", "escalated", "resolved"] as const)(
    "does not show a historical unsafe-export repair card: %s",
    (status) => {
      const { container } = render(
        <IssueRecoveryActionCard
          action={{ ...NATIVE, status, ownerType: "board", cause: "native_workspace_sync_out_unsafe_archive" }}
          onResolve={vi.fn()}
        />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );
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
