import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";

import { ProviderPicker } from "../ProvisioningStepper";

const billing = vi.hoisted(() => ({
  startCheckout: vi.fn().mockResolvedValue(undefined),
  account: { workspace_price_usd: 75, ultra_price_usd: 150, is_subscribed: false, managed_inference_available: false },
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post: vi.fn() }) }));

describe("ProviderPicker", () => {
  beforeEach(() => {
    billing.account.is_subscribed = false;
    billing.account.managed_inference_available = false;
  });
  it("discloses the server price and that provider selection follows checkout", () => {
    render(<ProviderPicker isOrg={true} orgName="Acme" />);

    expect(screen.getAllByText("$75 / month")).toHaveLength(2);
    expect(screen.getByText(/Choose your inference provider after checkout/)).toBeInTheDocument();
    expect(screen.getByText(/separate ChatGPT subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/provider bills inference separately/i)).toBeInTheDocument();
    expect(screen.queryByText(/Claude credits — no second step/)).not.toBeInTheDocument();
  });

  it("has one workspace checkout action without provider or credit intent", () => {
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getAllByRole("button", { name: /subscribe to workspace plan/i })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /subscribe to workspace plan/i }));

    expect(billing.startCheckout).toHaveBeenCalledWith("workspace");
  });

  it("shows managed workspace credits to a subscribed owner when available", () => {
    billing.account.is_subscribed = true;
    billing.account.managed_inference_available = true;
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getByText("Lightbulb-managed models")).toBeInTheDocument();
    expect(screen.queryByText("Powered by Claude")).toBeNull();
  });

  it("keeps the existing comparison before billing even if the capability is reported", () => {
    billing.account.managed_inference_available = true;
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.queryByText("Lightbulb-managed models")).toBeNull();
  });
});
