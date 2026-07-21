import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel Ultra Pro upgrade CTA (settings/page.tsx):
// a subscribed owner on the standard plan sees an "Upgrade to Ultra Pro —
// $150/mo" button that calls useBilling().upgradeToUltra on click; an owner
// already on ultra_pro sees an "Ultra Pro" active badge and no upgrade button;
// the per-seat team card never shows the upgrade button (Ultra rejects team
// subs). Harness mirrors ./BillingPanel.billing.test.tsx.

const nav = vi.hoisted(() => ({ panel: "billing" as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
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
  upgradeToUltra: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: vi.fn(),
    startTeamCheckout: vi.fn(),
    upgradeToUltra: billing.upgradeToUltra,
    refresh: vi.fn(),
  }),
}));

import SettingsPage from "../page";

describe("BillingPanel — Ultra Pro upgrade CTA", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = null;
    billing.isLoading = false;
    billing.upgradeToUltra = vi.fn().mockResolvedValue(undefined);
  });

  it("standard subscribed personal owner sees the upgrade button; clicking calls upgradeToUltra", async () => {
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
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    const btn = screen.getByRole("button", { name: /upgrade to ultra pro/i });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveTextContent("$150");

    // The existing $50 card + Manage Payment button stay intact.
    expect(screen.getByText("Isol8 — $50 / month")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(btn);
    });
    expect(billing.upgradeToUltra).toHaveBeenCalledTimes(1);
  });

  it("ultra_pro owner sees the Ultra Pro badge and no upgrade button", () => {
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
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    expect(screen.getByText("Ultra Pro")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /upgrade to ultra pro/i }),
    ).not.toBeInTheDocument();
  });

  it("per-seat team card does not show the upgrade button (Ultra rejects team subs)", () => {
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
      seat_count: 4,
      team_seat_price_usd: 20,
      is_seat_subscription: true,
    };

    render(<SettingsPage />);

    // TeamBillingCard is present, but no Ultra upgrade button.
    expect(
      screen.queryByRole("button", { name: /upgrade to ultra pro/i }),
    ).not.toBeInTheDocument();
  });
});
