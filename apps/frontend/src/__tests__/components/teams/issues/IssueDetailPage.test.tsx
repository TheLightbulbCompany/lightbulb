import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

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
const mockAddComment = vi.fn();
const mockCreate = vi.fn();

// Default read mock: returns empty agents/projects so existing tests are unaffected.
const defaultRead = vi.fn().mockReturnValue({ data: undefined, isLoading: false, error: null });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useIssueMutations).mockReturnValue({
    create: mockCreate,
    update: mockUpdate,
    addComment: mockAddComment,
  });
  defaultRead.mockReturnValue({ data: undefined, isLoading: false, error: null });
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
    const textarea = screen.getByPlaceholderText(/add a comment/i);
    fireEvent.change(textarea, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: /^comment$/i }));
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
          return { data: undefined, isLoading: false, error: null };
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
    expect(mockUpdate).toHaveBeenCalledWith("iss_1", { assigneeAgentId: "ag_1" });
  });
});
