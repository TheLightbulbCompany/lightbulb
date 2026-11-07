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

import { PANELS } from "@/components/settings/SettingsPlace";

const SettingsPage = PANELS.profile;

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
    expect(screen.getByText(/· Free tier/)).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("passes other statuses through for the uppercase CSS transform", () => {
    billing.account = { subscription_status: "active", trial_end: null };
    render(<SettingsPage />);
    // planLabel capitalizes the raw enum ("active" -> "Active"); settings.css's
    // text-transform: uppercase then renders it visually as ACTIVE on top.
    expect(screen.getByText(/· Active/)).toBeInTheDocument();
  });

});

// Covers the self-serve DeleteAccountCard: typed-email confirm gating the
// POST /users/me/delete call, sign-out on success, honest error on a partial
// teardown (502 deletion_incomplete), offered in a workspace context too.
describe("ProfilePanel — delete account", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    nav.panel = null;
    billing.account = null;
    clerk.organization = null;
    clerk.orgLoaded = true;
    clerk.signOut.mockClear();
    fetchMock.mockReset();
    // A fresh Response per call: the Profile panel's Daily summary card fetches too.
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ status: "deleted" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("requires the account email typed back before the delete button arms", async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: /^delete…$/i }));

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

    const [url, init] = fetchMock.mock.calls.find(([u]) => String(u).includes("/users/me/delete")) as [
      string,
      RequestInit,
    ];
    expect(url).toContain("/users/me/delete");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ confirm_email: "TEST@isol8.co" });
  });

  it("surfaces a partial teardown instead of signing out", async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: "deletion_incomplete" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: /^delete…$/i }));
    fireEvent.change(screen.getByLabelText("Confirm account email"), {
      target: { value: "test@isol8.co" },
    });
    fireEvent.click(screen.getByRole("button", { name: /delete forever/i }));

    await waitFor(() =>
      expect(screen.getByText(/Deletion didn't finish/)).toBeInTheDocument(),
    );
    expect(clerk.signOut).not.toHaveBeenCalled();
  });

  it("offers deletion in a workspace context too (the backend supports it)", () => {
    clerk.organization = { id: "org_1" };
    render(<SettingsPage />);
    expect(screen.getByRole("button", { name: /^delete…$/i })).toBeInTheDocument();
  });
});

// Covers WorkspaceExportCard: only a 200 NDJSON body is saved; an error shows
// the retry message and records no export.
const analytics = vi.hoisted(() => ({ capture: vi.fn() }));
vi.mock("@/lib/analytics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/analytics")>()),
  capture: analytics.capture,
}));

describe("ProfilePanel — workspace export", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    nav.panel = null;
    billing.account = null;
    clerk.organization = null;
    analytics.capture.mockClear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("does not save an error response as the export", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      String(url).includes("/tasks/export")
        ? new Response(JSON.stringify({ detail: { code: "not_found" } }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: /^download$/i }));

    await waitFor(() => expect(screen.getByText(/couldn't prepare your export/i)).toBeInTheDocument());
    expect(analytics.capture).not.toHaveBeenCalledWith("workspace_export_downloaded");
  });
});
