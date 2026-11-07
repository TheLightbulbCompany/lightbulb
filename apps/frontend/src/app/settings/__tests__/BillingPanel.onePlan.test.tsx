import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";

// One plan (T06) on the settings BillingPanel (settings/page.tsx): a Team owner
// sees Team current and no Base/Ultra; an owner still on a retired plan sees
// "Switch to Team". Harness mirrors ./BillingPanel.billing.test.tsx.

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

import { PANELS } from "@/components/settings/SettingsPlace";

const SettingsPage = PANELS.billing;

const live = {
  is_subscribed: true,
  current_spend: 1,
  lifetime_spend: 2,
  subscription_status: "active",
  trial_end: null,
  provider_choice: null,
  byo_provider: null,
  workspace_price_usd: 50,
  seat_price_usd: 20,
  included_members: 5,
};

describe("BillingPanel — one plan", () => {
  beforeEach(() => {
    nav.panel = "billing";
    clerk.membership = { role: "org:admin" };
    billing.isLoading = false;
    billing.isSubscribed = true;
    billing.startCheckout = vi.fn().mockResolvedValue(undefined);
  });

  it("a Team owner sees Team current, no Base or Ultra, and Manage Payment", () => {
    clerk.membership = null;
    billing.account = { ...live, plan: "standard" };
    render(<SettingsPage />);
    expect(screen.getByTestId("plan-workspace")).toHaveTextContent("Current plan");
    expect(screen.queryByText(/Ultra|Base/)).toBeNull();
    expect(screen.getByText("Lightbulb — $50 / month")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /manage payment/i })).toBeInTheDocument();
  });

  it.each(["ultra_pro", "base"])("a %s owner switches to Team after confirming", async (plan) => {
    billing.account = { ...live, plan };
    render(<SettingsPage />);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Switch to Team" }));
    });
    expect(billing.startCheckout).toHaveBeenCalledWith("workspace", true);
  });
});
