# Teams Entry Point + Onboarding Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the personal-vs-organization fork from the post-login onboarding screen; make account type implied by a landing-page "Create a team" CTA (`/sign-up?intent=team`) instead; add a Teams entry point to the landing pricing section.

**Architecture:** The team signal rides a one-hop URL param `intent=team`. `useOnboardingGate` (which runs on `/chat` and today drops the query string when it redirects fresh signups to `/onboarding`) is changed to preserve `intent` on that redirect. The onboarding page reads `intent` synchronously on first render, selects the org-creation branch, and lifts it into React state (`explicitMode="org"`) so it survives Clerk's `CreateOrganization` same-route soft navigation. Personal signups (no intent, no org, no invite) auto-complete onboarding with a spinner and never see a choice. Pending org invitations still force the accept-invite screen (unchanged tenancy invariant).

**Tech Stack:** Next.js 16 (App Router, client components), React 19, Clerk (`@clerk/nextjs`), Vitest + Testing Library, Tailwind.

## Global Constraints

- **Frontend tests: single-file runs only.** The full vitest suite is RAM-heavy — never run it. Run one file: `pnpm test <path>` (maps to `NODE_OPTIONS='--max-old-space-size=8192' vitest run <path>`), from `apps/frontend/`.
- **Typecheck test files:** after adding/editing tests, run `pnpm exec tsc --noEmit` from `apps/frontend/` — CI typechecks test files.
- All work is in `apps/frontend/`. No backend changes in this plan.
- **Preserve the tenancy invariant:** pending org invitations beat everything and force `invitations` mode. Do not weaken this.
- **`unsafeMetadata` writes must merge**, never replace: always spread `...(user.unsafeMetadata ?? {})` when setting `onboarded: true`, so a future durable key (e.g. the deferred provider fix) isn't clobbered.
- Commit after each task. Branch is already `claude/free-tier-phase-a`; do not push (no approval yet).

## File map

- `apps/frontend/src/hooks/useOnboardingGate.ts` — modify: preserve `intent` query param on the `/onboarding` redirect.
- `apps/frontend/src/hooks/__tests__/useOnboardingGate.test.tsx` — modify: make `useSearchParams` mock configurable; add intent-preservation test.
- `apps/frontend/src/app/onboarding/page.tsx` — modify: remove chooser, add intent→org + personal-auto-complete state machine, merge-preserving metadata writes.
- `apps/frontend/src/app/onboarding/__tests__/page.test.tsx` — modify: add `useSearchParams` mock; replace the chooser test; add intent=team test.
- `apps/frontend/src/components/landing/PricingThreeCard.tsx` — modify: add Teams strip below the provider grid.
- `apps/frontend/src/components/landing/__tests__/PricingThreeCard.test.tsx` — create: assert the Teams CTA renders and points at `/sign-up?intent=team`.

## Deferred / related (NOT in this plan)

The same gate redirect also drops the `?provider=` param, so fresh signups routed through onboarding lose their provider choice and the provider-specific ProvisioningStepper step is silently skipped (a real funnel bug — see memory `project_onboarding_funnel_posthog_blindspot`). The correct fix is durable via `unsafeMetadata.providerChoice` (multi-hop to `/chat`) and touches `ProvisioningStepper.tsx`. It is intentionally a separate fast-follow to keep this plan tight. The merge-preserving metadata writes in Task 2 are written so that fix drops in cleanly later.

---

### Task 1: Preserve `intent` through the onboarding-gate redirect

**Files:**
- Modify: `apps/frontend/src/hooks/useOnboardingGate.ts:94-98`
- Test: `apps/frontend/src/hooks/__tests__/useOnboardingGate.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces: after this task, a signed-in, never-onboarded user arriving at `/chat?intent=team` is redirected to `/onboarding?intent=team` (param-less users still redirect to `/onboarding`).

- [ ] **Step 1: Make the `useSearchParams` mock configurable and add the failing test**

In `apps/frontend/src/hooks/__tests__/useOnboardingGate.test.tsx`, replace the `next/navigation` mock (lines 37-40) so the search string is per-test controllable:

```tsx
let searchString = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(searchString),
}));
```

Add `searchString = "";` to the `beforeEach` reset block (inside `beforeEach`, after `setActiveMock.mockReset()...`):

```tsx
    searchString = "";
```

Then add this test at the end of the `describe` block:

```tsx
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pnpm test src/hooks/__tests__/useOnboardingGate.test.tsx`
Expected: FAIL — the new test sees `replaceMock` called with `"/onboarding"` (param dropped), not `"/onboarding?intent=team"`. Existing tests still pass.

- [ ] **Step 3: Preserve the `intent` param in the redirect**

In `apps/frontend/src/hooks/useOnboardingGate.ts`, add `useSearchParams` to the `next/navigation` import (line 4):

```tsx
import { useRouter, useSearchParams } from "next/navigation";
```

Inside `useOnboardingGate`, after `const router = useRouter();` (line 48), add:

```tsx
  const searchParams = useSearchParams();
```

Replace the redirect effect (lines 94-98) with:

```tsx
  // Side effect 1: redirect (idempotent — router.replace dedupes if already
  // on /onboarding). Both the "no row at all" path and the "pending
  // invitations" path land on the same /onboarding screen; the page itself
  // surfaces the right UI based on Clerk state. We forward ?intent=team so a
  // team signup (landing "Create a team" CTA → /sign-up?intent=team) still
  // reaches onboarding's org-creation branch — a bare redirect would drop it.
  useEffect(() => {
    if (needsOnboarding || needsInvitationFlow) {
      const intent = searchParams.get("intent");
      const target = intent ? `/onboarding?intent=${encodeURIComponent(intent)}` : "/onboarding";
      router.replace(target);
    }
  }, [needsOnboarding, needsInvitationFlow, router, searchParams]);
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm test src/hooks/__tests__/useOnboardingGate.test.tsx`
Expected: PASS — all tests green, including the new intent-preservation test.

- [ ] **Step 5: Typecheck**

Run: `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useOnboardingGate.ts apps/frontend/src/hooks/__tests__/useOnboardingGate.test.tsx
git commit -m "fix(onboarding): preserve intent=team through the onboarding-gate redirect"
```

---

### Task 2: Remove the onboarding chooser; intent→org, personal auto-completes

**Files:**
- Modify: `apps/frontend/src/app/onboarding/page.tsx` (full-file rewrite below)
- Test: `apps/frontend/src/app/onboarding/__tests__/page.test.tsx`

**Interfaces:**
- Consumes: `intent=team` on the URL (forwarded by Task 1).
- Produces: `/onboarding` renders `<CreateOrganization>` when `?intent=team`; otherwise auto-completes personal onboarding (writes merge-preserving `unsafeMetadata.onboarded=true`, `api.syncUser()`, `router.push("/chat")`) behind a spinner. Pending invitations still force the invitations screen.

- [ ] **Step 1: Update the tests first (failing)**

Replace the entire contents of `apps/frontend/src/app/onboarding/__tests__/page.test.tsx` with:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const syncUserMock = vi.fn().mockResolvedValue(undefined);
const pushMock = vi.fn();
let searchString = "";

// Minimal Clerk mocks — configurable invitations / memberships / org.
const buildClerkMocks = (
  overrides: {
    invitations?: unknown[];
    memberships?: unknown[];
    userUpdate?: () => Promise<void>;
  } = {},
) => ({
  useAuth: () => ({ isLoaded: true, orgId: null }),
  useUser: () => ({
    user: {
      unsafeMetadata: {},
      update: overrides.userUpdate ?? vi.fn().mockResolvedValue(undefined),
    },
  }),
  useOrganization: () => ({ organization: null, isLoaded: true }),
  useOrganizationList: () => ({
    userMemberships: { data: overrides.memberships ?? [] },
    userInvitations: {
      data: overrides.invitations ?? [],
      revalidate: vi.fn(),
    },
    isLoaded: true,
    setActive: vi.fn(),
  }),
  CreateOrganization: () => <div>CreateOrganization mock</div>,
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
  useSearchParams: () => new URLSearchParams(searchString),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ syncUser: syncUserMock }) }));
// lib/analytics re-exports PostHogProvider at module load; the mock must
// expose it (vitest is strict about import-time destructuring vs mocks).
vi.mock("posthog-js/react", () => ({
  usePostHog: () => null,
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock("@clerk/nextjs");
    searchString = "";
    syncUserMock.mockClear();
    pushMock.mockClear();
  });

  it("forces invitation mode when pending invitations exist — no Skip button", async () => {
    vi.doMock("@clerk/nextjs", () =>
      buildClerkMocks({
        invitations: [
          {
            id: "orginv_1",
            publicOrganizationData: { name: "Acme Org" },
            accept: vi.fn(),
          },
        ],
      }),
    );
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.getByText("Acme Org")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /skip invitations/i }),
    ).toBeNull();
  });

  it("auto-completes personal onboarding when no invitations and no team intent", async () => {
    const userUpdate = vi.fn().mockResolvedValue(undefined);
    vi.doMock("@clerk/nextjs", () => buildClerkMocks({ userUpdate }));
    const { default: Page } = await import("../page");
    render(<Page />);
    // No chooser is shown any more.
    expect(screen.queryByRole("button", { name: /personal/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /organization/i })).toBeNull();
    // Personal path runs automatically: onboarded flag + syncUser + redirect.
    await waitFor(() =>
      expect(userUpdate).toHaveBeenCalledWith({
        unsafeMetadata: { onboarded: true },
      }),
    );
    await waitFor(() => expect(syncUserMock).toHaveBeenCalled());
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/chat"));
  });

  it("renders CreateOrganization when intent=team", async () => {
    searchString = "intent=team";
    vi.doMock("@clerk/nextjs", () => buildClerkMocks());
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.getByText("CreateOrganization mock")).toBeInTheDocument();
    // Personal auto-complete must NOT fire on the team path.
    expect(syncUserMock).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm test src/app/onboarding/__tests__/page.test.tsx`
Expected: FAIL — the current page still renders the personal/organization chooser and never auto-runs personal.

- [ ] **Step 3: Rewrite the onboarding page**

Replace the entire contents of `apps/frontend/src/app/onboarding/page.tsx` with:

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  CreateOrganization,
  useAuth,
  useOrganization,
  useOrganizationList,
  useUser,
} from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { Users, Mail } from "lucide-react";
import { InviteTeammatesStep } from "@/components/onboarding/InviteTeammatesStep";

export default function OnboardingPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isLoaded, orgId: authOrgId } = useAuth();
  const { user } = useUser();
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { userMemberships, userInvitations, isLoaded: orgsLoaded, setActive } =
    useOrganizationList({
      userMemberships: true,
      userInvitations: true,
    });
  const api = useApi();
  const [acceptingId, setAcceptingId] = useState<string | null>(null);
  // Guards the personal auto-complete so it fires exactly once (the effect
  // that runs it has reactive deps and would otherwise re-enter).
  const autoRanRef = useRef(false);

  const pendingInvitations = userInvitations?.data ?? [];

  // Account type is implied by the landing-page CTA the user clicked:
  // "Create a team" links to /sign-up?intent=team, and useOnboardingGate
  // forwards ?intent=team through its redirect to /onboarding. Everyone else
  // is a personal signup and never sees a choice.
  const intentTeam = searchParams.get("intent") === "team";

  // Tenancy invariant: pending invitations force the invitations screen —
  // no escape into personal/org onboarding, because a user can't hold
  // personal tenancy AND a pending org invite at the same time.
  const [explicitMode, setExplicitMode] = useState<
    "org" | "invitations" | null
  >(null);
  const forcedInvitations = isLoaded && orgsLoaded && pendingInvitations.length > 0;
  const mode = forcedInvitations
    ? "invitations"
    : explicitMode ?? (intentTeam ? "org" : null);
  const setMode = setExplicitMode;

  // Lift team intent into explicit React state so it survives Clerk's
  // same-route soft navigation after <CreateOrganization> finishes
  // (afterCreateOrganizationUrl="/onboarding" drops the ?intent= query, but
  // component state persists across the soft nav). Without this, the
  // post-create render would recompute intentTeam=false, fall through to the
  // "org already active → redirect" branch below, and skip InviteTeammatesStep.
  useEffect(() => {
    if (intentTeam && explicitMode === null && !forcedInvitations) {
      capture("workspace_type_selected", { type: "org" });
      setMode("org");
    }
  }, [intentTeam, explicitMode, forcedInvitations]);

  // Merge-preserving completion: write onboarded=true WITHOUT clobbering
  // other unsafeMetadata keys, then redirect to /chat. The flag is the
  // durable, user-scoped source of truth for "completed onboarding" —
  // Clerk's active-org state is session-scoped and doesn't survive fresh
  // logins, so without the flag a user bounces back to /onboarding.
  async function completeOnboardingAndRedirect() {
    try {
      await user?.update({
        unsafeMetadata: {
          ...(user.unsafeMetadata as Record<string, unknown> | undefined),
          onboarded: true,
        },
      });
    } catch {
      // Best-effort — ChatLayout's auto-activate fallback still gets the
      // user into /chat on subsequent loads.
    }
    router.push("/chat");
  }

  async function handlePersonal() {
    capture("workspace_type_selected", { type: "personal" });
    try {
      await user?.update({
        unsafeMetadata: {
          ...(user.unsafeMetadata as Record<string, unknown> | undefined),
          onboarded: true,
        },
      });
      await api.syncUser();
      router.push("/chat");
    } catch {
      // Allow a retry on transient failure.
      autoRanRef.current = false;
    }
  }

  // Redirect / auto-complete effect. Handles, in order: org already active,
  // has memberships (auto-activate first), and the personal fast path.
  // Skips entirely for the org-creation and invitations UIs — they own their
  // own flow and rendering.
  useEffect(() => {
    if (!isLoaded || !orgLoaded || !orgsLoaded) return;
    if (mode === "org" || mode === "invitations") return;

    // Path 1: org already active → mark onboarded + redirect.
    if (organization) {
      completeOnboardingAndRedirect();
      return;
    }
    // Path 2: has memberships but no active org (fresh login on a new device)
    // → activate the first membership, then redirect.
    const memberships = userMemberships?.data;
    if (memberships && memberships.length > 0 && setActive) {
      setActive({ organization: memberships[0].organization.id }).then(
        completeOnboardingAndRedirect,
      );
      return;
    }
    // Path 3: fresh personal signup (no org, no membership, no team intent)
    // → complete onboarding automatically. No interstitial.
    if (mode === null && !autoRanRef.current) {
      autoRanRef.current = true;
      handlePersonal();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoaded, orgLoaded, orgsLoaded, organization, userMemberships, setActive, mode, user]);

  if (!isLoaded || !orgsLoaded || !orgLoaded) return null;

  async function handleAcceptInvitation(invitationId: string) {
    setAcceptingId(invitationId);
    try {
      capture("org_invitation_accepted", { org_id: invitationId });
      const inv = pendingInvitations.find((i) => i.id === invitationId);
      if (inv && typeof (inv as unknown as { accept?: () => Promise<void> }).accept === "function") {
        await (inv as unknown as { accept: () => Promise<void> }).accept();
      }
      // Don't redirect or mark onboarded here — the redirect effect above
      // watches userMemberships and handles setActive + onboarded + redirect
      // once Clerk propagates the membership. Avoids racing Clerk's eventual
      // consistency.
      await userInvitations?.revalidate?.();
      await userMemberships?.revalidate?.();
    } catch (err) {
      console.error("Failed to accept invitation:", err);
      setAcceptingId(null);
    }
  }

  if (mode === "invitations") {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-6 bg-background">
        <div className="text-center mb-4">
          <Mail className="h-10 w-10 mx-auto mb-3 text-primary" />
          <h1 className="text-2xl font-bold">You have been invited</h1>
          <p className="text-muted-foreground mt-2">
            Accept an invitation to join an organization on Isol8.
          </p>
        </div>

        <div className="flex flex-col gap-3 w-full max-w-md px-4">
          {pendingInvitations.map((invitation) => (
            <div
              key={invitation.id}
              className="flex items-center justify-between p-4 rounded-lg border border-border bg-card"
            >
              <div className="flex items-center gap-3">
                <Users className="h-5 w-5 text-muted-foreground" />
                <div>
                  <div className="font-medium">
                    {(invitation as unknown as { publicOrganizationData?: { name?: string } }).publicOrganizationData?.name || "Organization"}
                  </div>
                  <div className="text-sm text-muted-foreground">Pending invitation</div>
                </div>
              </div>
              <Button
                onClick={() => handleAcceptInvitation(invitation.id)}
                disabled={acceptingId !== null}
                size="sm"
              >
                {acceptingId === invitation.id ? "Accepting..." : "Accept"}
              </Button>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (mode === "org") {
    // Two-stage gate after Clerk's <CreateOrganization> finishes:
    //
    //   Stage A: `organization` is non-null but the JWT hasn't picked up the
    //   new org yet (Clerk's setActive → JWT refresh is async). Show a
    //   transient "Activating…" message. Without this gate, the first invite
    //   POST hits the backend's auth.org_id == org_id check with a pre-org
    //   JWT and returns 403 — flaky right after org creation.
    //
    //   Stage B: JWT now carries the new org. Mount InviteTeammatesStep so
    //   invites flow through the backend's Gate A (which rejects
    //   personal-subscriber emails) instead of Clerk's built-in invite UI.
    if (organization && authOrgId === organization.id) {
      return (
        <InviteTeammatesStep
          orgId={organization.id}
          onComplete={completeOnboardingAndRedirect}
        />
      );
    }
    if (organization && authOrgId !== organization.id) {
      return (
        <div className="flex flex-col items-center justify-center min-h-screen gap-4 bg-background">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-primary" />
          <p className="text-sm text-muted-foreground">Activating your organization…</p>
        </div>
      );
    }
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-6 bg-background">
        <div className="text-center mb-4">
          <h1 className="text-2xl font-bold">Create your team</h1>
          <p className="text-muted-foreground mt-2">
            Your team will share agents, workspace, and billing.
          </p>
        </div>
        <CreateOrganization
          // skipInvitationScreen={true} → Clerk creates the org and stops.
          // <InviteTeammatesStep> takes over above (rendered when
          // `organization` resolves) so all invites route through the
          // backend's Gate A.
          afterCreateOrganizationUrl="/onboarding?intent=team"
          skipInvitationScreen={true}
        />
      </div>
    );
  }

  // Personal fast path (or still resolving org/membership state) → spinner.
  // handlePersonal() runs from the effect above; this is the visual while it
  // completes.
  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-4 bg-background">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-primary" />
      <p className="text-sm text-muted-foreground">Setting up your workspace…</p>
    </div>
  );
}
```

Note the `Button` import: the invitations branch still uses `<Button>`. Keep the import — add it back at the top:

```tsx
import { Button } from "@/components/ui/button";
```

(Place it with the other imports, e.g. after the `useApi` import line.)

Also note: `afterCreateOrganizationUrl` is now `/onboarding?intent=team` (was `/onboarding`) — a belt-and-suspenders so that even if the soft-nav were to remount and lose React state, `intentTeam` recomputes true and keeps `mode === "org"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm test src/app/onboarding/__tests__/page.test.tsx`
Expected: PASS — all three tests green (invitations forced, personal auto-completes, intent=team renders CreateOrganization).

- [ ] **Step 5: Typecheck**

Run: `pnpm exec tsc --noEmit`
Expected: no errors. (If `Button` or an icon import is unused/missing, fix it now.)

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/app/onboarding/page.tsx apps/frontend/src/app/onboarding/__tests__/page.test.tsx
git commit -m "feat(onboarding): drop personal/org chooser; personal auto-completes, team via intent"
```

---

### Task 3: Add the Teams entry point to the landing pricing section

**Files:**
- Modify: `apps/frontend/src/components/landing/PricingThreeCard.tsx`
- Test: `apps/frontend/src/components/landing/__tests__/PricingThreeCard.test.tsx` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: a "Create a team" link in the pricing section pointing at `/sign-up?intent=team` (the entry point Tasks 1-2 route on).

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/components/landing/__tests__/PricingThreeCard.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PricingThreeCard } from "../PricingThreeCard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("PricingThreeCard", () => {
  it("renders a Teams CTA pointing at /sign-up?intent=team", () => {
    render(<PricingThreeCard />);
    const cta = screen.getByRole("link", { name: /create a team/i });
    expect(cta).toHaveAttribute("href", "/sign-up?intent=team");
  });

  it("still renders the three provider cards", () => {
    render(<PricingThreeCard />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pnpm test src/components/landing/__tests__/PricingThreeCard.test.tsx`
Expected: FAIL on the first test — no "Create a team" link exists yet. (Second test passes.)

- [ ] **Step 3: Add the Teams strip**

In `apps/frontend/src/components/landing/PricingThreeCard.tsx`, insert the Teams strip immediately after the closing `</div>` of `ld-price-grid` (currently line 69) and before the closing `</div>` of `ld-section`:

```tsx
        <div className="ld-teams-strip" data-ld-reveal>
          <div className="ld-teams-copy">
            <h3>Isol8 for Teams</h3>
            <p>Shared agents, one workspace, per-seat pricing.</p>
          </div>
          <Link
            href="/sign-up?intent=team"
            className="ld-btn"
            onClick={() => {
              capture("landing_pricing_card_clicked", {
                provider_choice: "team",
                destination: "/sign-up?intent=team",
              });
              capture("signup_started", {
                source: "landing_pricing_teams",
                provider_choice: "team",
                destination: "/sign-up?intent=team",
              });
            }}
          >
            Create a team
            <span className="ld-btn-arrow" aria-hidden="true">
              →
            </span>
          </Link>
        </div>
```

`Link` and `capture` are already imported at the top of the file — no new imports needed.

- [ ] **Step 4: Add minimal styling for the strip**

Find the landing stylesheet that defines the `ld-price-*` classes (search: `grep -rl "ld-price-grid" apps/frontend/src`). In that file, add:

```css
.ld-teams-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.5rem;
  flex-wrap: wrap;
  margin-top: 2rem;
  padding: 1.5rem 1.75rem;
  border: 1px solid var(--ld-border, rgba(255, 255, 255, 0.12));
  border-radius: 1rem;
}
.ld-teams-copy h3 {
  font-weight: 600;
  margin: 0;
}
.ld-teams-copy p {
  margin: 0.25rem 0 0;
  opacity: 0.7;
}
```

If the surrounding CSS uses a different custom-property name for borders than `--ld-border`, match the existing convention instead (the `var(..., fallback)` above degrades gracefully regardless).

- [ ] **Step 5: Run the test to verify it passes**

Run: `pnpm test src/components/landing/__tests__/PricingThreeCard.test.tsx`
Expected: PASS — both tests green.

- [ ] **Step 6: Typecheck**

Run: `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/landing/PricingThreeCard.tsx apps/frontend/src/components/landing/__tests__/PricingThreeCard.test.tsx
git commit -m "feat(landing): add Teams entry point (Create a team → /sign-up?intent=team)"
```

---

## Final verification

- [ ] Run each touched test file once more (single-file):
  - `pnpm test src/hooks/__tests__/useOnboardingGate.test.tsx`
  - `pnpm test src/app/onboarding/__tests__/page.test.tsx`
  - `pnpm test src/components/landing/__tests__/PricingThreeCard.test.tsx`
- [ ] `pnpm exec tsc --noEmit` — clean.
- [ ] `pnpm lint` — clean (re-commit any linter auto-edits before pushing).
- [ ] Manual smoke (dev): from the landing page, "Create a team" → sign up → lands on the create-team screen; a normal sign-up → no chooser, straight into `/chat` provisioning; a user with a pending org invite → still forced to the accept-invite screen.
```
