import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreditsStep } from "../CreditsStep";

const balance = vi.fn();
const startTopUp = vi.fn();
const refresh = vi.fn();
const canManageCredits = vi.fn();
const pendingTopUp = vi.fn();
const topUpOutcome = vi.fn();
let scopeKey = "user_A:org_A";
vi.mock("@/hooks/useCredits", () => ({
  useCredits: () => ({ scopeKey, balance: balance(), startTopUp, refresh, canManageCredits: canManageCredits(), pendingTopUp: pendingTopUp(), topUpOutcome: topUpOutcome() }),
  isCreditsExhausted: (value: { is_exhausted?: boolean; balance_microcents: number | null }) =>
    value.is_exhausted ?? (value.balance_microcents !== null && value.balance_microcents <= 0),
}));

describe("CreditsStep", () => {
  beforeEach(() => {
    scopeKey = "user_A:org_A";
    balance.mockReset(); startTopUp.mockReset(); refresh.mockReset(); canManageCredits.mockReset(); pendingTopUp.mockReset(); topUpOutcome.mockReset();
    balance.mockReturnValue({ balance_microcents: 0, balance_dollars: "0.00" });
    canManageCredits.mockReturnValue(true);
    pendingTopUp.mockReturnValue(null);
    topUpOutcome.mockReturnValue(null);
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

  it.each(["success", "failure"])("ignores a late %s POST from the previous workspace", async (outcome) => {
    let finishA!: () => void;
    startTopUp.mockImplementationOnce(() => new Promise((resolve, reject) => {
      finishA = () => outcome === "success" ? resolve({ status: "requested" }) : reject(new Error("A payment error"));
    })).mockImplementationOnce(() => new Promise(() => {}));
    const onComplete = vi.fn();
    const mounted = render(<CreditsStep onComplete={onComplete} />);
    fireEvent.click(screen.getByRole("button", { name: "$50" }));
    fireEvent.click(screen.getByRole("button", { name: "Add $50" }));
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();

    scopeKey = "user_B:org_B";
    mounted.rerender(<CreditsStep onComplete={onComplete} />);
    expect(screen.getByRole("button", { name: "Add $20" })).not.toBeDisabled();
    expect(screen.getByRole("spinbutton")).not.toBeDisabled();
    // B starts its own request before A responds; A's finally must not end B's loading state.
    fireEvent.click(screen.getByRole("button", { name: "Add $20" }));
    refresh.mockClear();
    await act(async () => { finishA(); });
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
    expect(screen.queryByText("A payment error")).toBeNull();
    expect(screen.queryByText(/Payment requested\./)).toBeNull();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("resets a previous workspace's payment error", async () => {
    startTopUp.mockRejectedValue(new Error("A payment error"));
    const onComplete = vi.fn();
    const mounted = render(<CreditsStep onComplete={onComplete} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Add $20" })); });
    expect(screen.getByText("A payment error")).toBeInTheDocument();
    scopeKey = "user_A:org_B";
    mounted.rerender(<CreditsStep onComplete={onComplete} />);
    expect(screen.queryByText("A payment error")).toBeNull();
    expect(screen.getByRole("button", { name: "$50" })).not.toBeDisabled();
  });

  it.each(["pending", "requested", "action_required"])("shows a persisted %s top-up as locked after a reload", async (state) => {
    pendingTopUp.mockReturnValue({ amount_cents: 2000, state });
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Payment requested" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
  });

  it("keeps a payment action-required top-up locked", async () => {
    pendingTopUp.mockReturnValue({ amount_cents: 2000, state: "action_required" });
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Payment requested" })).toBeDisabled();
  });

  it("shows a settled payment failure after unlocking the persisted intent", () => {
    topUpOutcome.mockReturnValue("failed");
    render(<CreditsStep onComplete={vi.fn()} />);
    expect(screen.getByText("Payment failed. Choose an amount to try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^add \$20$/i })).not.toBeDisabled();
  });
});
