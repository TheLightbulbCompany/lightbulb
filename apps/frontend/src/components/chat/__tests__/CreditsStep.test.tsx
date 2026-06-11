import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CreditsStep } from "../CreditsStep";

const replaceMock = vi.fn();
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const mockBalance = vi.fn();
const mockStartTopUp = vi.fn();
const mockRefresh = vi.fn();
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => {
    const balance = mockBalance();
    return {
      balance,
      // Mirrors the real hook: false only when the backend explicitly says so.
      lifecycleEnabled: balance ? balance.lifecycle_enabled !== false : undefined,
      startTopUp: mockStartTopUp,
      refresh: mockRefresh,
    };
  },
}));

describe("CreditsStep", () => {
  beforeEach(() => {
    mockBalance.mockReset();
    mockStartTopUp.mockReset();
    mockRefresh.mockReset();
    replaceMock.mockReset();
    searchParams = new URLSearchParams();
  });

  it("auto-advances when the owner already has a positive balance", () => {
    const onComplete = vi.fn();
    mockBalance.mockReturnValue({ balance_dollars: "10.00", balance_microcents: 10_000_000 });
    render(<CreditsStep onComplete={onComplete} />);
    expect(onComplete).toHaveBeenCalled();
  });

  it("latches the submit button after a successful request", async () => {
    // The payment collects asynchronously — re-enabling after success would
    // invite a second identical submit (= a second card charge).
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockResolvedValueOnce({ status: "requested", amount_cents: 2000 });
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    const latched = await screen.findByRole("button", { name: /top-up requested/i });
    expect(latched).toBeDisabled();
    expect(screen.getByText(/can take a few minutes to land/i)).toBeInTheDocument();
    fireEvent.click(latched);
    expect(mockStartTopUp).toHaveBeenCalledTimes(1);
  });

  it("stays retryable after a failed request", async () => {
    mockBalance.mockReturnValue({ balance_dollars: "0.00", balance_microcents: 0 });
    mockStartTopUp.mockRejectedValueOnce(new Error("billing offline"));
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i }));
    await waitFor(() => expect(screen.getByText("billing offline")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
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
    // wizard then falls back to the provider picker / normal flow.
    searchParams = new URLSearchParams({ provider: "bedrock_claude", checkout: "success" });
    mockBalance.mockReturnValue({
      balance_dollars: null,
      balance_microcents: null,
      lifecycle_enabled: false,
    });
    render(<CreditsStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /choose a different provider/i }));
    expect(replaceMock).toHaveBeenCalledWith("/chat?checkout=success", { scroll: false });
  });
});
