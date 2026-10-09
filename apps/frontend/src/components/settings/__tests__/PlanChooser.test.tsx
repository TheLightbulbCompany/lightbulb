import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import type { BillingAccount } from "@/hooks/useBilling";
import { PlanChooser } from "../PlanChooser";

const account = { plan: "standard", base_price_usd: 20, workspace_price_usd: 50, ultra_price_usd: 150 } as BillingAccount;

describe("PlanChooser on a subscribed workspace", () => {
  it("changes the plan only after the owner confirms the prorated charge", async () => {
    const user = userEvent.setup();
    const startCheckout = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<PlanChooser account={account} subscribed startCheckout={startCheckout} />);

    await user.click(screen.getByRole("button", { name: "Upgrade to Ultra" }));
    expect(confirm.mock.calls[0][0]).toMatch(/\$150\/mo.*next invoice/s);
    expect(startCheckout).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Upgrade to Ultra" }));
    expect(startCheckout).toHaveBeenCalledWith("ultra", true);
  });

  it("a first subscription goes straight to Checkout with no confirm", async () => {
    const user = userEvent.setup();
    const startCheckout = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm");
    render(<PlanChooser account={account} subscribed={false} startCheckout={startCheckout} />);

    await user.click(screen.getByRole("button", { name: "Choose Ultra" }));
    expect(confirm).not.toHaveBeenCalled();
    expect(startCheckout).toHaveBeenCalledWith("ultra", false);
  });
});
