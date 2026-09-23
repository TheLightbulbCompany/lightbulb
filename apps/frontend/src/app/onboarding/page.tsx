"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { CreateOrganization, OrganizationList, useAuth, useOrganization, useOrganizationList } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { ACCENT_ATTR, THEME_ATTR } from "@/hooks/useChatTheme";

// The page's own heading carries this step, so Clerk's card header would repeat
// it; the logo is optional and can be set later in workspace settings.
// Style objects, not the Tailwind `hidden` class: Clerk's own CSS-in-JS sets
// `display` on these elements after Tailwind, so the class silently loses.
const createAppearance = { elements: { header: { display: "none" }, organizationAvatarUploaderContainer: { display: "none" } } };

export default function OnboardingPage() {
  const router = useRouter();
  const { isLoaded, isSignedIn, orgId } = useAuth();
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { userMemberships, userInvitations, isLoaded: listLoaded, setActive } =
    useOrganizationList({ userMemberships: true, userInvitations: true });
  const [joiningOrgId, setJoiningOrgId] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loaded = isLoaded && isSignedIn && orgLoaded && listLoaded &&
    userMemberships?.data !== undefined && userInvitations?.data !== undefined &&
    !userMemberships?.isLoading && !userInvitations?.isLoading;
  const invitations = userInvitations?.data ?? [];
  const count = userMemberships?.data?.length ?? 0;
  const soleOrgId = loaded && !invitations.length && !organization && !joiningOrgId && count === 1
    ? userMemberships?.data?.[0]?.organization.id : undefined;

  // /chat stamps its theme on <html> and a client redirect from /chat keeps it,
  // which repaints this page's ground as the chat pane (white, or dark). Lift
  // it while onboarding is mounted and put it back on the way out: /chat's
  // bootstrap is an inline script that does NOT re-run on a client navigation.
  useEffect(() => {
    const root = document.documentElement;
    const saved = [THEME_ATTR, ACCENT_ATTR].map((name) => [name, root.getAttribute(name)] as const);
    saved.forEach(([name]) => root.removeAttribute(name));
    return () => saved.forEach(([name, value]) => { if (value !== null) root.setAttribute(name, value); });
  }, []);

  useEffect(() => {
    if (!soleOrgId || !setActive || error) return;
    void setActive({ organization: soleOrgId }).catch(() => {
      setError("We couldn't activate your workspace. Please try again.");
    });
  }, [soleOrgId, setActive, error]);

  useEffect(() => {
    if (!loaded || invitations.length || !organization || orgId !== organization.id) return;
    if (joiningOrgId && organization.id !== joiningOrgId) return;
    router.replace("/chat");
  }, [loaded, invitations.length, organization, orgId, joiningOrgId, router]);

  async function activateJoinedWorkspace(id: string) {
    await userInvitations?.revalidate?.();
    await userMemberships?.revalidate?.();
    await setActive?.({ organization: id });
  }

  async function retryActivation() {
    setError(null);
    if (!joiningOrgId) return;
    try {
      await activateJoinedWorkspace(joiningOrgId);
    } catch {
      setError("We couldn't activate your workspace. Please try again.");
    }
  }

  async function acceptInvitation(invitation: (typeof invitations)[number]) {
    setJoiningOrgId(invitation.publicOrganizationData.id);
    setAccepting(true);
    setError(null);
    let accepted = false;
    try {
      await invitation.accept();
      accepted = true;
      await activateJoinedWorkspace(invitation.publicOrganizationData.id);
    } catch {
      if (!accepted) setJoiningOrgId(null);
      setError("We couldn't join your workspace. Please try again.");
    } finally {
      setAccepting(false);
    }
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-background p-4">
      {error && <p role="alert">{error}</p>}
      {!loaded ? <p role="status">Loading your workspaces…</p> : invitations.length > 0 ? (
        <>
          <h1 className="text-2xl font-bold">You have been invited</h1>
          <p>Accept an invitation to join a workspace on Lightbulb.</p>
          <div className="flex w-full max-w-md flex-col gap-3">
            {invitations.map((invitation) => (
              <div key={invitation.id} className="flex items-center justify-between gap-4 rounded-lg border border-border bg-card p-4">
                <span>{invitation.publicOrganizationData.name || "Workspace"}</span>
                <Button onClick={() => acceptInvitation(invitation)} disabled={accepting}>
                  {accepting && joiningOrgId === invitation.publicOrganizationData.id ? "Accepting…" : "Accept"}
                </Button>
              </div>
            ))}
          </div>
        </>
      ) : joiningOrgId || organization || count === 1 ? (
        <>
          <p role="status">Activating your workspace…</p>
          {error && <Button onClick={retryActivation}>Try again</Button>}
        </>
      ) : count > 1 ? (
        <>
          <h1 className="text-2xl font-bold">Choose your workspace</h1>
          <OrganizationList hidePersonal afterSelectOrganizationUrl="/onboarding" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen />
        </>
      ) : (
        <>
          <h1 className="text-2xl font-bold">Create your workspace</h1>
          <p>Your workspace brings your agents, work, and billing together.</p>
          <CreateOrganization appearance={createAppearance} afterCreateOrganizationUrl="/onboarding" skipInvitationScreen />
        </>
      )}
    </main>
  );
}
