import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";

import { ProviderPicker } from "../ProvisioningStepper";

const billing = vi.hoisted(() => ({
  startCheckout: vi.fn().mockResolvedValue(undefined),
  account: { workspace_price_usd: 75, ultra_price_usd: 150 },
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post: vi.fn() }) }));

describe("ProviderPicker", () => {
  it("discloses the server price and that provider selection follows checkout", () => {
    render(<ProviderPicker isOrg={true} orgName="Acme" />);

    expect(screen.getAllByText("$75 / month")).toHaveLength(3);
    expect(screen.getByText(/Choose your inference provider after checkout/)).toBeInTheDocument();
    expect(screen.queryByText(/Claude credits — no second step/)).not.toBeInTheDocument();
  });

  it("has one workspace checkout action without provider or credit intent", () => {
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getAllByRole("button", { name: /subscribe to workspace plan/i })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /subscribe to workspace plan/i }));

    expect(billing.startCheckout).toHaveBeenCalledWith("workspace");
  });
});
