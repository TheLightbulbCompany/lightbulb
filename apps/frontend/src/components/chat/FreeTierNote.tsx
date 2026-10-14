"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
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
/** How often the clock below re-reads. Hourly is plenty for a day counter and
 * costs nothing; the point is only that it re-reads at all. */
const TICK_MS = 60 * 60 * 1000;

/** At or under this many days left, the note switches to the warning state. */
const NUDGE_DAYS = 5;

export function FreeTierNote() {
  const { account } = useBilling();
  // Date.now() is impure, so it cannot be read during render — but it also
  // cannot be frozen at mount. This sits in a sidebar footer that mounts once
  // per session, and a workspace tab routinely stays open for days. With a
  // frozen `nowMs` an owner who opened the app with 2 days left would still
  // read "2 days left" forty-eight hours later, and `isFreeTierExpired` would
  // never flip — the note would keep promising runway while the backend was
  // already returning the paywall mid-conversation, which is the exact failure
  // this component exists to prevent.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const status = account?.subscription_status;
  if (isFreeTierExpired(status, account?.trial_end, nowMs)) return null;
  const daysLeft = freeTierDaysLeft(status, account?.trial_end, nowMs);
  if (daysLeft == null) return null;

  // The final stretch gets louder: warn colour and a direct "Choose a plan".
  // `freeTierDaysLeft` rounds UP, so "1" is the last day, and says so.
  const urgent = daysLeft <= NUDGE_DAYS;
  return (
    <p className={urgent ? "free-tier-note free-tier-note--urgent" : "free-tier-note"}>
      {daysLeft === 1
        ? "Last day of your free trial"
        : `${daysLeft} days left on your free trial`}
      {" · "}
      <Link href="/settings?panel=billing">{urgent ? "Choose a plan" : "See plans"}</Link>
    </p>
  );
}
