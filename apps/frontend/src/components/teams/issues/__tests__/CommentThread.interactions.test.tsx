import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommentThread } from "@/components/teams/issues/CommentThread";
import { teamsDetailUrl } from "@/lib/shellNav";
import type {
  AskUserQuestionsInteraction,
  Issue,
  IssueThreadInteraction,
  RequestConfirmationInteraction,
  RequestItemVerdictsInteraction,
} from "@/components/teams/shared/types";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: undefined, isLoading: false, error: null, mutate: vi.fn() }),
    post: vi.fn(),
    mutate: vi.fn(),
  }),
}));

const ISSUE: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Tidy the backlog",
  status: "in_progress",
  priority: null,
  description: "",
  assigneeAgentId: "agt_friday",
};

const base = {
  companyId: "co_1",
  issueId: "iss_42",
  status: "pending" as const,
  continuationPolicy: "wake_assignee" as const,
  createdAt: "2026-09-24T10:00:00Z",
  updatedAt: "2026-09-24T10:00:00Z",
  createdByAgentId: "agt_friday",
};

const verdicts: RequestItemVerdictsInteraction = {
  ...base,
  id: "int_v",
  kind: "request_item_verdicts",
  payload: {
    version: 1,
    prompt: "Review these",
    items: [
      { id: "a", label: "Alpha" },
      { id: "b", label: "Beta" },
    ],
  },
};

const questions: AskUserQuestionsInteraction = {
  ...base,
  id: "int_q",
  kind: "ask_user_questions",
  createdAt: "2026-09-24T09:00:00Z",
  payload: {
    version: 1,
    questions: [
      { id: "q1", prompt: "Which colour?", selectionMode: "single", required: false, options: [{ id: "red", label: "Red" }] },
      { id: "q2", prompt: "Which size?", selectionMode: "single", required: true, options: [{ id: "s", label: "Small" }] },
    ],
  },
};

const confirmation: RequestConfirmationInteraction = {
  ...base,
  id: "int_c",
  kind: "request_confirmation",
  payload: { version: 1, prompt: "Ship it?" },
};

function renderThread(interactions: IssueThreadInteraction[]) {
  const handlers = {
    onSkipInteraction: vi.fn().mockResolvedValue(undefined),
    onSubmitInteractionVerdicts: vi.fn().mockResolvedValue(undefined),
    onSubmitInteractionAnswers: vi.fn().mockResolvedValue(undefined),
    onAcceptInteraction: vi.fn().mockResolvedValue(undefined),
  };
  render(
    <CommentThread
      issue={ISSUE}
      comments={[]}
      agents={[{ id: "agt_friday", name: "Friday" }]}
      interactions={interactions}
      onAdd={vi.fn()}
      {...handlers}
    />,
  );
  return handlers;
}

beforeEach(() => vi.clearAllMocks());

describe("task interaction takeover of the comment composer", () => {
  it("a pending interaction replaces the composer; X dismisses to the pending indicator and it reopens", async () => {
    renderThread([verdicts]);
    expect(screen.getByTestId("task-chat-composer-takeover")).toBeInTheDocument();
    expect(screen.queryByLabelText("Leave a comment")).not.toBeInTheDocument();
    // Pending interactions are not rendered in the timeline.
    expect(screen.queryByTestId("task-chat-interaction-receipt")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Dismiss Review items" }));
    expect(screen.getByLabelText("Leave a comment")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("task-chat-pending-input-indicator"));
    expect(screen.getByTestId("task-chat-composer-takeover")).toBeInTheDocument();
  });

  it("item verdicts: approve advances, reject needs a confirmed reason, Apply submits every draft", async () => {
    const { onSubmitInteractionVerdicts } = renderThread([verdicts]);
    const apply = () => screen.getByRole("button", { name: /^Apply/ });
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(screen.getByText("Beta")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(apply()).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "not needed");
    await userEvent.click(screen.getByRole("button", { name: "Submit rejection reason" }));
    expect(apply()).toHaveTextContent("Apply 2");
    await userEvent.click(apply());
    expect(onSubmitInteractionVerdicts).toHaveBeenCalledWith(verdicts, [
      { id: "a", verdict: "approve" },
      { id: "b", verdict: "reject", reason: "not needed" },
    ]);
  });

  it("an item's Open link to a task opens it in the shell, not the dead /issues route", async () => {
    const push = vi.spyOn(window.history, "pushState");
    renderThread([{ ...verdicts, payload: { ...verdicts.payload, items: [{ id: "a", label: "Alpha", href: "/issues/ADM-9" }] } }]);
    const open = screen.getByRole("link", { name: "Open" });
    expect(open).toHaveAttribute("href", teamsDetailUrl("issue", "ADM-9"));
    await userEvent.click(open);
    expect(push).toHaveBeenCalledWith(null, "", teamsDetailUrl("issue", "ADM-9"));
    push.mockRestore();
  });

  it("Skip sits beside Apply on item verdicts and calls the skip handler", async () => {
    const { onSkipInteraction } = renderThread([verdicts]);
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(onSkipInteraction).toHaveBeenCalledWith(verdicts);
    await waitFor(() => expect(screen.getByLabelText("Leave a comment")).toBeInTheDocument());
  });

  it("a failed skip keeps the takeover and says why", async () => {
    const { onSkipInteraction } = renderThread([verdicts]);
    onSkipInteraction.mockRejectedValueOnce(new Error("Interaction is no longer pending"));
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Interaction is no longer pending");
    expect(screen.getByTestId("task-chat-composer-takeover")).toBeInTheDocument();
  });

  it("confirmations keep their own decline path: no composer Skip", () => {
    renderThread([confirmation]);
    expect(screen.getByText("Ship it?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip" })).not.toBeInTheDocument();
  });

  it("the newest pending input shows first; 'N pending' cycles to the next", async () => {
    renderThread([questions, verdicts]);
    expect(screen.getByTestId("item-verdicts-card")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "2 pending" }));
    expect(screen.getByTestId("question-form")).toBeInTheDocument();
  });

  it("questions: Skip leaves an optional answer out; a missing required answer blocks the send", async () => {
    const { onSubmitInteractionAnswers } = renderThread([questions]);
    // Q1 is optional: Skip moves on. Q2 is required: no per-question Skip.
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(screen.getByText("Which size?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit answers" })).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "Small" }));
    await userEvent.click(screen.getByRole("button", { name: "Submit answers" }));
    expect(onSubmitInteractionAnswers).toHaveBeenCalledWith(questions, [
      { questionId: "q1", optionIds: [] },
      { questionId: "q2", optionIds: ["s"] },
    ]);
  });

  it("a resolved verdicts interaction renders its progress receipt in the timeline", () => {
    renderThread([
      {
        ...verdicts,
        status: "answered",
        result: {
          version: 1,
          outcome: "resolved",
          complete: true,
          items: [
            { id: "a", verdict: "approve", resolvedAt: "2026-09-24T10:01:00Z" },
            { id: "b", verdict: "reject", reason: "no", resolvedAt: "2026-09-24T10:01:00Z" },
          ],
        },
      },
    ]);
    expect(screen.getByTestId("task-chat-interaction-receipt")).toHaveTextContent("2 decided · 1 approved · 1 rejected");
    expect(screen.queryByTestId("task-chat-composer-takeover")).not.toBeInTheDocument();
  });
});
