import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { UseIssueMutationsResult } from "@/components/teams/issues/hooks/useIssueMutations";
import type { UseIssueDetailResult } from "@/components/teams/issues/hooks/useIssueDetail";
import type { Issue } from "@/components/teams/shared/types";

const WITH_DESCRIPTION: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Tidy the backlog",
  status: "todo",
  priority: null,
  description: "Existing body",
};

// A hoisted, mutable issue holder + setter lets each test render the page with
// or without a description, and a single update spy asserts the PATCH payload.
const { updateSpy, currentIssue, setIssue } = vi.hoisted(() => {
  const holder: { value: Issue } = {
    value: {
      id: "iss_42",
      identifier: "ISO-42",
      title: "Tidy the backlog",
      status: "todo",
      priority: null,
      description: "Existing body",
    },
  };
  return {
    updateSpy: vi.fn(),
    currentIssue: holder,
    setIssue: (i: Issue) => {
      holder.value = i;
    },
  };
});

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: (): UseIssueMutationsResult => ({
    create: vi.fn(),
    createChild: vi.fn(),
    update: updateSpy,
    remove: vi.fn(),
    addComment: vi.fn(),
    monitorCheckNow: vi.fn(),
  }),
}));

vi.mock("@/components/teams/issues/hooks/useIssueDetail", () => ({
  useIssueDetail: (): UseIssueDetailResult => ({
    issue: currentIssue.value,
    comments: [],
    isLoading: false,
    isError: false,
    error: null,
  }),
}));

// useTeamsApi.read feeds the agents/projects pickers + relation/children rows —
// return empty data for every path.
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

const writeText = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  setIssue({ ...WITH_DESCRIPTION });
});

describe("IssueDetailPage description editing + copy-as-markdown", () => {
  it("saves the edited description via mutations.update", async () => {
    const user = userEvent.setup();
    updateSpy.mockResolvedValue(currentIssue.value);

    render(<IssueDetailPage issueId="iss_42" />);

    // Click the read-only description to enter edit mode, replace, then blur.
    await user.click(screen.getByText("Existing body"));
    const textarea = screen.getByRole("textbox", { name: /description/i });
    await user.clear(textarea);
    await user.type(textarea, "Rewritten body");
    await user.tab();

    expect(updateSpy).toHaveBeenCalledWith("iss_42", {
      description: "Rewritten body",
    });
  });

  it("shows the 'Add a description…' affordance when description is null", () => {
    setIssue({ ...WITH_DESCRIPTION, description: null });

    render(<IssueDetailPage issueId="iss_42" />);

    expect(screen.getByText("Add a description…")).toBeInTheDocument();
  });

  it("copies the issue as markdown from the … menu", async () => {
    const user = userEvent.setup();
    // userEvent.setup() installs its own clipboard stub on navigator; override
    // it after setup so the handler writes through our spy.
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<IssueDetailPage issueId="iss_42" />);

    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    await user.click(await screen.findByText("Copy as markdown"));

    expect(writeText).toHaveBeenCalledWith(
      "# ISO-42: Tidy the backlog\n\nExisting body",
    );
  });
});
