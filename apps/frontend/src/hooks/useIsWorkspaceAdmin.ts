"use client";

import { useOrganization } from "@clerk/nextjs";

/**
 * Whether the current user administers this workspace: a personal owner
 * (no org membership) is implicitly the admin of their own resources; org
 * members count only with the `org:admin` role.
 *
 * While Clerk is still loading this reads as NOT admin: an unknown caller
 * must not see admin-only UI, even for a frame. Callers that latch a choice
 * from it (Settings' deep-linked panel) re-resolve once it flips. Every write
 * this gates is re-enforced server-side (403).
 *
 * This is the single source for the "personal owner OR org admin" gate.
 * Note the distinct strict check `membership?.role === "org:admin"`
 * (org-admin only, personal owners excluded) used by org-specific UI —
 * don't collapse the two.
 */
export function useIsWorkspaceAdmin(): boolean {
  const { isLoaded, membership } = useOrganization();
  if (!isLoaded) return false;
  return !membership || membership.role === "org:admin";
}
