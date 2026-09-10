import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreditsStep } from "../CreditsStep";

const balance = vi.fn();
const startTopUp = vi.fn();
const refresh = vi.fn();
const canManageCredits = vi.fn();
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => ({ balance: balance(), startTopUp, refresh, canManageCredits: canManageCredits(), pendingTopUp: null }),
  isCreditsExhausted: (value: { is_exhausted?: boolean; balance_microcents: number | null }) =>
    value.is_exhausted ?? (value.balance_microcents !== null && value.balance_microcents <= 0),
}));

describe("CreditsStep", () => {
  beforeEach(() => {
    balance.mockReset(); startTopUp.mockReset(); refresh.mockReset(); canManageCredits.mockReset();
    balance.mockReturnValue({ balance_microcents: 0, balance_dollars: "0.00" });
    canManageCredits.mockReturnValue(true);
  });

  it("pins the selected amount and disables controls after an uncertain request", async () => {
    startTopUp.mockResolvedValue({ status: "requested", amount_cents: 2000 });
    render(<CreditsStep onComplete={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i })); });
    await screen.findByRole("button", { name: "Payment requested" });
    expect(screen.getByRole("button", { name: "Payment requested" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
    expect(screen.getByRole("spinbutton")).toBeDisabled();
    expect(startTopUp).toHaveBeenCalledWith(2000);
  });

  it("keeps the same amount retryable when the request itself fails", async () => {
    startTopUp.mockRejectedValueOnce(new Error("billing unavailable")).mockResolvedValueOnce({ status: "requested" });
    render(<CreditsStep onComplete={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i })); });
    await waitFor(() => expect(screen.getByText("billing unavailable")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
    // A failed response can still mean the server accepted the POST. Keep the
    // amount locked so the stable UUID can only replay the original intent.
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^add \$20$/i })); });
    expect(startTopUp).toHaveBeenLastCalledWith(2000);
  });

  it("is read-only for a non-admin workspace member", () => {
    canManageCredits.mockReturnValue(false);
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(screen.getByText("Only workspace admins can add credits.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^add \$/i })).toBeNull();
  });
});
