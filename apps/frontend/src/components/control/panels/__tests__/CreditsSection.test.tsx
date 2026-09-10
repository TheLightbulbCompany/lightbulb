import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { CreditsSection } from "../CreditsSection";

const mockUseCredits = vi.fn();
vi.mock("@/hooks/useCredits", () => ({ useCredits: () => mockUseCredits() }));

describe("CreditsSection", () => {
  beforeEach(() => {
    mockUseCredits.mockReset();
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      openBilling: vi.fn(),
      canManageCredits: true,
      pendingTopUp: null,
    });
  });

  it("renders the balance and payment-gated top-up CTA without a page heading", () => {
    render(<CreditsSection />);
    expect(screen.getByText("$39.52")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /add \$20/i }),
    ).toBeInTheDocument();
    // No standalone page <h2> — the section embeds inside Model & Credits.
    expect(screen.queryByRole("heading", { name: /claude credits/i })).toBeNull();
  });

  it("keeps the balance visible but makes top-ups read-only for members", () => {
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      openBilling: vi.fn(),
      canManageCredits: false,
      pendingTopUp: { amount_cents: 2000, state: "action_required" },
    });
    render(<CreditsSection />);
    expect(screen.getByText("$39.52")).toBeInTheDocument();
    expect(screen.getByText("Only workspace admins can add credits.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add \$20/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open billing" })).toBeNull();
  });

  it.each(["success", "failure"])("ignores a late %s POST from the previous workspace", async (outcome) => {
    let finishA!: () => void;
    const startTopUp = vi.fn().mockImplementationOnce(() => new Promise((resolve, reject) => {
      finishA = () => outcome === "success" ? resolve({ status: "requested" }) : reject(new Error("A payment error"));
    })).mockImplementationOnce(() => new Promise(() => {}));
    const credits = { ...mockUseCredits(), scopeKey: "user_A:org_A", startTopUp };
    mockUseCredits.mockReturnValue(credits);
    const mounted = render(<CreditsSection />);
    fireEvent.click(screen.getByRole("button", { name: "$50" }));
    fireEvent.click(screen.getByRole("button", { name: "Add $50" }));
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();

    mockUseCredits.mockReturnValue({ ...credits, scopeKey: "user_B:org_B" });
    mounted.rerender(<CreditsSection />);
    expect(screen.getByRole("button", { name: "Add $20" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Add $20" }));
    await act(async () => { finishA(); });
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
    expect(screen.queryByText("A payment error")).toBeNull();
    expect(screen.queryByText(/Top-up of \$/)).toBeNull();
    expect(credits.refresh).not.toHaveBeenCalled();
  });

  it("resets a previous workspace's payment error", async () => {
    const credits = { ...mockUseCredits(), scopeKey: "user_A:org_A", startTopUp: vi.fn().mockRejectedValue(new Error("A payment error")) };
    mockUseCredits.mockReturnValue(credits);
    const mounted = render(<CreditsSection />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add $20" })); });
    expect(screen.getByText("A payment error")).toBeInTheDocument();
    mockUseCredits.mockReturnValue({ ...credits, scopeKey: "user_A:org_B" });
    mounted.rerender(<CreditsSection />);
    expect(screen.queryByText("A payment error")).toBeNull();
    expect(screen.getByRole("button", { name: "$50" })).not.toBeDisabled();
  });

  it.each(["requested", "action_required"])("shows a persisted %s top-up as locked after a reload", async (state) => {
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      openBilling: vi.fn(),
      canManageCredits: true,
      pendingTopUp: { amount_cents: 2000, state },
    });
    render(<CreditsSection />);
    expect(await screen.findByRole("button", { name: "Top-up requested" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
  });

  it("allows only the exact pending request to be retried", async () => {
    const startTopUp = vi.fn().mockResolvedValue({ status: "requested", amount_cents: 2000 });
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 0, balance_dollars: "0.00" },
      refresh: vi.fn(),
      startTopUp,
      openBilling: vi.fn(),
      canManageCredits: true,
      pendingTopUp: { amount_cents: 2000, state: "pending" },
    });
    render(<CreditsSection />);
    const retry = await screen.findByRole("button", { name: "Retry $20" });
    expect(retry).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
    await act(async () => { fireEvent.click(retry); });
    expect(startTopUp).toHaveBeenCalledWith(2000);
  });

  it("keeps a payment action-required top-up locked", async () => {
    const openBilling = vi.fn();
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      openBilling,
      canManageCredits: true,
      pendingTopUp: { amount_cents: 2000, state: "action_required" },
    });
    render(<CreditsSection />);
    expect(await screen.findByRole("button", { name: "Top-up requested" })).toBeDisabled();
    expect(screen.getByText("Payment needs attention. Update your payment method in billing.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open billing" }));
    expect(openBilling).toHaveBeenCalledOnce();
  });
});
