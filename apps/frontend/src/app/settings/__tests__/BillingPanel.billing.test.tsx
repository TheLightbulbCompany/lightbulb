import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel org-context branch (settings/page.tsx):
// a subscribed org on a flat/comped sub (is_seat_subscription: false) must
// NOT see the per-seat "N seats × $price" paywall math fabricated from a
// nonexistent Stripe subscription item — it should fall through to the
// plain "Isol8 plan / Active" card with the spend line instead. A real
// per-seat sub (is_seat_subscription: true) still gets TeamBillingCard.
//
// Mocking harness copied from ../page.nav.test.tsx: mock next/navigation's
// useSearchParams (deep-link into ?panel=billing), @clerk/nextjs's
// useOrganization membership, and @/hooks/useBilling's account shape.
// The Control panels stubbed there are unrelated to billing and are
// irrelevant here since we never leave the "billing" panel — they are not
// mocked in this file, matching how billing-route.test.tsx also stays
// minimal for its own surface.

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
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: vi.fn(),
    startTeamCheckout: vi.fn(),
  }),
}));

import SettingsPage from "../page";

describe("BillingPanel — org context seat-vs-flat branch", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = { role: "org:admin" };
    billing.isLoading = false;
  });

  it("flat/comped org (subscribed, is_seat_subscription: false) shows the plain plan card, not fabricated per-seat math", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 12.3456,
      lifetime_spend: 45.6,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      seat_count: 7,
      team_seat_price_usd: 20,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    // No per-seat paywall math anywhere on the panel.
    expect(screen.queryByText(/seats? ×/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$20/)).not.toBeInTheDocument();
    expect(screen.queryByText(/teams billing/i)).not.toBeInTheDocument();

    // The plain subscribed plan card renders instead.
    expect(screen.getByText("Isol8 plan")).toBeInTheDocument();
    expect(screen.getByText("Active subscription")).toBeInTheDocument();

    // The spend line was removed from the settings billing card (provider-
    // aware spend now lives on the dashboard Month Spend card / Usage panel).
    expect(screen.queryByText(/Current period spend/i)).not.toBeInTheDocument();
  });

  it("real seat-sub org (is_seat_subscription: true, subscribed) renders TeamBillingCard", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 1,
      lifetime_spend: 2,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      seat_count: 4,
      team_seat_price_usd: 20,
      is_seat_subscription: true,
    };

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /manage billing/i })).toBeInTheDocument();
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();
    expect(screen.queryByText("Isol8 plan")).not.toBeInTheDocument();
  });

  it("unsubscribed org renders TeamBillingCard with the subscribe CTA", () => {
    billing.isSubscribed = false;
    billing.account = null;

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /subscribe your team/i })).toBeInTheDocument();
    expect(screen.queryByText("Isol8 plan")).not.toBeInTheDocument();
  });

  it("non-admin member of a flat/comped subscribed org sees no spend and no Manage/CTA button (security fix)", () => {
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
      seat_count: 7,
      team_seat_price_usd: 20,
      is_seat_subscription: false,
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

  it("non-admin member of a real seat-sub org also sees no spend and no Manage/CTA button", () => {
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
      seat_count: 4,
      team_seat_price_usd: 20,
      is_seat_subscription: true,
    };

    render(<SettingsPage />);

    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /manage/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).not.toBeInTheDocument();
    expect(screen.getByText(/billing restricted/i)).toBeInTheDocument();
  });

  it("admin of a real seat-sub org sees TeamBillingCard without a spend line", () => {
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
      seat_count: 4,
      team_seat_price_usd: 20,
      is_seat_subscription: true,
    };

    render(<SettingsPage />);

    // TeamBillingCard (Manage) is present.
    expect(screen.getByRole("button", { name: /manage billing/i })).toBeInTheDocument();
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();

    // The spend line was removed from the settings billing card.
    expect(screen.queryByText(/Current period spend/i)).not.toBeInTheDocument();
  });
});
