// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Per-test Clerk state. Mocks read from this module-level object so each
// test can configure its own Clerk shape before calling renderHook.
// ---------------------------------------------------------------------------

const replaceMock = vi.fn();
const setActiveMock = vi.fn().mockResolvedValue(undefined);
const userUpdateMock = vi.fn().mockResolvedValue(undefined);

let searchString = "";

interface ClerkState {
  isSignedIn: boolean | null;
  authLoaded: boolean;
  user: { unsafeMetadata?: Record<string, unknown> } | null;
  userLoaded: boolean;
  organization: { id: string } | null;
  orgLoaded: boolean;
  memberships: Array<{ organization: { id: string } }>;
  invitations: Array<{ id: string }>;
  orgListLoaded: boolean;
}

const clerkState: ClerkState = {
  isSignedIn: true,
  authLoaded: true,
  user: { unsafeMetadata: { onboarded: true } },
  userLoaded: true,
  organization: null,
  orgLoaded: true,
  memberships: [],
  invitations: [],
  orgListLoaded: true,
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(searchString),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: clerkState.isSignedIn, isLoaded: clerkState.authLoaded }),
  useUser: () => ({
    user: clerkState.user ? { ...clerkState.user, update: userUpdateMock } : null,
    isLoaded: clerkState.userLoaded,
  }),
  useOrganization: () => ({
    organization: clerkState.organization,
    isLoaded: clerkState.orgLoaded,
  }),
  useOrganizationList: () => ({
    userMemberships: { data: clerkState.memberships },
    userInvitations: { data: clerkState.invitations },
    setActive: setActiveMock,
    isLoaded: clerkState.orgListLoaded,
  }),
}));

import { useOnboardingGate } from "../useOnboardingGate";

function setClerk(partial: Partial<ClerkState>): void {
  Object.assign(clerkState, partial);
}

describe("useOnboardingGate", () => {
  beforeEach(() => {
    replaceMock.mockReset();
    setActiveMock.mockReset().mockResolvedValue(undefined);
    userUpdateMock.mockReset().mockResolvedValue(undefined);
    searchString = "";
    // Clear any signup-intent cookie a prior test set (jsdom persists it).
    document.cookie = "lightbulb_signup_intent=; max-age=0; path=/";
    // Reset to a known baseline: signed-in, fully loaded, personal user
    // who completed onboarding, no orgs, no invitations.
    setClerk({
      isSignedIn: true,
      authLoaded: true,
      user: { unsafeMetadata: { onboarded: true } },
      userLoaded: true,
      organization: null,
      orgLoaded: true,
      memberships: [],
      invitations: [],
      orgListLoaded: true,
    });
  });

  it("returns 'loading' while Clerk is hydrating", () => {
    setClerk({ userLoaded: false });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("loading");
    expect(replaceMock).not.toHaveBeenCalled();
    expect(setActiveMock).not.toHaveBeenCalled();
  });

  it("returns 'loading' when not signed in", () => {
    setClerk({ isSignedIn: false });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("loading");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("returns 'ready' for an onboarded personal user with no orgs", () => {
    // Baseline matches this — explicit re-set for readability.
    setClerk({
      user: { unsafeMetadata: { onboarded: true } },
      organization: null,
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("ready");
    expect(replaceMock).not.toHaveBeenCalled();
    expect(setActiveMock).not.toHaveBeenCalled();
  });

  it("returns 'ready' when an organization is already active", () => {
    setClerk({
      organization: { id: "org_active" },
      memberships: [{ organization: { id: "org_active" } }],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("ready");
    expect(replaceMock).not.toHaveBeenCalled();
    expect(setActiveMock).not.toHaveBeenCalled();
  });

  it("completes a fresh personal signup inline and returns 'ready' (no /onboarding redirect)", async () => {
    // Personal signups (no team intent, no invitation) are finished here and
    // dropped straight into /chat — they never visit /onboarding.
    setClerk({
      user: { unsafeMetadata: {} },
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("ready");
    await waitFor(() =>
      expect(userUpdateMock).toHaveBeenCalledWith({
        unsafeMetadata: { onboarded: true },
      }),
    );
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("returns 'redirecting' and replaces to /onboarding when there are pending invitations", async () => {
    // Tenancy invariant: pending org invitation beats a stale onboarded
    // flag. The user must visit /onboarding to accept (or decline) before
    // we can let them into /chat under any tenancy.
    setClerk({
      user: { unsafeMetadata: { onboarded: true } },
      memberships: [],
      invitations: [{ id: "inv_x" }],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("redirecting");
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/onboarding"));
  });

  it("returns 'auto-activating' and calls setActive on the first membership when no org is active", async () => {
    setClerk({
      organization: null,
      memberships: [
        { organization: { id: "org_first" } },
        { organization: { id: "org_second" } },
      ],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("auto-activating");
    await waitFor(() =>
      expect(setActiveMock).toHaveBeenCalledWith({ organization: "org_first" }),
    );
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("preserves ?intent=team on the /onboarding redirect for a never-onboarded team signup", async () => {
    searchString = "intent=team";
    setClerk({
      user: { unsafeMetadata: {} },
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("redirecting");
    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/onboarding?intent=team"),
    );
  });

  it("routes an ALREADY-onboarded user with team intent to org creation", async () => {
    // "Create a team" must work for existing personal users, not only fresh
    // signups — so the team redirect does not require !onboarded.
    searchString = "intent=team";
    setClerk({
      user: { unsafeMetadata: { onboarded: true } },
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("redirecting");
    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/onboarding?intent=team"),
    );
    // Not treated as a personal completion.
    expect(userUpdateMock).not.toHaveBeenCalled();
  });

  it("routes on the intent COOKIE alone", async () => {
    // The cookie — not the query — is the signal that survives Clerk's
    // multi-step signup, so this is the path a real team signup takes.
    document.cookie = "lightbulb_signup_intent=team; path=/";
    searchString = "";
    setClerk({
      user: { unsafeMetadata: {} },
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("redirecting");
    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/onboarding?intent=team"),
    );
  });

  it("does NOT clear the intent cookie itself — the redirect landing on /chat while the cookie clears would re-arm useProvisioningState's poll suppression", async () => {
    // The cookie is what `useProvisioningState` suppresses its provisioning
    // poll on (`!orgId && hasTeamSignupIntentCookie()`), and App Router keeps
    // /chat committed until the /onboarding RSC payload lands. Clearing the
    // cookie here — before that navigation has actually landed — would
    // re-arm the poll on the still-mounted /chat and provision the very
    // throwaway personal container this whole mechanism exists to prevent.
    // Consumption moved to /onboarding (see app/onboarding/page.tsx), which
    // clears it once `mode === "org"` — i.e. once the browser has actually
    // stopped rendering /chat. This test pins that this hook stays a
    // non-consumer so that regression can't creep back in silently.
    document.cookie = "lightbulb_signup_intent=team; path=/";
    searchString = "";
    setClerk({
      user: { unsafeMetadata: {} },
      memberships: [],
      invitations: [],
    });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("redirecting");
    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith("/onboarding?intent=team"),
    );
    expect(document.cookie).toContain("lightbulb_signup_intent=team");
  });

  it("does not treat an undecodable intent cookie as team intent", async () => {
    // Fail-open: `decodeURIComponent("%")` throws, and this read happens
    // during render. The predicate swallows it, so the gate falls back to its
    // pre-cookie behaviour (personal) instead of crashing the chat shell.
    document.cookie = "lightbulb_signup_intent=%; path=/";
    searchString = "";
    setClerk({ user: { unsafeMetadata: {} }, memberships: [], invitations: [] });
    const { result } = renderHook(() => useOnboardingGate());
    expect(result.current).toBe("ready");
    expect(replaceMock).not.toHaveBeenCalled();
  });
});
