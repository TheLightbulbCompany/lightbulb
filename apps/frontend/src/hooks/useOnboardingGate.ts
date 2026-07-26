"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth, useOrganization, useOrganizationList, useUser } from "@clerk/nextjs";
import { hasTeamSignupIntentCookie } from "@/lib/signupIntent";

/**
 * Phase of the chat-shell onboarding/context gate.
 *
 * - ``loading``         — Clerk hydration + user/org context not settled yet.
 * - ``redirecting``     — User must visit /onboarding: either a team signup
 *                         (needs Clerk's org-creation surface) or a pending
 *                         invitation to accept. Personal signups are NOT
 *                         redirected — they're completed inline and go straight
 *                         to /chat. The hook fires ``router.replace`` itself;
 *                         consumers just render a loading shell while it lands.
 * - ``auto-activating`` — User has org memberships but no active organization
 *                         in this Clerk session (fresh login on a new device).
 *                         The hook calls ``setActive`` on the first membership;
 *                         the next render flips ``organization`` and the hook
 *                         transitions to ``ready``.
 * - ``ready``           — Clerk context is final. Safe to render owner_id-aware
 *                         hooks (useAgents / useBilling / useContainerStatus)
 *                         and to call ``api.syncUser``.
 */
export type OnboardingGatePhase =
  | "loading"
  | "redirecting"
  | "auto-activating"
  | "ready";

/**
 * The chat shell's onboarding/context gate, extracted from ChatLayout so its
 * state machine — and its critical side effects — live in one named place
 * with their own tests.
 *
 * The "block the whole shell until this resolves" rule prevents the
 * personal-vs-org JWT race that produced phantom personal billing rows in
 * prod. The hook bundles the three orthogonal conditions (needs-onboarding,
 * needs-auto-activate, needs-invitation-flow) into a single phase so
 * consumers can't accidentally read just one and miss another.
 *
 * The hook owns the side effects: ``router.replace("/onboarding")`` for the
 * redirect path and ``setActive`` for the auto-activate path. The consumer
 * (ChatLayout) just reads ``phase`` to decide whether to render a loading
 * shell or the chat surface.
 */
export function useOnboardingGate(): OnboardingGatePhase {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isSignedIn } = useAuth();
  const { user, isLoaded: userLoaded } = useUser();
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { userMemberships, userInvitations, setActive, isLoaded: orgListLoaded } =
    useOrganizationList({
      userMemberships: true,
      userInvitations: true,
    });

  const clerkLoaded = userLoaded && orgLoaded && orgListLoaded;
  const isOnboarded =
    (user?.unsafeMetadata as Record<string, unknown> | undefined)?.onboarded === true;
  const hasMemberships = (userMemberships?.data?.length ?? 0) > 0;
  const hasPendingInvitations = (userInvitations?.data?.length ?? 0) > 0;

  // Team signal: the landing "Create a team" CTA sets ?intent=team (single-step
  // signups) and a cookie (survives Clerk's multi-step signup, which drops the
  // query). Only team signups are routed to /onboarding; personal signups are
  // completed here and dropped straight into /chat.
  const teamIntent =
    searchParams.get("intent") === "team" || hasTeamSignupIntentCookie();
  const personalDoneRef = useRef(false);

  // The three "not ready" conditions. They're mutually exclusive by
  // construction — needsOnboarding requires !hasMemberships, needsAutoActivate
  // requires hasMemberships, needsInvitationFlow requires hasPendingInvitations
  // — but encoding that invariant as separate booleans keeps each rule
  // readable and matches the inline form ChatLayout had previously.
  const needsOnboarding =
    clerkLoaded &&
    isSignedIn === true &&
    !isOnboarded &&
    !hasMemberships &&
    !hasPendingInvitations &&
    !organization;
  const needsAutoActivate =
    clerkLoaded && isSignedIn === true && !organization && hasMemberships;
  // Tenancy invariant: pending invitations beat the unsafeMetadata.onboarded
  // flag. A user who completed personal onboarding earlier and was later
  // invited to an org MUST be routed to /onboarding (where the invitations
  // surface accepts), not silently sent to /chat in personal context. The
  // invariant forbids personal-tenancy + pending-org-invite coexisting.
  const needsInvitationFlow =
    clerkLoaded &&
    isSignedIn === true &&
    !hasMemberships &&
    hasPendingInvitations &&
    !organization;
  // Team creation is available to any signed-in user with team intent and no
  // existing org context — NOT only fresh signups. An already-onboarded
  // personal user who clicks "Create a team" must still be routed to the
  // org-creation surface, so this deliberately does NOT require !isOnboarded.
  const needsTeamCreation =
    clerkLoaded &&
    isSignedIn === true &&
    teamIntent &&
    !hasMemberships &&
    !hasPendingInvitations &&
    !organization;

  // Side effect 1: redirect to /onboarding — but ONLY for the two flows that
  // need an authenticated setup screen:
  //   • pending invitation → the accept surface
  //   • team creation      → Clerk's <CreateOrganization> + invite step
  // Personal signups are NOT redirected; they're completed inline below and
  // fall through to "ready" (straight into /chat).
  //
  // Deliberately does NOT clear the team-signup-intent cookie. This effect
  // only starts the navigation — `router.replace` doesn't unmount /chat
  // synchronously, App Router keeps it committed until the /onboarding RSC
  // payload lands (100-500ms). `teamIntentPending` in `useProvisioningState`
  // is recomputed from the cookie on every render with no latch, so clearing
  // it HERE re-arms that hook's poll suppression while /chat is still
  // mounted — any re-render in that window (WS connect, agents.list
  // resolving, a Clerk store update) fires `/container/status` and
  // provisions the throwaway personal container this cookie exists to
  // prevent. The cookie is cleared where it is actually CONSUMED instead:
  // on /onboarding once it has taken over rendering the team-creation UI
  // (`mode === "org"` in app/onboarding/page.tsx) — the one place both entry
  // paths (this redirect, and a direct `?intent=team` landing) converge.
  useEffect(() => {
    if (needsInvitationFlow) {
      router.replace("/onboarding");
      return;
    }
    if (needsTeamCreation) {
      router.replace("/onboarding?intent=team");
    }
  }, [needsTeamCreation, needsInvitationFlow, router]);

  // Side effect 1b: complete a fresh personal signup in place. No org, no
  // membership, no invitation, no team intent → this is a personal user, so
  // mark them onboarded (best-effort — the flag is non-critical for routing,
  // which returns "ready" regardless) and let them into /chat without a
  // /onboarding detour.
  useEffect(() => {
    if (!(needsOnboarding && !teamIntent) || personalDoneRef.current || !user) return;
    personalDoneRef.current = true;
    user
      .update({
        unsafeMetadata: {
          ...(user.unsafeMetadata as Record<string, unknown> | undefined),
          onboarded: true,
        },
      })
      .catch(() => {
        // Best-effort: a failed flag write just means the next login re-runs
        // this harmless idempotent path. Routing is unaffected.
      });
  }, [needsOnboarding, teamIntent, user]);

  // Side effect 2: auto-activate the first membership. Clerk doesn't persist
  // active-org state across sessions, so without this a user who created an
  // org on laptop A would land on /onboarding on laptop B despite already
  // being a member.
  useEffect(() => {
    if (!needsAutoActivate || !setActive) return;
    const first = userMemberships?.data?.[0];
    if (!first) return;
    setActive({ organization: first.organization.id }).catch((err: unknown) => {
      console.error("Auto-activate first org membership failed:", err);
    });
  }, [needsAutoActivate, setActive, userMemberships]);

  if (!clerkLoaded || isSignedIn !== true) return "loading";
  // Only team creation and pending invitations get the /onboarding detour.
  // A fresh personal signup (needsOnboarding && !teamIntent) is completed
  // inline above and falls through to "ready" — no redirect, no /onboarding.
  if (needsInvitationFlow || needsTeamCreation) return "redirecting";
  if (needsAutoActivate) return "auto-activating";
  return "ready";
}
