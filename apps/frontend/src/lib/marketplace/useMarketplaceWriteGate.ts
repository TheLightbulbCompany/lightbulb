"use client";

import { useOrganization } from "@clerk/nextjs";

/**
 * Mirrors the backend's `require_org_admin` gate for marketplace WRITE
 * actions (publish, hire/deploy, handle, payouts onboarding).
 *
 * Returns `restricted: true` ONLY when the active Clerk membership is an
 * org member whose role is not "org:admin". Personal users (no active
 * org → `membership === null`) and org admins pass through unchanged.
 *
 * Keep this in sync with `require_org_admin` in
 * `apps/backend/routers/marketplace_listings.py` (and friends). The
 * frontend gate is a UX courtesy; the backend gate is the security
 * boundary.
 */
export function useMarketplaceWriteGate(): { restricted: boolean } {
  const { membership } = useOrganization();
  // `membership` is undefined while Clerk is still resolving the active
  // org, null for personal users (no active org), or an object with
  // `.role` for org members. Treat undefined as "still loading" → not
  // restricted (the backend will 403 if the user actually tries to
  // write before Clerk loads, but in practice Clerk's hooks are eager
  // and this race is invisible).
  const restricted = !!membership && membership.role !== "org:admin";
  return { restricted };
}
