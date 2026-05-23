// Smoke tests for InboxPage — verifies the full assembly: skeleton on initial
// load, empty-state copy per tab, list rendering when issues exist, and the
// tab-switch -> empty-state-copy plumbing. Hooks are mocked at the module
// boundary so we don't drag SWR/cache wiring into the page-level test.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("@/components/teams/inbox/hooks/useInboxData", () => ({
  useInboxData: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxKeyboardNav", () => ({
  useInboxKeyboardNav: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxArchiveStack", () => ({
  useInboxArchiveStack: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useReadInboxItems", () => ({
  useReadInboxItems: vi.fn(),
}));
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));

// Override the global next/navigation mock so we can capture router.push calls.
const { mockRouterPush, mockRouterReplace } = vi.hoisted(() => ({
  mockRouterPush: vi.fn(),
  mockRouterReplace: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: mockRouterPush,
    replace: mockRouterReplace,
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

import { InboxPage } from "@/components/teams/inbox/InboxPage";
import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { Issue } from "@/components/teams/shared/types";

function makeIssue(overrides: Partial<Issue> = {}): Issue {
  return {
    id: "iss_1",
    title: "Test issue",
    status: "todo",
    identifier: "PAP-1",
    updatedAt: new Date().toISOString(),
    ...overrides,
  };
}

const baseProps = {
  companyId: "co_1",
  currentUserId: "u_1",
};

beforeEach(() => {
  vi.mocked(useInboxData).mockReturnValue({
    mineIssues: [],
    touchedIssues: [],
    allIssues: [],
    isLoading: false,
    isError: false,
    error: null,
  });
  vi.mocked(useInboxArchiveStack).mockReturnValue({
    archivingIssueIds: new Set(),
    hasUndoableArchive: false,
    archive: vi.fn().mockResolvedValue(undefined),
    undoArchive: vi.fn().mockResolvedValue(undefined),
    markRead: vi.fn().mockResolvedValue(undefined),
    markUnread: vi.fn().mockResolvedValue(undefined),
  });
  vi.mocked(useReadInboxItems).mockReturnValue({
    readItemKeys: new Set(),
    isRead: () => false,
    markRead: vi.fn(),
    markUnread: vi.fn(),
    markManyRead: vi.fn(),
    clearAll: vi.fn(),
  });
  vi.mocked(useIssueMutations).mockReturnValue({
    create: vi.fn().mockResolvedValue({ id: "iss_new", title: "x" } as Issue),
    update: vi.fn().mockResolvedValue(undefined),
    remove: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof useIssueMutations>);
  mockRouterPush.mockReset();
  mockRouterReplace.mockReset();
});

describe("InboxPage", () => {
  test("renders skeleton while initially loading with no sections", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [],
      touchedIssues: [],
      allIssues: [],
      isLoading: true,
      isError: false,
      error: null,
    });
    render(<InboxPage {...baseProps} />);
    expect(
      document.querySelector("[data-testid='page-skeleton']"),
    ).not.toBeNull();
  });

  test("renders 'Inbox zero.' empty state on mine tab when there are no issues", () => {
    render(<InboxPage {...baseProps} />);
    expect(screen.getByText(/inbox zero/i)).toBeInTheDocument();
  });

  test("clicking the unread tab swaps the empty-state copy", () => {
    render(<InboxPage {...baseProps} />);
    fireEvent.click(screen.getByRole("tab", { name: /unread/i }));
    expect(screen.getByText(/no new inbox items/i)).toBeInTheDocument();
  });

  test("clicking the recent tab swaps the empty-state copy", () => {
    render(<InboxPage {...baseProps} />);
    fireEvent.click(screen.getByRole("tab", { name: /recent/i }));
    expect(screen.getByText(/no recent inbox items/i)).toBeInTheDocument();
  });

  test("renders InboxList rows when the active tab has issues", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [makeIssue({ id: "iss_1" })],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    const { container } = render(<InboxPage {...baseProps} />);
    expect(
      container.querySelector("[data-inbox-item-id='iss_1']"),
    ).not.toBeNull();
    // Empty-state copy should NOT be visible.
    expect(screen.queryByText(/inbox zero/i)).not.toBeInTheDocument();
  });

  test("typing in search shows the no-search-match empty state", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [makeIssue({ id: "iss_1", title: "Apples" })],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<InboxPage {...baseProps} />);
    // PanelToolbar emits `<input type="search">` (role=searchbox). Both
    // mobile + desktop variants are present; either one drives state.
    const searchInput = screen.getAllByRole("searchbox")[0] as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "zzzzzz" } });
    expect(
      screen.getByText(/no inbox items match your search/i),
    ).toBeInTheDocument();
  });

  test("Mark all as read button is disabled when there are no unread items", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [makeIssue({ id: "iss_1", unread: false })],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<InboxPage {...baseProps} />);
    expect(
      screen.getByRole("button", { name: /mark all as read/i }),
    ).toBeDisabled();
  });

  test("Mark all as read button is enabled when there are unread items", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [makeIssue({ id: "iss_1", unread: true })],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: false,
      error: null,
    });
    render(<InboxPage {...baseProps} />);
    expect(
      screen.getByRole("button", { name: /mark all as read/i }),
    ).not.toBeDisabled();
  });

  test("shows error banner when useInboxData errors", () => {
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: true,
      error: new Error("boom"),
    });
    render(<InboxPage {...baseProps} />);
    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent(/failed to load inbox/i);
  });

  test("Mark all as read calls readItems.markManyRead with the unread ids", async () => {
    const markManyRead = vi.fn();
    vi.mocked(useReadInboxItems).mockReturnValue({
      readItemKeys: new Set(),
      isRead: () => false,
      markRead: vi.fn(),
      markUnread: vi.fn(),
      markManyRead,
      clearAll: vi.fn(),
    });
    vi.mocked(useInboxData).mockReturnValue({
      mineIssues: [
        makeIssue({ id: "iss_1", title: "Unread", unread: true }),
        makeIssue({ id: "iss_2", title: "Read", unread: false }),
      ],
      touchedIssues: [],
      allIssues: [],
      isLoading: false,
      isError: false,
      error: null,
    });

    render(<InboxPage {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: /mark all as read/i }));
    const confirm = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirm);
    expect(markManyRead).toHaveBeenCalledWith(["iss_1"]);
  });

  test("clicking 'New issue' opens NewIssueDialog", async () => {
    render(<InboxPage {...baseProps} />);
    // Dialog title isn't in the document until opened.
    expect(
      screen.queryByRole("dialog", { name: /new issue/i }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /new issue/i }));
    expect(
      await screen.findByRole("dialog", { name: /new issue/i }),
    ).toBeInTheDocument();
  });

  test("creating a new issue navigates to /teams/issues/<id>", async () => {
    const create = vi
      .fn()
      .mockResolvedValue({ id: "iss_new_42", title: "Hello" } as Issue);
    vi.mocked(useIssueMutations).mockReturnValue({
      create,
      update: vi.fn().mockResolvedValue(undefined),
      remove: vi.fn().mockResolvedValue(undefined),
    } as unknown as ReturnType<typeof useIssueMutations>);

    render(<InboxPage {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: /new issue/i }));
    // Fill the title — the dialog blocks submission on empty title.
    const titleInput = await screen.findByLabelText(/title/i);
    fireEvent.change(titleInput, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: /^create issue$/i }));

    await waitFor(() => {
      expect(create).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockRouterPush).toHaveBeenCalledWith("/teams/issues/iss_new_42");
    });
  });
});
