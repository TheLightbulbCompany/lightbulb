import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { UseIssueMutationsResult } from "@/components/teams/issues/hooks/useIssueMutations";
import type { UseIssueDetailResult } from "@/components/teams/issues/hooks/useIssueDetail";
import type { Issue } from "@/components/teams/shared/types";

// The watchdog is written through useTeamsApi().put, then the detail read is
// refreshed through useTeamsApi().mutate. Hoisted spies let the test make the
// write succeed and the refresh fail.
const { putSpy, mutateSpy } = vi.hoisted(() => ({ putSpy: vi.fn(), mutateSpy: vi.fn() }));

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: (): UseIssueMutationsResult => ({
    create: vi.fn(),
    createChild: vi.fn(),
    updateInList: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    addComment: vi.fn(),
    monitorCheckNow: vi.fn(),
  }),
}));

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

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({
      data: path === "/agents" ? { agents: [{ id: "agent-1", name: "Atlas" }] } : undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    }),
    put: putSpy,
    mutate: mutateSpy,
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssueDetailPage watchdog save", () => {
  it("treats the save as done when the PUT lands, even if the refresh after it fails", async () => {
    const user = userEvent.setup();
    putSpy.mockResolvedValue({ id: "wd-1", watchdogAgentId: "agent-1" });
    mutateSpy.mockRejectedValue(new Error("revalidation failed"));

    render(<IssueDetailPage issueId="iss_42" />);

    const row = screen.getByText("Watchdog").closest("div") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "None" }));
    await user.selectOptions(screen.getByLabelText("Watchdog agent"), "agent-1");
    await user.click(screen.getByRole("button", { name: "Set watchdog" }));

    // The editor closes (success) instead of showing an error with a retry.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Set watchdog" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mutateSpy).toHaveBeenCalledWith("/teams/issues/iss_42");
    expect(putSpy).toHaveBeenCalledTimes(1);
    expect(putSpy).toHaveBeenCalledWith("/issues/iss_42/watchdog", {
      agentId: "agent-1",
      instructions: null,
    });
  });
});
