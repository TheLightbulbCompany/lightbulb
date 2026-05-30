// apps/frontend/src/__tests__/teams/IssueApprovalsSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
vi.mock("@/components/teams/inbox/ApprovalRow", () => ({
  ApprovalRow: ({ approval }: { approval: { id: string; title?: string } }) => <li>row:{approval.id}</li>,
}));

import { IssueApprovalsSection } from "@/components/teams/issues/IssueApprovalsSection";

beforeEach(() => read.mockReset());

describe("IssueApprovalsSection", () => {
  it("renders nothing when there are no linked approvals", () => {
    read.mockReturnValue({ data: { approvals: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueApprovalsSection issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an Approvals header + a row per linked approval", () => {
    read.mockReturnValue({
      data: { approvals: [{ id: "ap1", status: "pending" }, { id: "ap2", status: "approved" }] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueApprovalsSection issueId="iss1" />);
    expect(screen.getByText(/Approvals/)).toBeInTheDocument();
    expect(screen.getByText("row:ap1")).toBeInTheDocument();
    expect(screen.getByText("row:ap2")).toBeInTheDocument();
  });
});
