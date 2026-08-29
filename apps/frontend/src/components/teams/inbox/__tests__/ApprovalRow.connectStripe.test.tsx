// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.connectStripe.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

// Failures surface as a toast via notifyApiError, not inline row text.
const { notifyApiError } = vi.hoisted(() => ({ notifyApiError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError }));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const A: Approval = {
  id: "a-stripe",
  type: "connect_stripe",
  status: "pending",
};

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  notifyApiError.mockReset();
  post.mockReset();
  get.mockReset();
});

describe("ApprovalRow connect_stripe", () => {
  it("shows Set up Stripe / Connect existing (no Approve); Set up Stripe posts, opens the link, then approves", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    vi.spyOn(window, "open").mockImplementation(() => window);
    render(<ApprovalRow approval={A} />);

    expect(screen.getByRole("button", { name: "Set up Stripe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect existing" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    expect(window.open).toHaveBeenCalledWith("https://connect.stripe.com/setup/x", "_blank", "noopener");
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
  });

  it("does not approve when the popup is blocked", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(approve).not.toHaveBeenCalled();
  });
});
