// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.connectStripe.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

// Failures surface as a toast, not inline row text.
const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

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
  notifyApiError.mockReset();
  notifyError.mockReset();
  post.mockReset();
  get.mockReset();
});

describe("ApprovalRow company_stripe", () => {
  it("shows Set up Stripe / Connect existing (no Approve); Set up Stripe posts, opens the link, then approves", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    vi.spyOn(window, "open").mockImplementation(() => ({ opener: undefined }) as unknown as Window);
    render(<ApprovalRow approval={A} />);

    expect(screen.getByText("Connect Stripe: Set up Northwind on Stripe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up Stripe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect existing" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    expect(window.open).toHaveBeenCalledWith("https://connect.stripe.com/setup/x", "_blank");
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
  });

  it("does not approve when the popup is blocked", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(approve).not.toHaveBeenCalled();
  });
});

it("a plain board approval keeps the generic Approve button", () => {
  render(<ApprovalRow approval={{ id: "a-board", type: "request_board_approval", status: "pending" }} />);
  expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
});
