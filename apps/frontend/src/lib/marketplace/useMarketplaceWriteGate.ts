"use client";

import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

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
  const restricted = !useIsWorkspaceAdmin();
  return { restricted };
}
