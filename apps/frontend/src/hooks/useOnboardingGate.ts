"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth, useOrganization, useOrganizationList, useUser } from "@clerk/nextjs";
import { clearSignupIntentCookie, readSignupIntentCookie } from "@/lib/signupIntent";

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
    searchParams.get("intent") === "team" ||
    (typeof document !== "undefined" &&
      readSignupIntentCookie(document.cookie) === "team");
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

  // Side effect 1: redirect to /onboarding — but ONLY for the two flows that
  // need an authenticated setup screen:
  //   • pending invitation → the accept surface
  //   • team signup        → Clerk's <CreateOrganization> + invite step
  // Personal signups are NOT redirected; they're completed inline below and
  // fall through to "ready" (straight into /chat). The team cookie is consumed
  // and cleared here so a later personal signup in the same browser isn't
  // mis-tagged as a team.
  useEffect(() => {
    if (needsInvitationFlow) {
      router.replace("/onboarding");
      return;
    }
    if (needsOnboarding && teamIntent) {
      clearSignupIntentCookie();
      router.replace("/onboarding?intent=team");
    }
  }, [needsOnboarding, needsInvitationFlow, teamIntent, router]);

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
  // Only team signups and pending invitations get the /onboarding detour.
  // A fresh personal signup (needsOnboarding && !teamIntent) is completed
  // inline above and falls through to "ready" — no redirect, no /onboarding.
  if (needsInvitationFlow || (needsOnboarding && teamIntent)) return "redirecting";
  if (needsAutoActivate) return "auto-activating";
  return "ready";
}
