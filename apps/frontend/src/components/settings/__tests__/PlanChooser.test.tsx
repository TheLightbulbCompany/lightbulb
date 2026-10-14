import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import type { BillingAccount } from "@/hooks/useBilling";
import { PlanChooser } from "../PlanChooser";

const prices = { workspace_price_usd: 50, seat_price_usd: 20, included_members: 5 };

describe("PlanChooser: one plan", () => {
  it("offers one Team plan with the seat price, and no Base or Ultra", async () => {
    const user = userEvent.setup();
    const startCheckout = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm");
    render(<PlanChooser account={{ ...prices } as BillingAccount} subscribed={false} startCheckout={startCheckout} />);

    expect(screen.getByText("$50 a month for up to 5 people. $20 a month for each member from the 6th.")).toBeInTheDocument();
    expect(screen.queryByText(/Ultra|Base/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Continue for $50 a month" }));
    expect(confirm).not.toHaveBeenCalled();
    expect(startCheckout).toHaveBeenCalledWith("workspace", false);
  });

  it("marks Team current for a workspace already on it", () => {
    render(
      <PlanChooser account={{ ...prices, plan: "standard" } as BillingAccount} subscribed startCheckout={vi.fn()} />,
    );
    expect(screen.getByText("Current plan")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("moves a retired plan to Team only after the owner confirms the prorated change", async () => {
    const user = userEvent.setup();
    const startCheckout = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <PlanChooser account={{ ...prices, plan: "ultra_pro" } as BillingAccount} subscribed startCheckout={startCheckout} />,
    );

    await user.click(screen.getByRole("button", { name: "Switch to Team" }));
    expect(confirm.mock.calls[0][0]).toMatch(/\$50\/mo.*next invoice/s);
    expect(startCheckout).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Switch to Team" }));
    expect(startCheckout).toHaveBeenCalledWith("workspace", true);
  });
});
