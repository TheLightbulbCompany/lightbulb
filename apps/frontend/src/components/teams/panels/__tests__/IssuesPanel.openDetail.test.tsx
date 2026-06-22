import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";
import { IssuesPanel } from "../IssuesPanel";

// useTeamsApi.read returns a single issue so the row (and its "Open →"
// affordance) renders; the other reads (agents/projects) return empty.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      if (path === "/issues")
        return {
          data: { issues: [{ id: "iss_1", title: "Fix the thing" }] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
  }),
}));

// IssueStatusMenu pulls in mutation hooks; stub it out — unrelated to the swap.
vi.mock("@/components/teams/issues/IssueStatusMenu", () => ({
  IssueStatusMenu: () => null,
}));
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: () => null,
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssuesPanel Open affordance", () => {
  it("renders a button that calls openDetail with the issue id when a nav provider is present", () => {
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail };
    render(
      <InlineTeamsNavProvider value={value}>
        <IssuesPanel />
      </InlineTeamsNavProvider>,
    );
    const open = screen.getByText("Open →");
    expect(open.tagName).toBe("BUTTON");
    fireEvent.click(open);
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });

  it("renders an anchor to /teams/issues/<id> when there is no nav provider", () => {
    render(<IssuesPanel />);
    const open = screen.getByText("Open →");
    expect(open.tagName).toBe("A");
    expect(open).toHaveAttribute("href", "/teams/issues/iss_1");
  });
});
