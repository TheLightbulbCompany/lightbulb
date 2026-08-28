import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";

// Covers the settings BillingPanel org-context branch (settings/page.tsx):
// a subscribed org on a flat/comped sub (is_seat_subscription: false) must
// NOT see the per-seat "N seats × $price" paywall math fabricated from a
// nonexistent Stripe subscription item — it should fall through to the
// plain "Lightbulb plan / Active" card with the spend line instead. A real
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
  startTrialCheckout: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: billing.isLoading,
    error: null,
    isSubscribed: billing.isSubscribed,
    openPortal: vi.fn(),
    startTeamCheckout: vi.fn(),
    startTrialCheckout: billing.startTrialCheckout,
    upgradeToUltra: vi.fn(),
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
      seat_count: 7,
      team_seat_price_usd: 20,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    expect(screen.getByText("Lightbulb plan")).toBeInTheDocument();
    expect(screen.getByText("Past due")).toBeInTheDocument();
    expect(screen.queryByText("past_due")).not.toBeInTheDocument();
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
    expect(screen.queryByText("Lightbulb plan")).not.toBeInTheDocument();
  });

  it("unsubscribed org renders TeamBillingCard with the subscribe CTA", () => {
    billing.isSubscribed = false;
    billing.account = null;

    render(<SettingsPage />);

    expect(screen.getByRole("button", { name: /subscribe your team/i })).toBeInTheDocument();
    expect(screen.queryByText("Lightbulb plan")).not.toBeInTheDocument();
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

describe("BillingPanel — personal free tier (trial_local)", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = null; // personal context
    billing.isLoading = false;
    billing.startTrialCheckout = vi.fn().mockResolvedValue(undefined);
  });

  it("trial_local renders the Free tier card with a Subscribe CTA and no Manage Payment", async () => {
    // is_subscribed is the PAID predicate — false for the no-card free
    // tier even though the platform is usable.
    billing.isSubscribed = false;
    billing.account = {
      is_subscribed: false,
      current_spend: 0.5,
      lifetime_spend: 0.5,
      subscription_status: "trial_local",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    // Free-tier framing: mapped title + chip, never the raw enum, and
    // never the paid "Active subscription" copy.
    expect(screen.getByText("Lightbulb — Free tier")).toBeInTheDocument();
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.getByText(/\$3\/day of included usage/)).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Active subscription")).not.toBeInTheDocument();

    // No portal button — a trial_local row has no Stripe customer, so the
    // portal would fail. The shared ProviderPicker renders instead: one card
    // per provider path, each wired to the flat-fee checkout with its own
    // provider, and each showing that path's REAL price.
    //
    // This surface used to hand-roll three pill buttons over
    // PROVIDER_SWITCH_CARDS — the array documented as the post-purchase
    // variant that deliberately carries no pricing. Rendered pre-purchase it
    // stripped the prices off the one screen whose job is to show them, and
    // hid that the Bedrock path costs $70 today rather than $50.
    expect(screen.queryByRole("button", { name: /manage payment/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with chatgpt/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /bring your own api key/i })).toBeInTheDocument();
    // The per-path price is the disclosure, and it must survive on this
    // surface: Bedrock takes $70 at checkout, the other two take $50.
    expect(screen.getByText(/\$70 at checkout/i)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /powered by claude/i }));
    });
    // The clicked provider must reach the hook — asserting the ARGUMENT is the
    // point, since a reintroduced hardcode still satisfies a bare call count.
    // The second argument is the starter-credit amount bundled into the same
    // Checkout ($20 default, in cents); dropping it would silently fall back to
    // the server default and make the "$70 at checkout" line above a lie
    // whenever the owner picked a different amount.
    expect(billing.startTrialCheckout).toHaveBeenCalledTimes(1);
    expect(billing.startTrialCheckout).toHaveBeenCalledWith("bedrock_claude", 2000);

    // Once a checkout is in flight every path disables — the browser is
    // leaving for Stripe, and two completable sessions means two charged
    // subscriptions (the backend refuses the second, but the UI must not
    // invite it).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    });
    expect(billing.startTrialCheckout).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /sign in with chatgpt/i })).toBeDisabled();
  });

  it("non-expired trial_local with a future trial_end keeps the unchanged Free-tier copy", () => {
    // A trial_local whose 3-day window is still open (trial_end in the
    // future) must render exactly the same Free-tier framing as before —
    // the expired-state swap must not trip early.
    billing.isSubscribed = false;
    billing.account = {
      is_subscribed: false,
      current_spend: 0.5,
      lifetime_spend: 0.5,
      subscription_status: "trial_local",
      // ~1 day in the future (epoch seconds, matching the backend stamp).
      trial_end: Math.floor(Date.now() / 1000) + 86_400,
      provider_choice: null,
      byo_provider: null,
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    expect(screen.getByText("Lightbulb — Free tier")).toBeInTheDocument();
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.getByText(/\$3\/day of included usage/)).toBeInTheDocument();
    // The expired framing must NOT appear while the window is open.
    expect(screen.queryByText(/free tier ended/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /powered by claude/i })).toBeInTheDocument();
  });

  it("EXPIRED trial_local swaps the $3/day promise for a paused state but keeps the Subscribe CTA", () => {
    // trial_end is the OVERALL free-tier window expiry (created + 3 days).
    // Once it's in the past, the backend blocks usage outright WITHOUT
    // flipping subscription_status off trial_local — so the card must stop
    // promising "$3/day of included usage" and show the paused state, while
    // still offering Subscribe as the path forward.
    billing.isSubscribed = false;
    billing.account = {
      is_subscribed: false,
      current_spend: 3,
      lifetime_spend: 3,
      subscription_status: "trial_local",
      // ~1 day in the past (epoch seconds).
      trial_end: Math.floor(Date.now() / 1000) - 86_400,
      provider_choice: null,
      byo_provider: null,
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    // Expired framing: chip + description reflect that the free tier ended
    // and usage is paused. The chip is the exact "Free tier ended" string.
    expect(screen.getByText("Free tier ended")).toBeInTheDocument();
    expect(screen.getByText(/free tier ended.*agents are paused/i)).toBeInTheDocument();

    // The misleading "$3/day of included usage" promise is gone.
    expect(screen.queryByText(/\$3\/day of included usage/)).not.toBeInTheDocument();
    // Still not the paid "Active subscription" copy, and the raw enum never renders.
    expect(screen.queryByText("Active subscription")).not.toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();

    // The Subscribe CTA remains the path forward (no Manage Payment — no
    // Stripe customer exists for a trial_local row).
    expect(screen.queryByRole("button", { name: /manage payment/i })).not.toBeInTheDocument();
    // All three paths stay on offer after expiry — an owner whose window ran
    // out is exactly who should get the full choice, not a silent default.
    expect(screen.getByRole("button", { name: /powered by claude/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with chatgpt/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /bring your own api key/i })).toBeInTheDocument();
  });

  it("active personal subscription keeps the unchanged paid card", () => {
    billing.isSubscribed = true;
    billing.account = {
      is_subscribed: true,
      current_spend: 12.5,
      lifetime_spend: 40,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "bedrock_claude",
      byo_provider: null,
      seat_count: null,
      team_seat_price_usd: null,
      is_seat_subscription: false,
    };

    render(<SettingsPage />);

    expect(screen.getByText("Lightbulb — $50 / month")).toBeInTheDocument();
    expect(screen.getByText("Active subscription")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe — \$50\/mo/i })).not.toBeInTheDocument();
  });
});
