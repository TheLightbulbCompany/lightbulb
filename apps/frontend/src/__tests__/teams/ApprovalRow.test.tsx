// apps/frontend/src/__tests__/teams/ApprovalRow.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const A: Approval = { id: "a1", title: "Deploy to prod?", status: "pending", createdAt: "2026-05-29T00:00:00Z" };

beforeEach(() => { approve.mockReset().mockResolvedValue(undefined); reject.mockReset().mockResolvedValue(undefined); });

describe("ApprovalRow", () => {
  it("Approve calls approve(id)", async () => {
    render(<ApprovalRow approval={A} />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a1"));
  });

  it("Reject requires a reason: confirm disabled until typed, then posts reason", async () => {
    render(<ApprovalRow approval={A} />);
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));
    const confirm = screen.getByRole("button", { name: /confirm reject/i });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/reason/i), { target: { value: "nope" } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(reject).toHaveBeenCalledWith("a1", "nope"));
  });

  it("falls back to a default title when none is given", () => {
    render(<ApprovalRow approval={{ id: "a2", status: "pending" }} />);
    expect(screen.getByText(/approval requested/i)).toBeInTheDocument();
  });

  it("renders a View link to the approval-detail page", () => {
    render(<ApprovalRow approval={A} />);
    const link = screen.getByRole("link", { name: /view/i });
    expect(link).toHaveAttribute("href", "/teams/approvals/a1");
  });

  it("on approve failure shows an error and keeps the row", async () => {
    approve.mockRejectedValueOnce(new Error("boom"));
    render(<ApprovalRow approval={A} />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(screen.getByText(/boom/i)).toBeInTheDocument());
    // Row is kept — the Approve button is still present.
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  });

  it("resolved approvals show a status chip and no action buttons", () => {
    render(<ApprovalRow approval={{ id: "a9", title: "Done one", status: "approved" }} />);
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^reject$/i })).toBeNull();
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
  });

  it("hides the reject form when a mid-reject approval becomes resolved", () => {
    const pending = { id: "a7", title: "Mid", status: "pending" as const };
    const { rerender } = render(<ApprovalRow approval={pending} />);
    // Enter reject mode -> the reason input appears.
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));
    expect(screen.getByPlaceholderText(/reason/i)).toBeInTheDocument();
    // The approval resolves (e.g. revalidation): form must disappear, chip shows.
    rerender(<ApprovalRow approval={{ ...pending, status: "rejected" }} />);
    expect(screen.queryByPlaceholderText(/reason/i)).toBeNull();
    expect(screen.getByText(/rejected/i)).toBeInTheDocument();
  });
});
