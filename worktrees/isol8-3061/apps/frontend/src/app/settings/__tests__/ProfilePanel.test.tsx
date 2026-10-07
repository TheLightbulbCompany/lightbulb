import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
  // The page forwards a ?panel= naming an href-only nav row (Connections) to
  // that route; no test here uses one, so a no-op replace suffices.
  useRouter: () => ({ replace: vi.fn() }),
}));

const clerk = vi.hoisted(() => ({
  organization: null as Record<string, unknown> | null,
  orgLoaded: true,
  signOut: vi.fn(() => Promise.resolve()),
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
  useOrganization: () => ({
    membership: null,
    organization: clerk.organization,
    isLoaded: clerk.orgLoaded,
  }),
  useClerk: () => ({ signOut: clerk.signOut }),
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
    startCheckout: vi.fn(),
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
    // planLabel capitalizes the raw enum ("active" -> "Active"); settings.css's
    // text-transform: uppercase then renders it visually as ACTIVE on top.
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("does not shout FREE TIER — the uppercase transform is dropped for trial_local", () => {
    billing.account = { subscription_status: "trial_local", trial_end: null };
    render(<SettingsPage />);
    const badge = screen.getByText("Free tier");
    expect(badge).toHaveStyle({ textTransform: "none" });
  });
});

// Covers the self-serve DeleteAccountCard: typed-email confirm gating the
// POST /users/me/delete call, sign-out on success, honest error on a partial
// teardown (502 deletion_incomplete), and the org-context support-only state.
describe("ProfilePanel — delete account", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    nav.panel = null;
    billing.account = null;
    clerk.organization = null;
    clerk.orgLoaded = true;
    clerk.signOut.mockClear();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "deleted" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("requires the account email typed back before the delete button arms", async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: /delete account/i }));

    const deleteBtn = screen.getByRole("button", { name: /delete forever/i });
    expect(deleteBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Confirm account email"), {
      target: { value: "wrong@isol8.co" },
    });
    expect(deleteBtn).toBeDisabled();

    // Case-insensitive match, same as the backend guard.
    fireEvent.change(screen.getByLabelText("Confirm account email"), {
      target: { value: "TEST@isol8.co" },
    });
    expect(deleteBtn).toBeEnabled();

    fireEvent.click(deleteBtn);
    await waitFor(() => expect(clerk.signOut).toHaveBeenCalled());

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/users/me/delete");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ confirm_email: "TEST@isol8.co" });
  });

  it("surfaces a partial teardown instead of signing out", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "deletion_incomplete" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: /delete account/i }));
    fireEvent.change(screen.getByLabelText("Confirm account email"), {
      target: { value: "test@isol8.co" },
    });
    fireEvent.click(screen.getByRole("button", { name: /delete forever/i }));

    await waitFor(() =>
      expect(screen.getByText(/Deletion didn't finish/)).toBeInTheDocument(),
    );
    expect(clerk.signOut).not.toHaveBeenCalled();
  });

  it("org context gets the support-only message, never a delete button", () => {
    clerk.organization = { id: "org_1" };
    render(<SettingsPage />);
    expect(screen.getByText(/contact support@thelightbulb\.company/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete account/i })).toBeNull();
  });

  it("withholds the card entirely while Clerk's org context is still loading", () => {
    // organization is unset during the load window — reading that as
    // "personal" would offer deletion to an org member (backend would 403).
    clerk.orgLoaded = false;
    render(<SettingsPage />);
    expect(screen.queryByRole("button", { name: /delete account/i })).toBeNull();
    expect(screen.queryByText(/contact support@thelightbulb\.company/)).toBeNull();
  });
});
