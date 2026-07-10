"use client";

import { Suspense, useEffect, useRef, useState } from "react";
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
import { Button } from "@/components/ui/button";
import { Users, Mail } from "lucide-react";
import { InviteTeammatesStep } from "@/components/onboarding/InviteTeammatesStep";

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
  // Only ever set to "org" (via the team-intent effect). "invitations" mode
  // is derived from forcedInvitations, never written into state.
  const [explicitMode, setExplicitMode] = useState<"org" | null>(null);
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
