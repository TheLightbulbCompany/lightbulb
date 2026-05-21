"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth, useOrganization, useOrganizationList, useUser } from "@clerk/nextjs";

/**
 * Phase of the chat-shell onboarding/context gate.
 *
 * - ``loading``         — Clerk hydration + user/org context not settled yet.
 * - ``redirecting``     — User must visit /onboarding (either: never picked
 *                         personal/org, OR has pending org invitations to
 *                         accept). The hook fires ``router.replace`` itself;
 *                         consumers just need to render a loading shell while
 *                         the navigation lands.
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

  // Side effect 1: redirect (idempotent — router.replace dedupes if already
  // on /onboarding). Both the "no row at all" path and the "pending
  // invitations" path land on the same /onboarding screen; the page itself
  // surfaces the right UI based on Clerk state.
  useEffect(() => {
    if (needsOnboarding || needsInvitationFlow) {
      router.replace("/onboarding");
    }
  }, [needsOnboarding, needsInvitationFlow, router]);

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
  if (needsOnboarding || needsInvitationFlow) return "redirecting";
  if (needsAutoActivate) return "auto-activating";
  return "ready";
}
