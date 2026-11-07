import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel workspace branch (SettingsPlace.tsx):
// subscribed workspaces render one plan card, while unsubscribed workspaces
// get the workspace-billing CTA. There is no membership-priced branch.
//
// Mocking harness copied from ../page.nav.test.tsx: mock next/navigation's
// useSearchParams (deep-link into ?panel=billing), @clerk/nextjs's
// useOrganization membership, and @/hooks/useBilling's account shape.
// The Control panels stubbed there are unrelated to billing and are
// irrelevant here since we never leave the "billing" panel — they are not
// mocked in this file, matching how billing-route.test.tsx also stays
// minimal for its own surface.

const nav = vi.hoisted(() => ({ panel: "billing" as string | null, checkout: null as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams({ ...(nav.panel ? { panel: nav.panel } : {}), ...(nav.checkout ? { checkout: nav.checkout } : {}) }),
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
  refresh: vi.fn(),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: vi.fn(),
    startCheckout: billing.startCheckout,
    refresh: billing.refresh,
  }),
}));

import { PANELS } from "@/components/settings/SettingsPlace";

const SettingsPage = PANELS.billing;

describe("BillingPanel — workspace billing", () => {
  beforeEach(() => {
    nav.panel = "billing";
    nav.checkout = null;
    clerk.membership = { role: "org:admin" };
    billing.isLoading = false;
    billing.refresh.mockClear();
  });

  it("refreshes billing immediately when Settings is the Checkout success target", () => {
    nav.checkout = "success";
    billing.isSubscribed = false;
    billing.account = { subscription_status: "trial_local", workspace_price_usd: 50 };
    render(<SettingsPage />);
    expect(billing.refresh).toHaveBeenCalled();
  });

  it("a subscribed workspace shows the plain plan card, not membership pricing", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 12.3456,
      lifetime_spend: 45.6,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    // No per-seat paywall math anywhere on the panel.
    expect(screen.queryByText(/seats? ×/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$20/)).not.toBeInTheDocument();
    expect(screen.queryByText(/teams billing/i)).not.toBeInTheDocument();

    // The plain subscribed plan card renders instead.
    expect(screen.getByText("Lightbulb plan")).toBeInTheDocument();
    expect(screen.getByText("Active subscription")).toBeInTheDocument();

    // The spend line was removed from the settings billing card (provider-
    // aware spend now lives on the dashboard Month Spend card / Usage panel).
    expect(screen.queryByText(/Current period spend/i)).not.toBeInTheDocument();
  });

  it("org status chip renders the humanized planLabel, not the raw enum", () => {
    // Regression guard for the org-context status chip: it used to render
    // `{subscriptionStatus}` directly under `textTransform: capitalize`,
    // which can't fix an underscore — "past_due" rendered "Past_due" (only
    // the first character capitalizes; there's no whitespace word boundary).
    // Routing through the shared `statusChipLabel`/`planLabel` — the same
    // source the personal card uses (:504-507) — humanizes the underscore
    // away so the chip reads "Past due".
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "past_due",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByText("Lightbulb plan")).toBeInTheDocument();
    expect(screen.getByText("Past due")).toBeInTheDocument();
    expect(screen.queryByText("past_due")).not.toBeInTheDocument();
  });

  it("a subscribed workspace renders the plan card without membership pricing", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();
    expect(screen.getByText("Lightbulb plan")).toBeInTheDocument();
    expect(screen.queryByText(/seats?/i)).not.toBeInTheDocument();
  });

  it("unsubscribed org renders the plan chooser", () => {
    billing.isSubscribed = false;
    billing.account = null;

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /^continue/i })).toBeInTheDocument();
    expect(screen.queryByText("Lightbulb plan")).not.toBeInTheDocument();
  });

  it("a non-admin member of a subscribed workspace sees no billing controls", () => {
    clerk.membership = { role: "org:member" };
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 12.3456,
      lifetime_spend: 45.6,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    // No spend figures anywhere on the panel for a non-admin member.
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Current period spend/i)).not.toBeInTheDocument();

    // No billing controls either.
    expect(screen.queryByRole("button", { name: /manage/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).not.toBeInTheDocument();

    // Restricted messaging instead.
    expect(screen.getByText(/billing restricted/i)).toBeInTheDocument();
  });

  it("a non-admin member sees the same restriction regardless of workspace membership", () => {
    clerk.membership = { role: "org:member" };
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /manage/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).not.toBeInTheDocument();
    expect(screen.getByText(/billing restricted/i)).toBeInTheDocument();
  });

  it("admin of a subscribed workspace sees the plan card without a spend line", () => {
    clerk.membership = { role: "org:admin" };
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 3.14,
      lifetime_spend: 99.5,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      ultra_price_usd: 150,
    };

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();
    expect(screen.getByText("Lightbulb plan")).toBeInTheDocument();
    expect(screen.queryByText(/seats?/i)).not.toBeInTheDocument();

    // The spend line was removed from the settings billing card.
    expect(screen.queryByText(/Current period spend/i)).not.toBeInTheDocument();
  });
});

describe("BillingPanel — org trial_local conversion", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = { role: "org:admin" };
    billing.isLoading = false;
    billing.startCheckout = vi.fn().mockResolvedValue(undefined);
  });

  it("offers the one plan, with no second stray upgrade button", async () => {
    billing.isSubscribed = false;
    billing.account = {
      is_subscribed: false,
      current_spend: 0.5,
      lifetime_spend: 0.5,
      subscription_status: "trial_local",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      workspace_price_usd: 50,
      seat_price_usd: 20,
      included_members: 5,
    };

    render(<SettingsPage />);

    // One plan, not marked current on the trial; its button starts it.
    expect(screen.getByTestId("plan-chooser")).toBeInTheDocument();
    expect(screen.queryByText("Current plan")).toBeNull();
    expect(screen.queryByText(/Ultra|Base/)).toBeNull();
    expect(screen.queryByRole("button", { name: /subscribe workspace|upgrade to base/i })).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Continue for $50 a month" }));
    });
    expect(billing.startCheckout).toHaveBeenCalledWith("workspace", false);
    // The Bedrock card is gone (#2932); managed inference shows only when available.
    expect(screen.queryByText(/managed Claude inference/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Create or select a workspace/i)).not.toBeInTheDocument();
  });

  it.each([
    [null, "Free trial"],
    [Math.floor(Date.now() / 1000) + 2.5 * 86_400, "Free trial — 3 days left"],
    [Math.floor(Date.now() / 1000) - 60, "This workspace is reading-only"],
  ])("shows the free-trial window (trial_end %s)", (trialEnd, title) => {
    billing.isSubscribed = false;
    billing.account = { subscription_status: "trial_local", trial_end: trialEnd, workspace_price_usd: 50 };
    render(<SettingsPage />);
    expect(screen.getByTestId("trial-status")).toHaveTextContent(title);
  });
});
