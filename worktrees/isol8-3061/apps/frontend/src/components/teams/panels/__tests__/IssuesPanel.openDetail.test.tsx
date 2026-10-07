import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";
import { IssuesPanel } from "../IssuesPanel";

// useTeamsApi.read returns a single task so the row renders; the other reads
// (agents/projects) return empty. After the Tasks rebuild, a row is itself the
// open affordance — a plain left-click routes to the in-shell detail view when
// a nav provider is present, and falls back to a native <a> otherwise.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      // Prefix-match: the panel's issues key carries the routine-filter query
      // string (`/issues?excludeRoutineExecutions=true`) by default.
      if (path.startsWith("/issues"))
        return {
          data: { issues: [{ id: "iss_1", title: "Fix the thing", status: "todo" }] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
  }),
}));

// The panel calls useIssueMutations().update for inline status-circle / board
// patches; stub it out — unrelated to the open affordance.
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({
    update: vi.fn().mockResolvedValue(undefined),
    create: vi.fn(),
    createChild: vi.fn(),
    addComment: vi.fn(),
  }),
}));
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: () => null,
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssuesPanel Open affordance", () => {
  it("row click calls openDetail with the issue id when a nav provider is present", () => {
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
    render(
      <InlineTeamsNavProvider value={value}>
        <IssuesPanel />
      </InlineTeamsNavProvider>,
    );
    fireEvent.click(screen.getByText("Fix the thing"));
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });

  it("renders the row as an anchor to the canonical detail URL when there is no nav provider", () => {
    render(<IssuesPanel />);
    const row = screen.getByText("Fix the thing").closest("a");
    expect(row).not.toBeNull();
    expect(row).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_1",
    );
  });
});
