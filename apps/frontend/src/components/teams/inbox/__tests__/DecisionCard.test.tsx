import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// MarkdownView reaches useTeamsApi (attachment downloads); the card itself never fetches.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: vi.fn(() => ({ data: undefined })), post: vi.fn(), mutate: vi.fn(), download: vi.fn() }),
}));

import { DecisionCard } from "@/components/teams/inbox/DecisionCard";
import type { Decision, DecisionEffectExecution } from "@/components/teams/shared/types";

const ISSUE = "11111111-1111-4111-8111-111111111111";

const decision = (over: Partial<Decision> = {}): Decision => ({
  id: "dec_1",
  companyId: "co_1",
  bundleId: null,
  originAgentId: "ag_1",
  originIssueId: ISSUE,
  originRunId: "run_1",
  ruleKey: null,
  title: "Ship the release?",
  body: "Tests are green.",
  options: [
    {
      id: "ship",
      label: "Ship it",
      effects: [{ type: "update_issue_status", targetIssueId: ISSUE, staleness: "lenient", status: "done" }],
    },
    {
      id: "hold",
      label: "Hold",
      effects: [{ type: "comment_on_issue", targetIssueId: ISSUE, staleness: "lenient", bodyMarkdown: "Holding." }],
    },
  ],
  inputs: null,
  status: "open",
  executionStatus: null,
  chosenOptionId: null,
  inputValues: null,
  decidedByUserId: null,
  decidedAt: null,
  expiresAt: "2026-10-09T00:00:00Z",
  idempotencyKey: null,
  targetSnapshots: {},
  continuationPolicy: "wake_origin_agent",
  metadata: {},
  createdAt: "2026-10-02T00:00:00Z",
  updatedAt: "2026-10-02T00:00:00Z",
  ...over,
});

const resolveIssue = (id: string) =>
  id === ISSUE ? { id, identifier: "ISO-7", title: "Release", href: "/chat?panel=teams-issue-detail&detailId=x" } : null;

describe("DecisionCard", () => {
  it("renders the options with what each will do, and decides with the option id", () => {
    const onDecide = vi.fn();
    render(<DecisionCard decision={decision()} resolveIssue={resolveIssue} originAgentName="Friday" onDecide={onDecide} />);

    expect(screen.getByText("Ship the release?")).toBeInTheDocument();
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("Set ISO-7 to done")).toBeInTheDocument();
    expect(screen.getByText("Comment on ISO-7")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /ship it/i }));
    expect(onDecide).toHaveBeenCalledWith("ship", {});
  });

  it("offers a no-effect dismiss", () => {
    const onDismiss = vi.fn();
    render(<DecisionCard decision={decision()} onDismiss={onDismiss} />);

    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(onDismiss).toHaveBeenCalled();
  });

  it("keeps the options disabled until a required input is filled", () => {
    const onDecide = vi.fn();
    render(
      <DecisionCard
        decision={decision({ inputs: [{ id: "why", label: "Reason", required: true }] })}
        onDecide={onDecide}
      />,
    );

    const ship = screen.getByRole("button", { name: /ship it/i });
    expect(ship).toBeDisabled();
    fireEvent.click(ship);
    expect(onDecide).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Customer is waiting" } });
    expect(ship).toBeEnabled();
    fireEvent.click(ship);
    expect(onDecide).toHaveBeenCalledWith("ship", { why: "Customer is waiting" });
  });

  it("disables an option whose strict target changed since it was proposed", () => {
    const strict = decision({
      options: [
        {
          id: "ship",
          label: "Ship it",
          effects: [{ type: "update_issue_status", targetIssueId: ISSUE, staleness: "strict", status: "done" }],
        },
      ],
    });
    render(<DecisionCard decision={strict} targetChanged={{ [ISSUE]: true }} resolveIssue={resolveIssue} />);

    expect(screen.getByText(/1 target changed since this was proposed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ship it/i })).toBeDisabled();
  });

  it("keeps strict options disabled while staleness is unknown, lenient ones enabled", () => {
    const mixed = decision({
      options: [
        {
          id: "ship",
          label: "Ship it",
          effects: [{ type: "update_issue_status", targetIssueId: ISSUE, staleness: "strict", status: "done" }],
        },
        {
          id: "hold",
          label: "Hold",
          effects: [{ type: "comment_on_issue", targetIssueId: ISSUE, staleness: "lenient", bodyMarkdown: "Holding." }],
        },
      ],
    });
    render(<DecisionCard decision={mixed} stalenessUnknown resolveIssue={resolveIssue} />);

    expect(screen.getByRole("button", { name: /ship it/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /hold/i })).toBeEnabled();
    // Not known to be stale, so it is not labelled as such.
    expect(screen.queryByText(/blocked · stale/i)).not.toBeInTheDocument();
  });

  it("the cancel-tree confirm re-checks staleness that arrived after it opened", () => {
    const onDecide = vi.fn();
    const destructive = decision({
      options: [
        {
          id: "kill",
          label: "Cancel the work",
          style: "destructive",
          effects: [{ type: "cancel_issue_tree", targetIssueId: ISSUE, staleness: "strict", reasonComment: "Dropped." }],
        },
      ],
    });
    const { rerender } = render(
      <DecisionCard decision={destructive} targetChanged={{}} resolveIssue={resolveIssue} onDecide={onDecide} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /cancel the work/i }));
    fireEvent.change(screen.getByLabelText(/type the issue identifier/i), { target: { value: "ISO-7" } });
    const confirm = screen.getByRole("button", { name: /cancel tree/i });
    expect(confirm).toBeEnabled();

    rerender(
      <DecisionCard
        decision={destructive}
        targetChanged={{ [ISSUE]: true }}
        resolveIssue={resolveIssue}
        onDecide={onDecide}
      />,
    );
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("a cancel-tree option asks for the identifier before it decides", () => {
    const onDecide = vi.fn();
    const destructive = decision({
      options: [
        {
          id: "kill",
          label: "Cancel the work",
          style: "destructive",
          effects: [{ type: "cancel_issue_tree", targetIssueId: ISSUE, staleness: "strict", reasonComment: "Dropped." }],
        },
      ],
    });
    render(<DecisionCard decision={destructive} resolveIssue={resolveIssue} onDecide={onDecide} />);

    fireEvent.click(screen.getByRole("button", { name: /cancel the work/i }));
    expect(onDecide).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: /cancel tree/i });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/type the issue identifier/i), { target: { value: "ISO-7" } });
    fireEvent.click(confirm);
    expect(onDecide).toHaveBeenCalledWith("kill", {});
  });

  it("the cancel-tree confirm re-checks a required input cleared after it opened", () => {
    const onDecide = vi.fn();
    const destructive = decision({
      inputs: [{ id: "why", label: "Reason", placeholder: "Why?", required: true }],
      options: [
        {
          id: "kill",
          label: "Cancel the work",
          style: "destructive",
          effects: [{ type: "cancel_issue_tree", targetIssueId: ISSUE, staleness: "strict", reasonComment: "Dropped." }],
        },
      ],
    });
    render(<DecisionCard decision={destructive} resolveIssue={resolveIssue} onDecide={onDecide} />);

    const reason = screen.getByPlaceholderText("Why?");
    fireEvent.change(reason, { target: { value: "No longer needed" } });
    fireEvent.click(screen.getByRole("button", { name: /cancel the work/i }));
    fireEvent.change(screen.getByLabelText(/type the issue identifier/i), { target: { value: "ISO-7" } });
    const confirm = screen.getByRole("button", { name: /cancel tree/i });
    expect(confirm).toBeEnabled();

    fireEvent.change(reason, { target: { value: "" } });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("a decided decision shows what each effect did instead of the options", () => {
    const executions: DecisionEffectExecution[] = [
      {
        id: "ex_1",
        decisionId: "dec_1",
        effectIndex: 0,
        effectType: "update_issue_status",
        targetIssueId: ISSUE,
        status: "executed",
        result: { status: "done" },
        error: null,
        activityLogId: null,
        executedAt: "2026-10-02T01:00:00Z",
      },
      {
        id: "ex_2",
        decisionId: "dec_1",
        effectIndex: 1,
        effectType: "comment_on_issue",
        targetIssueId: ISSUE,
        status: "failed",
        result: null,
        error: "target_changed",
        activityLogId: null,
        executedAt: null,
      },
    ];
    render(
      <DecisionCard
        decision={decision({ status: "decided", executionStatus: "partial", chosenOptionId: "ship" })}
        executions={executions}
        resolveIssue={resolveIssue}
      />,
    );

    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.getByText("Set ISO-7 to done")).toBeInTheDocument();
    expect(screen.getByText(/failed on ISO-7 — the target changed since this was proposed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ship it/i })).not.toBeInTheDocument();
  });
});
