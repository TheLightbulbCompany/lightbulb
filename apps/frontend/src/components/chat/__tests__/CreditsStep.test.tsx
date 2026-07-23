import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

import { CreditsStep } from "../CreditsStep";

const replaceMock = vi.fn();
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const mockBalance = vi.fn();
const mockStartTopUp = vi.fn();
const mockStartTopUpCheckout = vi.fn();
const mockRefresh = vi.fn();
// The one-click submit path only renders for owners who previously opted in
// (autoChargeEnabled); everyone else gets the Stripe Checkout hand-off.
// Tests flip this per-case to target the path under test.
let mockAutoChargeEnabled = false;
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => {
    const balance = mockBalance();
    return {
      balance,
      // Mirrors the real hook: false only when the backend explicitly says so.
      lifecycleEnabled: balance ? balance.lifecycle_enabled !== false : undefined,
      autoChargeEnabled: mockAutoChargeEnabled,
      startTopUp: mockStartTopUp,
      startTopUpCheckout: mockStartTopUpCheckout,
      refresh: mockRefresh,
    };
  },
}));

describe("CreditsStep", () => {
  beforeEach(() => {
    mockBalance.mockReset();
    mockStartTopUp.mockReset();
    mockStartTopUpCheckout.mockReset();
    mockRefresh.mockReset();
    mockAutoChargeEnabled = false;
    replaceMock.mockReset();
    searchParams = new URLSearchParams();
  });

  it("auto-advances when the owner already has a positive balance", () => {
    const onComplete = vi.fn();
    mockBalance.mockReturnValue({ balance_dollars: "10.00", balance_microcents: 10_000_000 });
    render(<CreditsStep onComplete={onComplete} />);
    expect(onComplete).toHaveBeenCalled();
  });

  it("latches the submit button after a successful one-click request", async () => {
    // The payment collects asynchronously — re-enabling after success would
    // invite a second identical submit (= a second card charge).
    mockAutoChargeEnabled = true;
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({ status: "requested", amount_cents: 2000 });
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    const latched = await screen.findByRole("button", { name: /payment requested/i });
    expect(latched).toBeDisabled();
    expect(screen.getByText(/advances automatically once the credits land/i)).toBeInTheDocument();
    fireEvent.click(latched);
    expect(mockStartTopUp).toHaveBeenCalledTimes(1);
  });

  it("stays retryable after a failed one-click request", async () => {
    mockAutoChargeEnabled = true;
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockRejectedValueOnce(new Error("billing offline"));
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() => expect(screen.getByText("billing offline")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
  });

  it("un-sticks the latched button with an error if credits never land within the grace window", async () => {
    // The charge was ACCEPTED ("requested") but silently fails to collect (e.g.
    // a later decline), so no balance ever arrives. Without the timebox the
    // button would read "Payment requested" forever; after the grace window it
    // re-enables with a retryable error.
    vi.useFakeTimers();
    try {
      mockAutoChargeEnabled = true;
      mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
      mockStartTopUp.mockResolvedValueOnce({ status: "requested", amount_cents: 2000 });
      render(<CreditsStep onComplete={vi.fn()} />);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
        // Flush the startTopUp resolution so the `requested` latch commits.
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByRole("button", { name: /payment requested/i })).toBeDisabled();

      // Grace window elapses with no balance change -> un-stick + error.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(90_000);
      });
      expect(screen.getByText(/taking longer than expected/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("hands off to Stripe Checkout when one-click is not enabled", () => {
    // Default path: no card-on-file opt-in → the submit is a Checkout
    // hand-off, never an implicit charge.
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /^add \$/i })).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue to payment — \$20/i }),
    ).toBeInTheDocument();
  });

  it("renders the credits-unavailable state instead of the form when the lifecycle is off", () => {
    mockBalance.mockReturnValue({
      balance_dollars: null,
      balance_microcents: null,
      lifecycle_enabled: false,
    });
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(screen.getByText(/not yet available in this environment/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^add \$/i })).not.toBeInTheDocument();
  });

  it("strips ?provider= when choosing a different provider, preserving other params", () => {
    // Stripping the provider param is the stepper's exit mechanism — the
    // wizard then falls back to the provider picker / normal flow. The strip
    // commits through the History API (shellNav), not the router.
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    searchParams = new URLSearchParams({ provider: "bedrock_claude", checkout: "success" });
    mockBalance.mockReturnValue({
      balance_dollars: null,
      balance_microcents: null,
      lifecycle_enabled: false,
    });
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /choose a different provider/i }));
    const target = replaceStateSpy.mock.calls.at(-1)?.[2] as string;
    expect(target).not.toMatch(/provider=/);
    expect(target).toMatch(/checkout=success/);
    replaceStateSpy.mockRestore();
  });
});
