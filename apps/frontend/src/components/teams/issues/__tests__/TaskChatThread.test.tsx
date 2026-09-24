import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TaskChatThread } from "@/components/teams/issues/task-chat/TaskChatThread";
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

function renderThread(props: Partial<Parameters<typeof TaskChatThread>[0]> = {}) {
  const onAdd = vi.fn().mockResolvedValue(undefined);
  const onDeleteComment = vi.fn().mockResolvedValue(undefined);
  render(
    <TaskChatThread
      issue={ISSUE}
      comments={[]}
      agents={AGENTS}
      currentUserId="u_ada"
      onAdd={onAdd}
      onDeleteComment={onDeleteComment}
      onSaveDescription={vi.fn()}
      {...props}
    />,
  );
  return { onAdd, onDeleteComment };
}

beforeEach(() => {
  reads.clear();
  vi.clearAllMocks();
});

describe("TaskChatThread (ported stock task chat)", () => {
  it("renders the description as the first bubble and comments as markdown bubbles", () => {
    renderThread({
      comments: [
        comment({ id: "c1", authorAgentId: "agt_friday", body: "Done — see `notes.md`" }),
        comment({ id: "c2", authorUserId: "pcu_1", authorName: "Ada Lovelace", body: "**Thanks**" }),
      ],
    });
    const brief = screen.getByTestId("task-chat-description-bubble");
    expect(within(brief).getByText("tidy").tagName).toBe("STRONG");
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("notes.md").tagName).toBe("CODE");
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Thanks").tagName).toBe("STRONG");
  });

  it('labels the viewer "You" and an unenriched human "User"', () => {
    renderThread({
      comments: [
        comment({ id: "c1", authorUserId: "pcu_1", authorName: "Ada", authorClerkUserId: "u_ada" }),
        comment({ id: "c2", authorUserId: "pcu_2" }),
      ],
    });
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.queryByText("Ada")).not.toBeInTheDocument();
    expect(screen.getByText("User")).toBeInTheDocument();
  });

  it("renders agent:// mentions as chips, not dead links", () => {
    renderThread({ comments: [comment({ authorAgentId: "agt_friday", body: "Ping [@Friday](agent://agt_friday)" })] });
    const chip = screen.getByText("@Friday");
    expect(chip.tagName).toBe("SPAN");
    expect(chip).toHaveAttribute("data-mention-href", "agent://agt_friday");
  });

  it("posts from the composer with ⌘+Enter and inserts an @mention as an agent link", async () => {
    const user = userEvent.setup();
    const { onAdd } = renderThread();
    expect(screen.getByPlaceholderText("Message Friday — describe what you want done…")).toBeInTheDocument();
    const box = screen.getByRole("combobox", { name: "Message" });
    await user.type(box, "hey @fri");
    await user.click(screen.getByRole("option", { name: "@Friday" }));
    await user.type(box, "please look{Meta>}{Enter}{/Meta}");
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

  it("shows the empty state and keeps Send disabled until there is a draft", () => {
    renderThread();
    expect(screen.getByText("No messages yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends on Ctrl+Enter, not on a plain Enter (newline)", async () => {
    const { onAdd } = renderThread();
    const box = screen.getByRole("combobox", { name: "Message" });
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
    fireEvent.change(screen.getByRole("combobox", { name: "Message" }), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mockNotifyApiError).toHaveBeenCalledWith(failure, "Failed to post comment"));
  });

  it("⌘+Enter sends even with the mention list open on a bare @", async () => {
    const user = userEvent.setup();
    const { onAdd } = renderThread();
    const box = screen.getByRole("combobox", { name: "Message" });
    await user.type(box, "ask @");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(box).toHaveAttribute("aria-expanded", "true");
    expect(box).not.toHaveAttribute("aria-activedescendant");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(onAdd).toHaveBeenCalledWith("ask @");
  });

  it("Esc closes the mention list without touching the draft", async () => {
    const user = userEvent.setup();
    renderThread();
    const box = screen.getByRole("combobox", { name: "Message" });
    await user.type(box, "hi @fri");
    expect(box).toHaveAttribute("aria-activedescendant");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(box).toHaveAttribute("aria-expanded", "false");
    expect(box).toHaveValue("hi @fri");
  });

  it('labels a comment with no author ids "Comment" and offers no feedback votes', () => {
    renderThread({
      onVote: vi.fn(),
      comments: [
        comment({ id: "c1", body: "orphan" }),
        comment({ id: "c2", authorAgentId: "agt_friday", body: "from friday" }),
      ],
    });
    expect(screen.getByText("Comment")).toBeInTheDocument();
    expect(screen.queryByText("Agent")).not.toBeInTheDocument();
    // Only Friday's comment carries the vote controls.
    expect(screen.getAllByRole("button", { name: /Helpful/ })).toHaveLength(1);
  });

  it("drops tombstoned comments", () => {
    renderThread({ comments: [comment({ body: "gone", deletedAt: "2026-09-24T11:00:00Z" })] });
    expect(screen.queryByText("gone")).not.toBeInTheDocument();
  });

  it("shows the live run pill with the run's status copy, and Stop cancels the run", async () => {
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

  describe("interaction cards stay mounted in the thread", () => {
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
      renderThread({
        interactions: [
          interaction({ resolvedByUserId: "pcu_1", resolvedByEmail: "ada@example.com", resolvedByClerkUserId: "u_bob" }),
        ],
      });
      expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    });

    it('falls back to "User" (never an id prefix) for an unenriched resolver', () => {
      renderThread({ interactions: [interaction({ resolvedByUserId: "user_abcdef123" })] });
      expect(screen.getByText("User")).toBeInTheDocument();
      expect(screen.queryByText("user_")).not.toBeInTheDocument();
    });
  });
});
