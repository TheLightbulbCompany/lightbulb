// apps/frontend/src/__tests__/teams/SubIssuesSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
// Stub the dialog so we don't pull its mutation tree; assert it opens with parentId.
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: ({ open, parentId }: { open: boolean; parentId?: string }) =>
    open ? <div>dialog-open:{parentId}</div> : null,
}));

import { SubIssuesSection } from "@/components/teams/issues/SubIssuesSection";

function child(id: string, status = "todo") {
  return { id, title: `T-${id}`, status, createdAt: "2026-05-01T00:00:00Z" };
}

beforeEach(() => read.mockReset());

describe("SubIssuesSection", () => {
  it("shows just the New Sub-issue button when there are no descendants", () => {
    read.mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    expect(screen.getByRole("button", { name: /new sub-issue/i })).toBeInTheDocument();
    expect(screen.queryByText(/Sub-issues/)).toBeNull();
  });

  it("renders the descendants list + progress summary (>=2)", () => {
    read.mockReturnValue({
      data: { issues: [child("a", "done"), child("b", "in_progress")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    expect(screen.getByText(/Sub-issues/)).toBeInTheDocument();
    // Scope title-existence checks to the descendants list: the progress
    // summary also embeds the "Next:" target title (e.g. "Next: T-b"), so an
    // unscoped getByText(/T-b/) would match both the summary and the row.
    const list = within(screen.getByRole("list"));
    expect(list.getByText(/T-a/)).toBeInTheDocument();
    expect(list.getByText(/T-b/)).toBeInTheDocument();
    expect(screen.getByText(/1\s*\/\s*2/)).toBeInTheDocument(); // 1 done of 2
  });

  it("opens the dialog with the parent id when New Sub-issue is clicked", () => {
    read.mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    fireEvent.click(screen.getByRole("button", { name: /new sub-issue/i }));
    expect(screen.getByText("dialog-open:iss1")).toBeInTheDocument();
  });
});
