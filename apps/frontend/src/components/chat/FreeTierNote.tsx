"use client";

import Link from "next/link";
import { useState } from "react";
import { useBilling } from "@/hooks/useBilling";
import { freeTierDaysLeft, isFreeTierExpired } from "@/lib/planLabel";

/**
 * The free tier's countdown, in the sidebar footer under the user row.
 *
 * There was no countdown for this tier anywhere. `TrialBanner` looks like one
 * but only matches `subscription_status === "trialing"` — a LEGACY Stripe
 * trial — so it never renders for a `trial_local` owner, and its own header
 * says it is safe to delete once the last pre-cutover row converts. The plan
 * badge beside the user's name said "Free tier" and nothing else for the whole
 * window. Since that window is wall-clock from signup and burns down whether
 * or not anyone opens the app, an owner could lose all three days without a
 * single surface mentioning a deadline, and then meet it as a paywall
 * mid-conversation.
 *
 * Its own line rather than more text in the badge: the badge shares a 280px
 * footer row with a name that already ellipsizes, and "FREE TIER · 2 DAYS
 * LEFT" in uppercase caps-tracking would leave almost nothing for the name.
 *
 * Renders only for a live free tier. An ELAPSED one is deliberately left to
 * the existing blocked-state surfaces (`ChatErrorCard`'s in-place paywall and
 * the "Free tier ended" badge), which say it far better than one line here
 * could — and which the owner is about to hit anyway.
 */
export function FreeTierNote() {
  const { account } = useBilling();
  // Date.now() is impure; capture once so this is stable across re-renders.
  // Same pattern as the two shells' own `nowMs`.
  const [nowMs] = useState(() => Date.now());

  const status = account?.subscription_status;
  if (isFreeTierExpired(status, account?.trial_end, nowMs)) return null;
  const daysLeft = freeTierDaysLeft(status, account?.trial_end, nowMs);
  if (daysLeft == null) return null;

  return (
    <p className="free-tier-note">
      {daysLeft <= 0 ? "Last day of your free trial" : `${daysLeft} day${daysLeft === 1 ? "" : "s"} left on your free trial`}
      {" · "}
      <Link href="/settings?panel=billing">See plans</Link>
    </p>
  );
}
