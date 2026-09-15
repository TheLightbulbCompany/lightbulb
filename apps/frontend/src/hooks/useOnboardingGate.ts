"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth, useOrganization, useOrganizationList } from "@clerk/nextjs";
import { pendingJoinOrgId } from "@/lib/workspaceJoin";

export type OnboardingGatePhase = "loading" | "redirecting" | "selecting" | "ready";

/** Routing only. useProvisioningState independently verifies the workspace JWT before polling. */
export function useOnboardingGate(): OnboardingGatePhase {
  const router = useRouter();
  const { isLoaded, isSignedIn, orgId } = useAuth();
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { userMemberships, userInvitations, setActive, isLoaded: listLoaded } =
    useOrganizationList({ userMemberships: true, userInvitations: true });
  const loaded = isLoaded && orgLoaded && listLoaded &&
    userMemberships?.data !== undefined && userInvitations?.data !== undefined &&
    !userMemberships?.isLoading && !userInvitations?.isLoading;
  const count = userMemberships?.data?.length ?? 0;
  const invited = (userInvitations?.data?.length ?? 0) > 0;
  const noActiveWorkspace = !orgId && !organization;
  const redirect = loaded && isSignedIn && noActiveWorkspace && (invited || count === 0);
  const soleOrgId = loaded && isSignedIn && !invited && noActiveWorkspace && count === 1
    ? userMemberships?.data?.[0]?.organization.id : undefined;

  useEffect(() => {
    if (!redirect) return;
    // Every sign-up detour lands here. A join link parked in this tab beats
    // workspace creation; the join page drops the token once it settles.
    const joinOrgId = pendingJoinOrgId();
    router.replace(joinOrgId ? `/join/${encodeURIComponent(joinOrgId)}` : "/onboarding");
  }, [redirect, router]);

  useEffect(() => {
    if (!soleOrgId || !setActive) return;
    void setActive({ organization: soleOrgId }).catch(() => router.replace("/onboarding"));
  }, [soleOrgId, setActive, router]);

  if (!loaded || !isSignedIn) return "loading";
  if (redirect) return "redirecting";
  if (orgId && !organization) return "loading";
  if (!organization) return count > 1 ? "selecting" : "loading";
  return orgId === organization.id ? "ready" : "loading";
}
