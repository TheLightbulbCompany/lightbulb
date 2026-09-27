import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommentThread } from "@/components/teams/issues/CommentThread";
import { IssueThreadInteractionCard } from "@/components/teams/issues/IssueThreadInteractionCard";
import type {
  CompanyAgent,
  Issue,
  IssueComment,
  RequestConfirmationInteraction,
} from "@/components/teams/shared/types";

const { mockNotifyApiError } = vi.hoisted(() => ({ mockNotifyApiError: vi.fn() }));
vi.mock("@/lib/notify", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/notify")>()),
  notifyApiError: (err: unknown, fallback?: string) => mockNotifyApiError(err, fallback),
}));

// Per-path read data + write spies for the Teams BFF.
const { reads, postSpy } = vi.hoisted(() => ({
  reads: new Map<string, unknown>(),
  postSpy: vi.fn(),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => ({
      data: path === null ? undefined : reads.get(path.split("?")[0]),
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
    post: postSpy,
    mutate: vi.fn(),
  }),
}));

const ISSUE: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Tidy the backlog",
  status: "in_progress",
  priority: null,
  description: "Please **tidy** it",
  assigneeAgentId: "agt_friday",
};
const AGENTS: CompanyAgent[] = [{ id: "agt_friday", name: "Friday" }];

function comment(overrides: Partial<IssueComment>): IssueComment {
  return { id: "cmt_1", body: "hello", createdAt: "2026-09-24T10:00:00Z", ...overrides };
}

function renderThread(props: Partial<Parameters<typeof CommentThread>[0]> = {}) {
  const onAdd = vi.fn().mockResolvedValue(undefined);
  const onDeleteComment = vi.fn().mockResolvedValue(undefined);
  render(
    <CommentThread
      issue={ISSUE}
      comments={[]}
      agents={AGENTS}
      currentUserId="u_ada"
      onAdd={onAdd}
      onDeleteComment={onDeleteComment}
      {...props}
    />,
  );
  return { onAdd, onDeleteComment };
}

beforeEach(() => {
  reads.clear();
  vi.clearAllMocks();
});

describe("CommentThread (ported stock issue timeline)", () => {
  it("renders comments as author cards with full date-time anchors and markdown bodies", () => {
    renderThread({
      comments: [
        comment({ id: "c1", authorAgentId: "agt_friday", body: "Done — see `notes.md`" }),
        comment({ id: "c2", authorUserId: "pcu_1", authorName: "Ada Lovelace", body: "**Thanks**" }),
      ],
    });
    const cards = screen.getAllByTestId("issue-comment-card");
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("notes.md").tagName).toBe("CODE");
    expect(within(cards[1]!).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Thanks").tagName).toBe("STRONG");
    // Stock formatDateTime: the date is always part of the stamp.
    const stamp = within(cards[0]!).getByRole("link", { name: /2026/ });
    expect(stamp).toHaveAttribute("href", "#comment-c1");
    expect(stamp.textContent).toMatch(/^Sep 24, 2026/);
    expect(screen.getByText("Timeline (2)")).toBeInTheDocument();
  });

  it("orders comments, status changes and runs chronologically", () => {
    reads.set("/issues/iss_42/activity", [
      {
        id: "act_1",
        actorType: "agent",
        actorId: "agt_friday",
        action: "issue.updated",
        details: { status: "in_progress", _previous: { status: "todo" } },
        createdAt: "2026-09-24T09:30:00Z",
      },
    ]);
    reads.set("/issues/iss_42/runs", {
      runs: [{ runId: "run_abcdef123", status: "succeeded", agentId: "agt_friday", finishedAt: "2026-09-24T10:30:00Z" }],
    });
    renderThread({
      comments: [
        comment({ id: "late", authorAgentId: "agt_friday", body: "second", createdAt: "2026-09-25T08:00:00Z" }),
        comment({ id: "early", authorAgentId: "agt_friday", body: "first", createdAt: "2026-09-24T09:00:00Z" }),
      ],
    });
    const order = Array.from(
      document.querySelectorAll('[data-testid="issue-comment-card"], [data-testid="issue-timeline-event"], [data-testid="issue-timeline-run"]'),
    ).map((el) => el.id);
    expect(order).toEqual(["comment-early", "activity-act_1", "run-run_abcdef123", "comment-late"]);
    const event = screen.getByTestId("issue-timeline-event");
    expect(event).toHaveTextContent("updated this task");
    expect(event).toHaveTextContent("todo");
    expect(event).toHaveTextContent("in progress");
    expect(screen.getByTestId("issue-timeline-run")).toHaveTextContent("run run_abcd");
  });

  it('labels the viewer "You", an unenriched human "User", and an unattributed comment "Comment" (no votes)', () => {
    renderThread({
      onVote: vi.fn(),
      comments: [
        comment({ id: "c1", authorUserId: "pcu_1", authorName: "Ada", authorClerkUserId: "u_ada" }),
        comment({ id: "c2", authorUserId: "pcu_2" }),
        comment({ id: "c3", body: "orphan" }),
        comment({ id: "c4", authorAgentId: "agt_friday", body: "from friday" }),
      ],
    });
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.queryByText("Ada")).not.toBeInTheDocument();
    expect(screen.getByText("User")).toBeInTheDocument();
    expect(screen.getByText("Comment", { selector: "span" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Helpful/ })).toHaveLength(1);
  });

  it("shows the run chip on a comment written by a run", () => {
    renderThread({ comments: [comment({ authorAgentId: "agt_friday", createdByRunId: "run_12345678abc" })] });
    expect(screen.getByRole("link", { name: "run run_1234" })).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=run_12345678abc",
    );
  });

  it("renders agent:// mentions as chips, not dead links", () => {
    renderThread({ comments: [comment({ authorAgentId: "agt_friday", body: "Ping [@Friday](agent://agt_friday)" })] });
    const chip = screen.getByText("@Friday");
    expect(chip.tagName).toBe("SPAN");
    expect(chip).toHaveAttribute("data-mention-href", "agent://agt_friday");
  });

  it("posts from the stock composer and inserts an @mention as an agent link", async () => {
    const user = userEvent.setup();
    const { onAdd } = renderThread();
    const box = screen.getByRole("combobox", { name: "Leave a comment" });
    expect(box).toHaveAttribute("placeholder", "Leave a comment...");
    await user.type(box, "hey @fri");
    await user.click(screen.getByRole("option", { name: "@Friday" }));
    await user.type(box, "please look");
    await user.click(screen.getByRole("button", { name: "Comment" }));
    expect(onAdd).toHaveBeenCalledWith("hey [@Friday](agent://agt_friday) please look");
  });

  it("deletes only the viewer's own comment, behind the stock confirm dialog", async () => {
    const user = userEvent.setup();
    const { onDeleteComment } = renderThread({
      comments: [
        comment({ id: "mine", authorUserId: "pcu_1", authorClerkUserId: "u_ada", body: "mine" }),
        comment({ id: "theirs", authorUserId: "pcu_2", authorClerkUserId: "u_bob", body: "theirs" }),
      ],
    });
    const deletes = screen.getAllByRole("button", { name: "Delete comment" });
    expect(deletes).toHaveLength(1);
    await user.click(deletes[0]!);
    await user.click(await screen.findByRole("button", { name: "Delete comment", hidden: false }));
    expect(onDeleteComment).toHaveBeenCalledWith("mine");
  });

  it("shows the empty state and keeps Comment disabled until there is a draft", () => {
    renderThread();
    expect(screen.getByText("No timeline entries yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Comment" })).toBeDisabled();
  });

  it("sends on Ctrl+Enter, not on a plain Enter (newline)", async () => {
    const { onAdd } = renderThread();
    const box = screen.getByRole("combobox", { name: "Leave a comment" });
    fireEvent.change(box, { target: { value: "  line  " } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onAdd).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onAdd).toHaveBeenCalledWith("line"));
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("surfaces a failed post", async () => {
    const failure = new Error("server boom");
    renderThread({ onAdd: vi.fn().mockRejectedValue(failure) });
    fireEvent.change(screen.getByRole("combobox", { name: "Leave a comment" }), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() => expect(mockNotifyApiError).toHaveBeenCalledWith(failure, "Failed to post comment"));
  });

  it("Esc closes the mention list without touching the draft", async () => {
    const user = userEvent.setup();
    renderThread();
    const box = screen.getByRole("combobox", { name: "Leave a comment" });
    await user.type(box, "hi @fri");
    expect(box).toHaveAttribute("aria-activedescendant");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(box).toHaveValue("hi @fri");
  });

  it("keeps a deleted comment as stock's tombstone", () => {
    renderThread({ comments: [comment({ body: "gone", deletedAt: "2026-09-24T11:00:00Z" })] });
    expect(screen.queryByText("gone")).not.toBeInTheDocument();
    expect(screen.getByText("Comment deleted")).toBeInTheDocument();
  });

  it("shows the live run row with the run's status copy, and Stop cancels the run", async () => {
    const user = userEvent.setup();
    reads.set("/issues/iss_42/live-runs", [
      {
        id: "run_1",
        status: "running",
        agentName: "Friday",
        startedAt: "2026-09-24T10:00:00Z",
        currentStatusMessage: "Friday is working",
      },
    ]);
    renderThread();
    expect(screen.getByTestId("task-chat-live-run-pill")).toHaveTextContent("Working");
    expect(screen.getByText("Friday is working")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(postSpy).toHaveBeenCalledWith("/heartbeat-runs/run_1/cancel", {});
  });

  it("says 'Waiting to start...' for a queued run with no transcript yet", () => {
    reads.set("/issues/iss_42/active-run", { id: "run_2", status: "queued" });
    renderThread();
    expect(screen.getByText("Waiting to start...")).toBeInTheDocument();
  });

  describe("classic interaction card actor labels (inbox + takeover body)", () => {
    function interaction(overrides: Partial<RequestConfirmationInteraction>): RequestConfirmationInteraction {
      return {
        id: "int_1",
        companyId: "co_1",
        issueId: "iss_42",
        kind: "request_confirmation",
        status: "accepted",
        continuationPolicy: "none",
        createdAt: "2026-09-24T10:00:00Z",
        updatedAt: "2026-09-24T10:00:00Z",
        createdByAgentId: "agt_friday",
        payload: { version: 1, prompt: "Ship it?" },
        ...overrides,
      };
    }

    it("shows the enriched email for a human resolver", () => {
      render(
        <IssueThreadInteractionCard
          interaction={interaction({ resolvedByUserId: "pcu_1", resolvedByEmail: "ada@example.com", resolvedByClerkUserId: "u_bob" })}
          currentUserId="u_ada"
        />,
      );
      expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    });

    it('falls back to "User" (never an id prefix) for an unenriched resolver', () => {
      render(<IssueThreadInteractionCard interaction={interaction({ resolvedByUserId: "user_abcdef123" })} />);
      expect(screen.getByText("User")).toBeInTheDocument();
      expect(screen.queryByText("user_")).not.toBeInTheDocument();
    });

    it("collapses a resolved interaction in the thread to one receipt row", () => {
      renderThread({ interactions: [interaction({})] });
      expect(screen.getByTestId("task-chat-interaction-receipt")).toHaveTextContent("Confirmed request");
      expect(screen.queryByText("Ship it?")).not.toBeVisible();
    });
  });
});
