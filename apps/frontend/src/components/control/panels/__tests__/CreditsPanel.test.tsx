import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CreditsPanel } from "../CreditsPanel";

const mockBalance = vi.fn();
const mockStartTopUp = vi.fn();
const mockRefresh = vi.fn();
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => ({
    balance: mockBalance(),
    startTopUp: mockStartTopUp,
    refresh: mockRefresh,
  }),
}));

// Top-ups are Metronome payment-gated commits charged to the saved card —
// there is no Stripe Checkout redirect, no Stripe SDK in the bundle, and no
// publishable key. The panel stays put and shows a "requested" confirmation.

describe("CreditsPanel", () => {
  beforeEach(() => {
    mockBalance.mockReset();
    mockStartTopUp.mockReset();
    mockRefresh.mockReset();
  });

  it("renders the BALANCE eyebrow + dollar balance from the hook", () => {
    mockBalance.mockReturnValue({ balance_dollars: "12.50", balance_microcents: 12_500_000 });
    render(<CreditsPanel />);
    expect(screen.getByText("BALANCE")).toBeInTheDocument();
    expect(screen.getByText("$12.50")).toBeInTheDocument();
  });

  it("shows a dash placeholder before the balance loads", () => {
    mockBalance.mockReturnValue(null);
    render(<CreditsPanel />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("shows the unavailable note when Metronome has no balance to report", () => {
    mockBalance.mockReturnValue({ balance_dollars: null, balance_microcents: null });
    render(<CreditsPanel />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/balance unavailable/i)).toBeInTheDocument();
  });

  it("toggles the active style on quick-pick buttons", () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    render(<CreditsPanel />);
    const fiftyButton = screen.getByRole("button", { name: /\$50/ });
    fireEvent.click(fiftyButton);
    expect(fiftyButton.className).toContain("border-[#06402B]");
  });

  it("calls startTopUp with the selected amount when Add is clicked", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({ status: "requested", amount_cents: 5000 });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /\$50/ }));
    fireEvent.click(screen.getByRole("button", { name: /add \$50/i }));
    await waitFor(() => expect(mockStartTopUp).toHaveBeenCalledWith(5000));
  });

  it("shows the requested confirmation and refreshes the balance on success", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({ status: "requested", amount_cents: 2000 });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() =>
      expect(screen.getByText(/top-up of \$20 requested/i)).toBeInTheDocument(),
    );
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("calls refresh when the refresh icon is clicked", () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("surfaces a top-up error and stays retryable", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockRejectedValueOnce(new Error("billing offline"));
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() => expect(screen.getByText("billing offline")).toBeInTheDocument());
    // Submitting cleared — the Add button label flips back from "Loading…"
    // so the user can retry instead of being stuck.
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
  });

  it("treats a non-requested response as an error", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({});
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() =>
      expect(screen.getByText("Top-up was not accepted")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
  });
});
