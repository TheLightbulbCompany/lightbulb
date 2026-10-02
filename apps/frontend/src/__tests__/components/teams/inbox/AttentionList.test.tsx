// apps/frontend/src/__tests__/components/teams/inbox/AttentionList.test.tsx
//
// The unified "needs you" list. What these pin down is the behaviour the
// surface exists for: both entity types render in ONE list, each resolves
// through the SAME mutation hooks the in-thread surfaces use, and an empty or
// failed feed is reported honestly rather than as good news.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const read = vi.fn();
const post = vi.fn().mockResolvedValue({});
const mutate = vi.fn();
// Revalidation (useAttentionFeed's refresh + useInteractionMutations) goes
// through useTeamsApi's scoped mutate, not useSWRConfig's.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read, post, patch: vi.fn(), del: vi.fn(), mutate }),
}));

import { AttentionList } from "@/components/teams/inbox/AttentionList";

function installFeed(feed: unknown, opts: { isLoading?: boolean; error?: Error } = {}) {
  read.mockImplementation((path: string) => {
    if (path === "/attention") {
      return {
        data: feed,
        isLoading: opts.isLoading ?? false,
        error: opts.error ?? null,
        mutate: vi.fn(),
      };
    }
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

const approvalItem = {
  id: "att_1",
  sourceKind: "approval",
  subject: { kind: "approval", id: "ap_1", title: "Hire", identifier: null, status: "pending", href: null },
  whyNow: "Approval requested.",
  severity: "medium",
  activityAt: new Date().toISOString(),
  createdAt: new Date().toISOString(),
  relatedIssue: null,
  originAgentName: "Friday",
  approval: { id: "ap_1", type: "hire_agent", status: "pending", payload: {}, createdAt: new Date().toISOString() },
};

const interactionItem = {
  id: "att_2",
  sourceKind: "issue_thread_interaction",
  subject: { kind: "interaction", id: "int_1", title: "Confirm", identifier: null, status: "pending", href: null },
  whyNow: "Confirmation requested on an issue thread.",
  severity: "medium",
  activityAt: new Date().toISOString(),
  createdAt: new Date().toISOString(),
  relatedIssue: { kind: "issue", id: "iss_1", title: "Ship", identifier: "ISO-1", status: "todo", href: null },
  originAgentName: "Atlas",
  interaction: {
    id: "int_1",
    companyId: "co_1",
    issueId: "iss_1",
    kind: "request_confirmation",
    status: "pending",
    continuationPolicy: "resume",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    createdByAgentId: "ag_1",
    payload: { prompt: "Ship it?" },
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  post.mockResolvedValue({});
});

describe("AttentionList", () => {
  test("renders both entity types in one list", () => {
    installFeed({ items: [approvalItem, interactionItem] });
    render(<AttentionList />);

    // The approval keeps its own row component (approve/reject verbs)...
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    // ...and the interaction keeps its card (its own prompt + verbs).
    expect(screen.getByText(/ship it\?/i)).toBeInTheDocument();
    expect(document.querySelectorAll("[data-attention-kind]")).toHaveLength(2);
  });

  test("labels each row with its kind and who is waiting", () => {
    installFeed({ items: [approvalItem, interactionItem] });
    render(<AttentionList />);
    expect(screen.getByText("Approval")).toBeInTheDocument();
    expect(screen.getByText("Question")).toBeInTheDocument();
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("ISO-1")).toBeInTheDocument();
  });

  test("a decision waiting over a day is styled as overdue", () => {
    const old = new Date(Date.now() - 38 * 60 * 60 * 1000).toISOString();
    installFeed({ items: [{ ...approvalItem, createdAt: old }] });
    render(<AttentionList />);
    // The 38-hour approval that motivated this surface: the wait is the
    // pressure cue, so past a day it stops reading as ordinary metadata.
    const wait = screen.getByText(/waiting 1d ago/i);
    expect(wait.getAttribute("style") ?? "").toMatch(/--err/);
  });

  test("resolving an interaction goes through the shared mutation route", async () => {
    installFeed({ items: [interactionItem] });
    render(<AttentionList />);

    fireEvent.click(screen.getByRole("button", { name: /^confirm$|^accept$|^yes$/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/issues/iss_1/interactions/int_1/accept",
        expect.anything(),
      );
    });
    // ...and the answered item is refetched out of this list.
    expect(mutate).toHaveBeenCalledWith("/teams/attention");
  });

  test("says plainly when nothing needs you", () => {
    installFeed({ items: [] });
    render(<AttentionList />);
    expect(screen.getByText(/nothing needs you/i)).toBeInTheDocument();
    expect(document.querySelectorAll("[data-attention-kind]")).toHaveLength(0);
  });

  test("a failed read is surfaced, never rendered as inbox zero", () => {
    installFeed(undefined, { error: new Error("boom") });
    render(<AttentionList />);
    // Claiming "nothing needs you" off a failed read is the exact failure
    // this surface exists to prevent.
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn't load/i);
    expect(screen.queryByText(/nothing needs you/i)).not.toBeInTheDocument();
  });

  test("health rows are not counted as decisions", () => {
    // The upstream feed also carries failed runs / budget alerts. Those are
    // FYI, not questions, and live in the Agent failures band — mixing them
    // in is what made the old band impossible to act on.
    installFeed({
      items: [
        approvalItem,
        { ...approvalItem, id: "att_9", sourceKind: "failed_run", approval: null },
        { ...approvalItem, id: "att_10", sourceKind: "budget_alert", approval: null },
      ],
    });
    render(<AttentionList />);
    expect(screen.getAllByText("Approval")).toHaveLength(1);
    expect(document.querySelectorAll("[data-attention-kind]")).toHaveLength(1);
  });

  test("blocked tasks are decisions — the owner named them explicitly", () => {
    installFeed({
      items: [
        {
          ...interactionItem,
          id: "att_b",
          sourceKind: "blocker_attention",
          interaction: null,
          whyNow: "Task is blocked behind an unresolved dependency.",
        },
      ],
    });
    render(<AttentionList />);
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(
      screen.getByText(/blocked behind an unresolved dependency/i),
    ).toBeInTheDocument();
  });

  const decisionItem = {
    id: "att_d",
    sourceKind: "decision",
    subject: { kind: "decision", id: "dec_1", title: "Ship?", identifier: null, status: "open", href: null },
    whyNow: "An agent decision is waiting for a board response.",
    severity: "medium",
    activityAt: new Date().toISOString(),
    createdAt: new Date().toISOString(),
    relatedIssue: { kind: "issue", id: "iss_1", title: "Release", identifier: "ISO-1", status: "todo", href: null },
    originAgentName: "Friday",
  };
  const decision = {
    id: "dec_1",
    companyId: "co_1",
    originAgentId: "ag_1",
    originIssueId: "iss_1",
    title: "Ship the release?",
    body: "",
    options: [
      {
        id: "ship",
        label: "Ship it",
        effects: [{ type: "comment_on_issue", targetIssueId: "iss_1", staleness: "strict", bodyMarkdown: "Go." }],
      },
    ],
    inputs: null,
    status: "open",
    executionStatus: null,
    chosenOptionId: null,
    targetSnapshots: {},
    continuationPolicy: "wake_origin_agent",
    metadata: {},
    executions: [],
  };

  const OPEN_LIST = "/decisions?status=open&limit=100";
  const mutateDecision = vi.fn();

  /** `openList: null` is the open list still loading. */
  function installDecision(openList: unknown[] | null = []) {
    read.mockImplementation((path: string | null) => ({
      data:
        path === "/attention"
          ? { items: [decisionItem] }
          : path === "/decisions/dec_1"
            ? decision
            : path === OPEN_LIST && openList
              ? { decisions: openList }
              : undefined,
      isLoading: false,
      error: null,
      mutate: path === "/decisions/dec_1" ? mutateDecision : vi.fn(),
    }));
  }

  test("a decision is answerable in place and leaves the list once answered", async () => {
    installDecision();
    post.mockResolvedValue({ ...decision, status: "decided", executionStatus: "succeeded" });
    render(<AttentionList />);

    // The full card, not the read-only "Open thread" row.
    expect(screen.getByText("Ship the release?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open thread/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /ship it/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/decisions/dec_1/decide",
        expect.objectContaining({ optionId: "ship", inputValues: {} }),
      );
    });
    await waitFor(() => expect(mutate).toHaveBeenCalledWith("/teams/attention"));
    expect(mutate).toHaveBeenCalledWith(`/teams${OPEN_LIST}`);
  });

  test("a decision card takes targetChanged from the one shared open list", () => {
    installDecision([{ id: "dec_1", targetChanged: { iss_1: true } }]);
    render(<AttentionList />);

    expect(read).toHaveBeenCalledWith(OPEN_LIST);
    expect(screen.getByText(/1 target changed since this was proposed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ship it/i })).toBeDisabled();
  });

  test("a strict option stays disabled until the open list has loaded", () => {
    installDecision(null);
    render(<AttentionList />);

    expect(screen.getByRole("button", { name: /ship it/i })).toBeDisabled();
    expect(screen.queryByText(/target changed since this was proposed/i)).not.toBeInTheDocument();
  });

  test("a failed decide re-reads the decision, the open list and the feed", async () => {
    installDecision();
    post.mockRejectedValueOnce(new Error("Gateway timeout"));
    render(<AttentionList />);

    fireEvent.click(screen.getByRole("button", { name: /ship it/i }));

    // The attempt may have landed upstream: reconcile rather than trust the error.
    await screen.findByText("Gateway timeout");
    expect(mutateDecision).toHaveBeenCalledWith();
    expect(mutate).toHaveBeenCalledWith(`/teams${OPEN_LIST}`);
    expect(mutate).toHaveBeenCalledWith("/teams/attention");
  });

  test("retrying a failed decide reuses the idempotency key", async () => {
    installDecision();
    post.mockRejectedValueOnce(new Error("Gateway timeout"));
    render(<AttentionList />);

    fireEvent.click(screen.getByRole("button", { name: /ship it/i }));
    // The failure is shown on the card and the option is offered again.
    await screen.findByText("Gateway timeout");
    fireEvent.click(screen.getByRole("button", { name: /ship it/i }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const [first, second] = post.mock.calls.map((call) => (call[1] as { idempotencyKey: string }).idempotencyKey);
    expect(first).toBeTruthy();
    expect(second).toBe(first);
  });

  test("an unhydrated item links out instead of pretending to be resolvable", () => {
    const onOpenIssue = vi.fn();
    installFeed({ items: [{ ...interactionItem, interaction: null }] });
    render(<AttentionList onOpenIssue={onOpenIssue} />);

    fireEvent.click(screen.getByRole("button", { name: /open thread/i }));
    expect(onOpenIssue).toHaveBeenCalledWith("iss_1");
  });
});
