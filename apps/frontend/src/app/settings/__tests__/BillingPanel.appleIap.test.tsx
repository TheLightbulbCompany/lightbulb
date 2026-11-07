import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel Apple-IAP branch (settings/page.tsx): a
// workspace billed through the App Store (subscription_source === "apple_iap")
// shows the plan + App Store management line instead of any Stripe control —
// no portal button, no PlanChooser (subscribe/change-plan/top-up). Backend
// already 409s web checkout for this case (routers/billing.py "apple_managed").
// Harness copied from ./BillingPanel.billing.test.tsx.

const nav = vi.hoisted(() => ({ panel: "billing" as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
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
  openPortal: vi.fn().mockResolvedValue(undefined),
  refresh: vi.fn(),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: billing.openPortal,
    startCheckout: billing.startCheckout,
    refresh: billing.refresh,
  }),
}));

import { PANELS } from "@/components/settings/SettingsPlace";

const SettingsPage = PANELS.billing;

describe("BillingPanel — Apple IAP billing", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = { role: "org:admin" };
    billing.isLoading = false;
    billing.startCheckout = vi.fn().mockResolvedValue(undefined);
    billing.openPortal = vi.fn().mockResolvedValue(undefined);
  });

  it("shows the plan + App Store line and no Stripe controls", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      plan: "standard",
      subscription_source: "apple_iap",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(
      screen.getByText(
        "Billed through the App Store. Change or cancel on your iPhone: Settings → Apple Account → Subscriptions.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Team/)).toBeInTheDocument();

    // No Stripe portal button.
    expect(screen.queryByRole("button", { name: /manage payment/i })).not.toBeInTheDocument();
    // No PlanChooser (subscribe / change-plan / top-up controls).
    expect(screen.queryByTestId("plan-chooser")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /choose|upgrade to|switch to/i })).not.toBeInTheDocument();
  });

  it("an Apple subscription in billing retry (past_due) still shows the App Store state", () => {
    // past_due is not "subscribed", but it is still Apple's subscription to
    // fix — the Stripe portal and a web checkout would both be wrong here.
    billing.isSubscribed = false;
    billing.account = {
      is_subscribed: false,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "past_due",
      plan: "base",
      subscription_source: "apple_iap",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByText(/Billed through the App Store/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /manage payment/i })).not.toBeInTheDocument();
    expect(screen.queryByTestId("plan-chooser")).not.toBeInTheDocument();
  });

  it("a Stripe-billed workspace (subscription_source null) still shows the portal button", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      plan: "standard",
      subscription_source: null,
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();
    expect(screen.queryByText(/App Store/)).not.toBeInTheDocument();
  });
});
