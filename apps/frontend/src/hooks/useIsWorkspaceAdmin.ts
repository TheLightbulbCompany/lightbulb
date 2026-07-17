"use client";

import { useOrganization } from "@clerk/nextjs";

/**
 * Whether the current user administers this workspace: a personal owner
 * (no org membership) is implicitly the admin of their own resources; org
 * members count only with the `org:admin` role.
 *
 * `membership` is undefined while Clerk is still resolving the active org,
 * which reads as admin here — a UX-only race, invisible in practice because
 * Clerk's hooks are eager, and every write this gates is re-enforced
 * server-side (403).
 *
 * This is the single source for the "personal owner OR org admin" gate.
 * Note the distinct strict check `membership?.role === "org:admin"`
 * (org-admin only, personal owners excluded) used by org-specific UI —
 * don't collapse the two.
 */
export function useIsWorkspaceAdmin(): boolean {
  const { membership } = useOrganization();
  return !membership || membership.role === "org:admin";
}
