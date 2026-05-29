"use client";

import { useOrganization, useUser } from "@clerk/nextjs";

import { InboxPage } from "@/components/teams/inbox/InboxPage";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";

/**
 * Teams Inbox panel — thin shim that resolves Clerk identity (companyId =
 * organization id when present, else the user id; currentUserId = the signed-in
 * Clerk user) and delegates rendering to <InboxPage />. The Teams BFF resolves
 * the actual workspace/owner from the Clerk JWT, so these ids are used purely
 * client-side for localStorage namespacing + the assignee-filter "Assigned to
 * me" preset.
 *
 * While Clerk is loading, render nothing — TeamsLayout already shows a global
 * loading state for `useTeamsWorkspaceStatus.kind === "loading"`, so the brief
 * gap here is invisible to users.
 */
export function InboxPanel() {
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { user, isLoaded: userLoaded } = useUser();

  // Fetch the company roster so InboxPage can resolve agent names — the
  // NeedsAttentionBand failed-run rows and the toolbar's agent filter both
  // need it; without it they fall back to raw `agent_…` ids. `read` calls
  // `useSWR` internally, so it must run unconditionally above the early
  // return (rules of hooks).
  const { read } = useTeamsApi();
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");

  if (!orgLoaded || !userLoaded || !user) return null;

  // Owner-id convention: org membership uses the org id, personal accounts
  // use the user id. Matches how the BFF keys per-owner workspaces.
  const companyId = organization?.id ?? user.id;
  const currentUserId = user.id;

  return (
    <InboxPage
      companyId={companyId}
      currentUserId={currentUserId}
      agents={agentsData?.agents ?? []}
    />
  );
}
