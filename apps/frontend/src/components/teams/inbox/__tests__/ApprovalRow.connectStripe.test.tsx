// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.connectStripe.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));


import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

// Paperclip only accepts its own four approval types, so the card is filed as
// `request_board_approval` and `payload.isol8_action` is the discriminator.
const A: Approval = {
  id: "a-stripe",
  type: "request_board_approval",
  payload: { isol8_action: "company_stripe", title: "Set up Northwind on Stripe" },
  status: "pending",
};

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  post.mockReset();
  get.mockReset();
});

describe("ApprovalRow company_stripe", () => {
  // The Stripe doors live once, on the Stripe card in Settings → Connections.
  it("offers one Open Stripe settings button plus Approve, and no Stripe doors of its own", async () => {
    render(<ApprovalRow approval={A} />);

    expect(screen.getByText("Connect Stripe: Set up Northwind on Stripe")).toBeInTheDocument();
    for (const name of ["Set up Stripe", "Connect existing", "Connect your Stripe", "Open Accounts"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    // Settings → Connections, where the Stripe card lives.
    expect(screen.getByRole("link", { name: "Open Stripe settings" })).toHaveAttribute(
      "href",
      "/settings?panel=connections",
    );

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });
});

it("a plain board approval keeps the generic Approve button", () => {
  render(<ApprovalRow approval={{ id: "a-board", type: "request_board_approval", status: "pending" }} />);
  expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
});
