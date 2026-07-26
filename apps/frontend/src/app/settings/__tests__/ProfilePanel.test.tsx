import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Covers the settings ProfilePanel's `.settings-profile-badge` — it renders
// the raw `subscription_status` next to the user's name, styled
// text-transform: uppercase (settings.css). Before this fix a trial_local
// owner saw "TRIAL_LOCAL" here; it must now go through planLabel() the same
// way the BillingPanel's chip does, with the uppercase transform dropped so
// "Free tier" doesn't come out "FREE TIER".
//
// Mocking harness copied from BillingPanel.billing.test.tsx; nav.panel left
// null so SettingsPageInner's default-panel fallback lands on "profile".

const nav = vi.hoisted(() => ({ panel: null as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
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
  useOrganization: () => ({ membership: null, organization: null }),
  UserButton: () => <div data-testid="user-button" />,
}));

const billing = vi.hoisted(() => ({
  account: null as Record<string, unknown> | null,
}));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billing.account,
    isLoading: false,
    error: null,
    isSubscribed: false,
    openPortal: vi.fn(),
    startTeamCheckout: vi.fn(),
    startTrialCheckout: vi.fn(),
    upgradeToUltra: vi.fn(),
  }),
}));

import SettingsPage from "../page";

describe("ProfilePanel — subscription badge", () => {
  beforeEach(() => {
    nav.panel = null;
  });

  it("renders no badge when there is no billing account yet", () => {
    billing.account = null;
    render(<SettingsPage />);
    expect(document.querySelector(".settings-profile-badge")).toBeNull();
  });

  it("maps trial_local to Free tier, never the raw enum", () => {
    billing.account = { subscription_status: "trial_local", trial_end: null };
    render(<SettingsPage />);
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("passes other statuses through for the uppercase CSS transform", () => {
    billing.account = { subscription_status: "active", trial_end: null };
    render(<SettingsPage />);
    // The label itself stays "active" — settings.css's text-transform:
    // uppercase renders it visually as ACTIVE; textContent stays lowercase.
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("does not shout FREE TIER — the uppercase transform is dropped for trial_local", () => {
    billing.account = { subscription_status: "trial_local", trial_end: null };
    render(<SettingsPage />);
    const badge = screen.getByText("Free tier");
    expect(badge).toHaveStyle({ textTransform: "none" });
  });
});
