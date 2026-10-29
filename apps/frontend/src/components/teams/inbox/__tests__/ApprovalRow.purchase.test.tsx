import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject: vi.fn() }),
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get: vi.fn() }) }));
vi.mock("@/components/settings/CompanyStripeSection", () => ({ useCompanyStripeStatus: () => ({ data: undefined }) }));

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const CARD: Approval = {
  id: "a-buy",
  type: "request_board_approval",
  payload: {
    isol8_action: "purchase_request",
    title: "Approve a $35.00 purchase at Acme for Northwind",
    request_id: "r".repeat(32),
    merchant: "Acme",
    amount_display: "$35.00",
  },
  status: "pending",
};

beforeEach(() => {
  approve.mockReset();
  post.mockReset();
});

describe("ApprovalRow purchase_request", () => {
  it("mints the Link URL on the click, opens it, and does not approve the card", async () => {
    const tab = { opener: undefined, location: { href: "" }, close: vi.fn() };
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockResolvedValue({ status: "pending_approval", approvalUrl: "https://app.link.com/activity/approve/x" });
    render(<ApprovalRow approval={CARD} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve in Link" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(`/company/link-wallet/purchases/${"r".repeat(32)}/approval-link`, {}),
    );
    await waitFor(() => expect(tab.location.href).toBe("https://app.link.com/activity/approve/x"));
    expect(approve).not.toHaveBeenCalled();
  });
});
