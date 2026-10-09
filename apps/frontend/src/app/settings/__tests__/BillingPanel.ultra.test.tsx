import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel Ultra upgrade CTA (settings/page.tsx):
// a subscribed owner on the standard plan sees an "Upgrade to Ultra —
// $150/mo" button that calls useBilling().startCheckout("ultra") on click; an owner
// already on ultra_pro sees an "Ultra" active badge and no upgrade button.
// Workspace owners use the same CTA. Harness mirrors ./BillingPanel.billing.test.tsx.

const nav = vi.hoisted(() => ({ panel: "billing" as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
  // The page forwards a ?panel= naming an href-only nav row (Connections) to
  // that route; no test here uses one, so a no-op replace suffices.
  useRouter: () => ({ replace: vi.fn() }),
}));

const clerk = vi.hoisted(() => ({
  membership: null as { role: string } | null,
}));

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: {
      fullName: "Test User",
      firstName: "Test",
      lastName: "User",
      primaryEmailAddress: { emailAddress: "test@isol8.co" },
    },
  }),
  useAuth: () => ({ getToken: () => Promise.resolve("jwt"), isLoaded: true }),
  useOrganization: () => ({ membership: clerk.membership, organization: null }),
  UserButton: () => <div data-testid="user-button" />,
}));

const billing = vi.hoisted(() => ({
  account: null as Record<string, unknown> | null,
  isLoading: false,
  isSubscribed: false,
  startCheckout: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: vi.fn(),
    startCheckout: billing.startCheckout,
    refresh: vi.fn(),
  }),
}));

import SettingsPage from "../page";

describe("BillingPanel — Ultra upgrade CTA", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = null;
    billing.isLoading = false;
    billing.startCheckout = vi.fn().mockResolvedValue(undefined);
  });

  it("standard subscribed workspace owner sees the upgrade button; clicking starts the ultra checkout", async () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      plan: "standard",
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    const btn = screen.getByRole("button", { name: /upgrade to ultra/i });
    expect(screen.getByTestId("plan-ultra")).toHaveTextContent("$150/mo");
    // Workspace is current; Base is still offered as a switch.
    expect(screen.getByTestId("plan-workspace")).toHaveTextContent("Current plan");
    expect(screen.getByRole("button", { name: "Switch to Base" })).toBeInTheDocument();

    // The existing $50 card + Manage Payment button stay intact.
    expect(screen.getByText("Lightbulb — $50 / month")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();

    // A live plan change asks first (the prorated difference lands on the next invoice).
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(billing.startCheckout).toHaveBeenCalledWith("ultra", true);
  });

  it("ultra_pro owner sees the Ultra badge and no upgrade button", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      plan: "ultra_pro",
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByTestId("plan-ultra")).toHaveTextContent("Current plan");
    expect(
      screen.queryByRole("button", { name: /upgrade to ultra/i }),
    ).not.toBeInTheDocument();
  });

  it("a workspace admin can upgrade the standard workspace to Ultra", () => {
    clerk.membership = { role: "org:admin" };
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      plan: "standard",
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /upgrade to ultra/i })).toBeInTheDocument();
  });

  it("a Base owner sees Base current and both upgrades, Workspace included", () => {
    clerk.membership = { role: "org:admin" };
    billing.isSubscribed = true;
    billing.account = {
      subscription_status: "active",
      plan: "base",
      base_price_usd: 20,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };
    render(<SettingsPage />);
    expect(screen.getByTestId("plan-base")).toHaveTextContent("Current plan");
    expect(screen.getByRole("button", { name: "Upgrade to Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upgrade to Ultra" })).toBeInTheDocument();
  });
});
