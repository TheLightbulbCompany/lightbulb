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

// NeedsAttentionBand (mounted above the toolbar) calls useTeamsApi().read for
// "/approvals" and "/inbox/runs". Mock the hook so every read returns a benign
// empty SWR-shaped object — with no triage data the band's count is 0 and it
// renders null, leaving every assertion below untouched. The default applies to
// any path, so the band's two reads are both covered.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: undefined,
      isLoading: false,
      error: undefined,
      isValidating: false,
      mutate: vi.fn(),
    }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
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
import { useInboxKeyboardNav } from "@/components/teams/inbox/hooks/useInboxKeyboardNav";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
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
  vi.mocked(useInboxKeyboardNav).mockReset();
  mockRouterPush.mockReset();
  mockRouterReplace.mockReset();
});

/**
 * Pulls the `onOpen` callback InboxPage hands to useInboxKeyboardNav. The hook
 * is mocked so the callback never fires from a real keystroke; invoking it
 * directly is the unit-level way to exercise the "open the selected row" path
 * (Enter / Arrow-Open) without simulating the whole keyboard pipeline.
 */
function captureOnOpen(): (item: { id: string }) => void {
  const call = vi.mocked(useInboxKeyboardNav).mock.calls.at(-1);
  if (!call) throw new Error("useInboxKeyboardNav was not called");
  return call[0].onOpen as (item: { id: string }) => void;
}

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

  test("a ?tab deep link (initialTab) seeds the active tab ahead of the stored default", () => {
    render(<InboxPage {...baseProps} initialTab="unread" />);
    expect(screen.getByText(/no new inbox items/i)).toBeInTheDocument();
  });

  test("an unknown initialTab falls back to the stored/default tab", () => {
    render(<InboxPage {...baseProps} initialTab="constructor" />);
    expect(screen.getByText(/inbox zero/i)).toBeInTheDocument();
  });

  test("a re-delivered initialTab re-applies on an already-mounted page", () => {
    const { rerender } = render(<InboxPage {...baseProps} />);
    expect(screen.getByText(/inbox zero/i)).toBeInTheDocument();
    rerender(<InboxPage {...baseProps} initialTab="recent" />);
    expect(screen.getByText(/no recent inbox items/i)).toBeInTheDocument();
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

  test("clicking 'New task' opens NewIssueDialog", async () => {
    render(<InboxPage {...baseProps} />);
    // Dialog title isn't in the document until opened.
    expect(
      screen.queryByRole("dialog", { name: /new task/i }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    expect(
      await screen.findByRole("dialog", { name: /new task/i }),
    ).toBeInTheDocument();
  });

  test("creating a new task opens the canonical in-shell detail URL", async () => {
    const pushStateSpy = vi.spyOn(window.history, "pushState");
    const create = vi
      .fn()
      .mockResolvedValue({ id: "iss_new_42", title: "Hello" } as Issue);
    vi.mocked(useIssueMutations).mockReturnValue({
      create,
      update: vi.fn().mockResolvedValue(undefined),
      remove: vi.fn().mockResolvedValue(undefined),
    } as unknown as ReturnType<typeof useIssueMutations>);

    render(<InboxPage {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    // Fill the title — the dialog blocks submission on empty title.
    const titleInput = await screen.findByLabelText(/title/i);
    fireEvent.change(titleInput, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: /^create task$/i }));

    await waitFor(() => {
      expect(create).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(pushStateSpy).toHaveBeenCalledWith(
        null,
        "",
        "/chat?panel=teams-issue-detail&detailId=iss_new_42",
      );
    });
    pushStateSpy.mockRestore();
  });

  test("keyboard-open falls back to the canonical URL when no nav provider is mounted", () => {
    const pushStateSpy = vi.spyOn(window.history, "pushState");
    render(<InboxPage {...baseProps} />);
    const onOpen = captureOnOpen();
    onOpen({ id: "iss_kbd" });
    expect(pushStateSpy).toHaveBeenCalledWith(
      null,
      "",
      "/chat?panel=teams-issue-detail&detailId=iss_kbd",
    );
    pushStateSpy.mockRestore();
  });

  test("keyboard-open opens the detail in-shell when a nav provider is present", () => {
    const openDetail = vi.fn();
    render(
      <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
        <InboxPage {...baseProps} />
      </InlineTeamsNavProvider>,
    );
    const onOpen = captureOnOpen();
    const pushStateSpy = vi.spyOn(window.history, "pushState");
    onOpen({ id: "iss_kbd" });
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_kbd");
    expect(pushStateSpy).not.toHaveBeenCalled();
    pushStateSpy.mockRestore();
  });

  test("creating a new task opens the detail in-shell when a nav provider is present", async () => {
    const create = vi
      .fn()
      .mockResolvedValue({ id: "iss_new_99", title: "Hi" } as Issue);
    vi.mocked(useIssueMutations).mockReturnValue({
      create,
      update: vi.fn().mockResolvedValue(undefined),
      remove: vi.fn().mockResolvedValue(undefined),
    } as unknown as ReturnType<typeof useIssueMutations>);
    const openDetail = vi.fn();

    render(
      <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
        <InboxPage {...baseProps} />
      </InlineTeamsNavProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    const titleInput = await screen.findByLabelText(/title/i);
    fireEvent.change(titleInput, { target: { value: "Hi" } });
    fireEvent.click(screen.getByRole("button", { name: /^create task$/i }));

    await waitFor(() => {
      expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_new_99");
    });
  });

});
