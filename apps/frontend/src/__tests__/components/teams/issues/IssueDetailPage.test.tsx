import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  Issue,
  IssueExecutionPolicy,
  CompanyAgent,
} from "@/components/teams/shared/types";

// Mock all hooks before importing the component under test.
vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: vi.fn(),
}));
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: vi.fn(),
}));

import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import { useIssueDetail } from "@/components/teams/issues/hooks/useIssueDetail";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUpdate = vi.fn();
const mockRemove = vi.fn();
const mockAddComment = vi.fn();
const mockCreate = vi.fn();

// Default read mock: returns an empty `{ issues: [] }` envelope so existing
// tests are unaffected and SubIssuesSection (which reads
// `/issues/{id}/children`) sees no children and renders its empty state.
const defaultRead = vi
  .fn()
  .mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useIssueMutations).mockReturnValue({
    create: mockCreate,
    createChild: vi.fn(),
    update: mockUpdate,
    updateInList: vi.fn(),
    remove: mockRemove,
    addComment: mockAddComment,
    monitorCheckNow: vi.fn(),
  });
  defaultRead.mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });
  vi.mocked(useTeamsApi).mockReturnValue({
    read: defaultRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
});

describe("IssueDetailPage", () => {
  test("renders Loading state when isLoading + no issue", () => {
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: undefined,
      comments: [],
      isLoading: true,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  test("renders error state when isError + no issue", () => {
    const err = new Error("server boom");
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: undefined,
      comments: [],
      isLoading: false,
      isError: true,
      error: err,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/failed to load issue/i)).toBeInTheDocument();
    expect(screen.getByText(/server boom/i)).toBeInTheDocument();
  });

  test("renders 'Issue not found' when no issue + not loading + no error", () => {
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: undefined,
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByText(/issue not found/i)).toBeInTheDocument();
  });

  test("renders header + properties when issue loaded", () => {
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: {
        id: "iss_1",
        identifier: "PAP-1",
        title: "Fix the inbox",
        status: "todo",
        priority: "high",
      },
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
    expect(screen.getByText("PAP-1")).toBeInTheDocument();
    // Sidebar property label.
    expect(screen.getByText("Status")).toBeInTheDocument();
  });

  test("description renders when present", () => {
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: {
        id: "iss_1",
        title: "x",
        status: "todo",
        description: "Body text here",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any,
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByText("Body text here")).toBeInTheDocument();
  });

  test("title save flows through update mutation", async () => {
    mockUpdate.mockResolvedValue({});
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: { id: "iss_1", title: "Old title", status: "todo" },
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    fireEvent.click(screen.getByText("Old title"));
    const input = screen.getByDisplayValue("Old title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "New title" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith("iss_1", { title: "New title" }),
    );
  });

  test("comment submit flows through addComment mutation", async () => {
    mockAddComment.mockResolvedValue({});
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: { id: "iss_1", title: "x", status: "todo" },
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    const textarea = screen.getByRole("combobox", { name: "Leave a comment" });
    fireEvent.change(textarea, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(mockAddComment).toHaveBeenCalledWith("iss_1", "Hello"),
    );
  });

  test("renders comments list when present", () => {
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: { id: "iss_1", title: "x", status: "todo" },
      comments: [
        { id: "c1", body: "First comment", createdAt: "2026-05-05T00:00:00Z" },
        { id: "c2", body: "Second comment", createdAt: "2026-05-05T01:00:00Z" },
      ],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<IssueDetailPage issueId="iss_1" />);
    expect(screen.getByText("First comment")).toBeInTheDocument();
    expect(screen.getByText("Second comment")).toBeInTheDocument();
  });

  test("changing the assignee picker calls mutations.update with assigneeAgentId", async () => {
    const user = userEvent.setup();

    // Wire up /agents and /projects reads; all other paths return empty.
    const readMock = vi.fn((path: string) => {
      switch (path) {
        case "/agents":
          return { data: { agents: [{ id: "ag_1", name: "Friday" }] }, isLoading: false, error: null };
        case "/projects":
          return { data: { projects: [] }, isLoading: false, error: null };
        default:
          // e.g. `/issues/{id}/children` from SubIssuesSection — empty envelope.
          return { data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() };
      }
    });
    vi.mocked(useTeamsApi).mockReturnValue({
      read: readMock,
      post: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);

    mockUpdate.mockResolvedValue({});
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: { id: "iss_1", title: "Fix the inbox", status: "todo", assigneeAgentId: null },
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });

    render(<IssueDetailPage issueId="iss_1" />);
    await user.click(screen.getAllByRole("button", { name: /Unassigned/i })[0]);
    await user.click(await screen.findByText("Friday"));
    // Picking an agent clears any human assignee in the same PATCH (agent and
    // human assignees are mutually exclusive).
    expect(mockUpdate).toHaveBeenCalledWith("iss_1", {
      assigneeAgentId: "ag_1",
      assigneeUserId: null,
    });
  });

  // ── executionPolicy merge semantics ────────────────────────────────────────
  // The Monitor, Reviewers, and Approvers rows all write the issue's
  // executionPolicy, which has REPLACE semantics on the wire. The detail page
  // routes every edit through patchExecutionPolicy, which read-modify-writes the
  // current policy so editing one sub-field never wipes the others. These are
  // the load-bearing correctness tests for that invariant.

  // Wire /agents (so the reviewer picker has a roster) + /projects; everything
  // else returns empty. Returns the spied mock so a test can override per-call.
  function wireAgentsRead(agents: CompanyAgent[]) {
    const readMock = vi.fn((path: string) => {
      switch (path) {
        case "/agents":
          return { data: { agents }, isLoading: false, error: null };
        case "/projects":
          return { data: { projects: [] }, isLoading: false, error: null };
        case "/members":
          return { data: { members: [] }, isLoading: false, error: null };
        default:
          return { data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() };
      }
    });
    vi.mocked(useTeamsApi).mockReturnValue({
      read: readMock,
      post: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
  }

  test("editing the monitor preserves existing reviewer/approver stages", async () => {
    const user = userEvent.setup();
    wireAgentsRead([{ id: "ag_rev", name: "Reviewer Bot" }]);
    mockUpdate.mockResolvedValue({});

    // An issue that already has a review stage AND a scheduled monitor.
    const policy: IssueExecutionPolicy = {
      mode: "normal",
      monitor: { nextCheckAt: "2026-06-01T00:00:00.000Z", notes: null, serviceName: null },
      stages: [
        {
          id: "stg_rev",
          type: "review",
          approvalsNeeded: 1,
          participants: [{ id: "p1", type: "agent", agentId: "ag_rev", userId: null }],
        },
      ],
    };
    const issue: Issue = {
      id: "iss_1",
      title: "Gated task",
      // Monitors are editable only on an agent-owned in-progress task.
      status: "in_progress",
      assigneeAgentId: "ag_rev",
      executionPolicy: policy,
    };
    vi.mocked(useIssueDetail).mockReturnValue({
      issue,
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });

    render(<IssueDetailPage issueId="iss_1" />);

    // Re-schedule the monitor via the rail's Edit → Schedule flow.
    await user.click(screen.getByRole("button", { name: /^Edit$/i }));
    await user.clear(screen.getByLabelText("Re-check at"));
    await user.type(screen.getByLabelText("Re-check at"), "2026-08-01T09:00");
    const editor = screen.getByRole("dialog");
    await user.click(within(editor).getByRole("button", { name: /^Schedule$/i }));

    expect(mockUpdate).toHaveBeenCalledTimes(1);
    const sent = mockUpdate.mock.calls[0][1] as { executionPolicy: IssueExecutionPolicy };
    // The monitor changed…
    expect(sent.executionPolicy.monitor?.nextCheckAt).not.toBe(
      "2026-06-01T00:00:00.000Z",
    );
    // …but the review stage SURVIVED the edit (the core regression guard).
    const reviewStage = sent.executionPolicy.stages?.find((s) => s.type === "review");
    expect(reviewStage).toBeDefined();
    expect(reviewStage?.participants).toEqual([
      expect.objectContaining({ type: "agent", agentId: "ag_rev" }),
    ]);
  });

  test("editing reviewers preserves an existing monitor", async () => {
    const user = userEvent.setup();
    wireAgentsRead([{ id: "ag_rev", name: "Reviewer Bot" }]);
    mockUpdate.mockResolvedValue({});

    // An issue with only a scheduled monitor and no stages yet.
    const monitor = {
      nextCheckAt: "2026-06-01T00:00:00.000Z",
      notes: "keep watching",
      serviceName: "CI",
    };
    const issue: Issue = {
      id: "iss_1",
      title: "Monitored task",
      status: "todo",
      executionPolicy: { mode: "normal", monitor, stages: [] },
    };
    vi.mocked(useIssueDetail).mockReturnValue({
      issue,
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });

    render(<IssueDetailPage issueId="iss_1" />);

    // Add a reviewer via the rail's Reviewers row.
    await user.click(screen.getByRole("button", { name: /Add reviewer/i }));
    await user.click(await screen.findByText("Reviewer Bot"));

    expect(mockUpdate).toHaveBeenCalledTimes(1);
    const sent = mockUpdate.mock.calls[0][1] as { executionPolicy: IssueExecutionPolicy };
    // The review stage was created…
    const reviewStage = sent.executionPolicy.stages?.find((s) => s.type === "review");
    expect(reviewStage?.participants).toEqual([
      expect.objectContaining({ type: "agent", agentId: "ag_rev" }),
    ]);
    // …and the monitor SURVIVED (the inverse regression guard).
    expect(sent.executionPolicy.monitor).toEqual(monitor);
  });

  test("Delete issue action confirms then removes + leaves for the Tasks list", async () => {
    const user = userEvent.setup();
    mockRemove.mockResolvedValue(undefined);
    vi.mocked(useIssueDetail).mockReturnValue({
      issue: { id: "iss_1", identifier: "PAP-1", title: "Fix the inbox", status: "todo" },
      comments: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    // Seed the URL with the (soon-to-be-dead) detail view's params so the
    // test can prove the swap: panel -> teams-issues, detailId dropped, and
    // replaceState (no history entry for Back to land on).
    window.history.replaceState(
      null,
      "",
      "/chat?panel=teams-issue-detail&detailId=iss_1",
    );

    render(<IssueDetailPage issueId="iss_1" />);

    // Open the header actions menu, click "Delete issue" -> confirm dialog.
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: /delete issue/i }));

    // Confirm in the dialog (the destructive button, not the menu item).
    const confirm = await screen.findByRole("button", { name: /^delete issue$/i });
    await user.click(confirm);

    await waitFor(() => expect(mockRemove).toHaveBeenCalledWith("iss_1"));
    await waitFor(() => {
      const params = new URLSearchParams(window.location.search);
      expect(params.get("panel")).toBe("teams-issues");
      expect(params.get("detailId")).toBeNull();
    });
  });
});
