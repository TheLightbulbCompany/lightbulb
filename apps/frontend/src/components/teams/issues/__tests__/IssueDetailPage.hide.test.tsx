import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { UseIssueMutationsResult } from "@/components/teams/issues/hooks/useIssueMutations";
import type { UseIssueDetailResult } from "@/components/teams/issues/hooks/useIssueDetail";
import type { Issue } from "@/components/teams/shared/types";

// The "…" menu's Hide action goes through useIssueMutations().update. A single
// hoisted spy lets the test assert the exact PATCH payload the item fires.
const { updateSpy } = vi.hoisted(() => ({ updateSpy: vi.fn() }));

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: (): UseIssueMutationsResult => ({
    create: vi.fn(),
    createChild: vi.fn(),
    updateInList: vi.fn(),
    update: updateSpy,
    remove: vi.fn(),
    addComment: vi.fn(),
    monitorCheckNow: vi.fn(),
    updateInList: vi.fn(),
  }),
}));

// Supply a loaded issue so the page renders its body (and the "…" menu).
const ISSUE: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Tidy the backlog",
  status: "todo",
  priority: null,
};

vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: (): UseIssueDetailResult => ({
    issue: ISSUE,
    comments: [],
    isLoading: false,
    isError: false,
    error: null,
  }),
}));

// useTeamsApi.read feeds the agents/projects pickers — return empty lists.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssueDetailPage Hide action", () => {
  it("hides the task with an ISO hiddenAt timestamp from the … menu", async () => {
    const user = userEvent.setup();
    updateSpy.mockResolvedValue(ISSUE);

    render(<IssueDetailPage issueId="iss_42" />);

    // Open the overflow menu, then click Hide task (no confirm dialog).
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    await user.click(await screen.findByText("Hide task"));

    expect(updateSpy).toHaveBeenCalledWith(
      "iss_42",
      expect.objectContaining({ hiddenAt: expect.any(String) }),
    );
  });
});
