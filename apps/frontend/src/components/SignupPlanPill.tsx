"use client";

import { useSyncExternalStore } from "react";

/**
 * The cost line in the sign-up page's feature panel — which is NOT the same
 * claim for both signup paths.
 *
 * A personal signup is auto-enrolled in the card-less free tier
 * (`user_provision.ensure_personal_trial_default`). A TEAM signup is not:
 * that helper is personal-only, and the team-intent cookie actively suppresses
 * the `/container/status` poll that would otherwise create a `trial_local`
 * row. Team billing is a $50 base plus $20 per seat, charged at checkout.
 *
 * So a single unconditional "Free for 3 days · no credit card, nothing to
 * cancel" — which is what this panel shipped at first — is a specific, false
 * promise to everyone arriving via the landing page's "Create a team" CTA,
 * which routes to `/sign-up?intent=team`.
 *
 * Reads `window.location` rather than `useSearchParams`, matching
 * {@link SignupIntentCookie} and for the same reason: `useSearchParams` would
 * force a CSR-bailout Suspense boundary on this page.
 *
 * Via `useSyncExternalStore` (the same shape as `useIsDesktop`) rather than a
 * setState-in-effect, so React owns the server-vs-client difference instead of
 * a cascading render. The server snapshot is `false` — personal — which is
 * both the safe default and the large majority of arrivals; the team copy
 * swaps in at hydration.
 */

/** The query never changes after load, so there is nothing to subscribe to. */
function subscribe(): () => void {
  return () => {};
}

function getSnapshot(): boolean {
  return new URLSearchParams(window.location.search).get("intent") === "team";
}

export function SignupPlanPill() {
  const teamIntent = useSyncExternalStore(subscribe, getSnapshot, () => false);

  return (
    <div>
      <div className="auth-feat-title">{teamIntent ? "Per-seat billing" : "Free for 3 days"}</div>
      <div className="auth-feat-desc">
        {teamIntent ? "$50 a month plus $20 per teammate." : "No credit card. Nothing to cancel."}
      </div>
    </div>
  );
}
