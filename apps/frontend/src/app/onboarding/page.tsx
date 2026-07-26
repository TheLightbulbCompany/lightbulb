"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  CreateOrganization,
  useAuth,
  useOrganization,
  useOrganizationList,
  useUser,
} from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { Button } from "@/components/ui/button";
import { Users, Mail } from "lucide-react";
import { InviteTeammatesStep } from "@/components/onboarding/InviteTeammatesStep";
import { clearSignupIntentCookie } from "@/lib/signupIntent";

function OnboardingFlow() {
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
  const [acceptingId, setAcceptingId] = useState<string | null>(null);

  const pendingInvitations = userInvitations?.data ?? [];

  // This page handles only the two flows that need an authenticated setup
  // screen: team creation and invitation acceptance. Personal signups are
  // completed by useOnboardingGate and never reach here — so there is no
  // personal path, no auto-complete, and no chooser.
  //
  // Team intent arrives as ?intent=team, either directly from a completed
  // /sign-up (SignUpWithIntent resolves the redirect target itself so team
  // signups land here without ever mounting /chat) or forwarded by
  // useOnboardingGate for the in-app "Create a team" flow. It stays in the
  // URL across Clerk's org-creation soft nav via
  // afterCreateOrganizationUrl="/onboarding?intent=team", so `mode` is
  // derived straight from the URL — no sticky state needed.
  //
  // Tenancy invariant: pending invitations force the invitations screen — a
  // user can't hold personal tenancy AND a pending org invite at once.
  const intentTeam = searchParams.get("intent") === "team";
  const forcedInvitations = isLoaded && orgsLoaded && pendingInvitations.length > 0;
  const mode: "org" | "invitations" | null = forcedInvitations
    ? "invitations"
    : intentTeam
      ? "org"
      : null;

  // Analytics only (no state update — that would trip the cascading-render
  // lint rule): record that the org-creation flow opened.
  useEffect(() => {
    if (mode === "org") capture("workspace_type_selected", { type: "org" });
  }, [mode]);

  // Team intent has now been consumed for routing (this page is rendering
  // the org-creation UI off of it). Clear the cookie so a later personal
  // signup in the same browser doesn't get mis-tagged as a team by the
  // cookie's 30-minute TTL. This is the SignUpWithIntent entry path's
  // clearing point — team signups redirect here directly and never mount
  // /chat, so useOnboardingGate's own clear (for the in-app "Create a team"
  // flow, which still goes through /chat) never runs for them. Clearing an
  // already-absent cookie is a harmless no-op, so this is safe to also fire
  // when useOnboardingGate got there first.
  useEffect(() => {
    if (mode === "org") clearSignupIntentCookie();
  }, [mode]);

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

  // Completion effect for the org paths. Skips the org-creation and
  // invitations UIs (they own their own rendering).
  useEffect(() => {
    if (!isLoaded || !orgLoaded || !orgsLoaded) return;
    if (mode === "org" || mode === "invitations") return;

    // Path 1: org already active (just created, or an accepted invite that
    // Clerk auto-activated) → mark onboarded + redirect.
    if (organization) {
      completeOnboardingAndRedirect();
      return;
    }
    // Path 2: has memberships but no active org (accepted invite propagated,
    // or fresh login on a new device) → activate the first membership, then
    // redirect.
    const memberships = userMemberships?.data;
    if (memberships && memberships.length > 0 && setActive) {
      setActive({ organization: memberships[0].organization.id }).then(
        completeOnboardingAndRedirect,
      );
      return;
    }
    // Else: no org, no membership. Two cases:
    //   • An invite accept is in flight (acceptingId set): Clerk has dropped
    //     the invitation but the membership hasn't propagated yet. HOLD here
    //     (spinner below) — Path 2 fires the moment it lands. Redirecting to
    //     /chat now would bounce the invitee through the gate during that gap
    //     and risk mis-classifying them as personal.
    //   • Otherwise a stray user reached /onboarding without a team/invite
    //     context (personal is owned by the gate) → send them to /chat.
    if (acceptingId === null) {
      router.replace("/chat");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoaded, orgLoaded, orgsLoaded, organization, userMemberships, setActive, mode, acceptingId, router]);

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

  // Transient spinner: resolving org/membership state, holding while an
  // accepted invitation propagates, or redirecting a stray visitor to /chat.
  return (
    <div className="flex flex-col items-center justify-center min-h-screen gap-4 bg-background">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-primary" />
      <p className="text-sm text-muted-foreground">
        {acceptingId !== null ? "Joining your team…" : "One moment…"}
      </p>
    </div>
  );
}

// OnboardingFlow reads useSearchParams() (for ?intent=team), which triggers a
// CSR bailout at static-prerender time. A Suspense boundary is required so
// `next build` can prerender /onboarding. The fallback mirrors the personal
// spinner so there's no visual flash.
export default function OnboardingPage() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-col items-center justify-center min-h-screen gap-4 bg-background">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-primary" />
        </div>
      }
    >
      <OnboardingFlow />
    </Suspense>
  );
}
