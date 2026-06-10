import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CreditsPanel } from "../CreditsPanel";

const mockBalance = vi.fn();
const mockStartTopUp = vi.fn();
const mockSetAutoReload = vi.fn();
const mockRefresh = vi.fn();
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => ({
    balance: mockBalance(),
    startTopUp: mockStartTopUp,
    setAutoReload: mockSetAutoReload,
    refresh: mockRefresh,
  }),
}));

// Top-ups go through Stripe Checkout (full-page redirect via
// window.location.href) — there is no embedded PaymentElement, no Stripe
// SDK in the bundle, and no publishable key. Stub location so the redirect
// is observable instead of navigating the test DOM.
const realLocation = window.location;

describe("CreditsPanel", () => {
  beforeEach(() => {
    mockBalance.mockReset();
    mockStartTopUp.mockReset();
    mockSetAutoReload.mockReset();
    mockRefresh.mockReset();
    Object.defineProperty(window, "location", {
      value: { ...realLocation, href: "http://localhost/chat" },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  it("renders the BALANCE eyebrow + dollar balance from the hook", () => {
    mockBalance.mockReturnValue({ balance_dollars: "12.50", balance_microcents: 12_500_000 });
    render(<CreditsPanel />);
    expect(screen.getByText("BALANCE")).toBeInTheDocument();
    expect(screen.getByText("$12.50")).toBeInTheDocument();
  });

  it("shows $0.00 placeholder when balance is null", () => {
    mockBalance.mockReturnValue(null);
    render(<CreditsPanel />);
    expect(screen.getByText("$0.00")).toBeInTheDocument();
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
    mockStartTopUp.mockResolvedValueOnce({ checkout_url: "https://checkout.stripe.com/c/pay_x" });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /\$50/ }));
    fireEvent.click(screen.getByRole("button", { name: /add \$50/i }));
    await waitFor(() => expect(mockStartTopUp).toHaveBeenCalledWith(5000));
  });

  it("redirects to Stripe Checkout when startTopUp returns a checkout_url", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({ checkout_url: "https://checkout.stripe.com/c/pay_y" });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() =>
      expect(window.location.href).toBe("https://checkout.stripe.com/c/pay_y"),
    );
  });

  it("reveals threshold + amount inputs when Auto-reload is enabled", () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    render(<CreditsPanel />);
    expect(screen.queryByText(/when balance drops below/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByText(/when balance drops below/i)).toBeInTheDocument();
    expect(screen.getByText(/charge me/i)).toBeInTheDocument();
  });

  it("calls setAutoReload with the right payload when Save is clicked", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockSetAutoReload.mockResolvedValueOnce(undefined);
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(mockSetAutoReload).toHaveBeenCalledWith({
        enabled: true,
        threshold_cents: 500,
        amount_cents: 2000,
      }),
    );
  });

  it("calls refresh when the refresh icon is clicked", () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("surfaces a top-up start error and stays on the panel", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockRejectedValueOnce(new Error("billing offline"));
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() => expect(screen.getByText("billing offline")).toBeInTheDocument());
    expect(window.location.href).toBe("http://localhost/chat");
  });

  it("clears submitting state and surfaces error when no checkout_url is returned", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({});
    render(<CreditsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() =>
      expect(screen.getByText("No checkout_url returned")).toBeInTheDocument(),
    );
    // Submitting cleared — the Add button label flips back from "Loading…"
    // so the user can retry instead of being stuck.
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
    expect(window.location.href).toBe("http://localhost/chat");
  });
});
