/**
 * The single `subscription_status` → display-label mapping.
 *
 * The raw enum must never reach a user surface: the plan badge is
 * `text-transform: uppercase`, so `trial_local` rendered as "TRIAL_LOCAL".
 * Settings already had this mapping inline; the two chat shells did not, which
 * is exactly the drift one shared helper prevents.
 *
 * Statuses other than the free tier are humanized (underscores -> spaces,
 * first letter capitalized) but otherwise passed through — callers may still
 * layer their own `capitalize`/`uppercase` treatment on top, as Settings and
 * the chat-shell badges do. The humanization step exists because CSS
 * `text-transform` has no notion of `_`: a raw Stripe status like
 * "past_due" rendered under `capitalize` reads "Past_due" (only the very
 * first character gets capitalized, since there's no whitespace word
 * boundary) instead of "Past due".
 */
export function planLabel(
  status: string | null | undefined,
  opts?: { expired?: boolean },
): string | null {
  if (!status) return null;
  if (status === "trial_local") {
    // Stays SHORT deliberately. This badge shares a 280px sidebar footer row
    // with the user's name, which already ellipsizes, so a longer label here
    // squeezes the name away. The free-tier countdown lives on its own line
    // instead — see FreeTierNote, which is the ONE place that copy exists.
    return opts?.expired ? "Free tier ended" : "Free tier";
  }
  // Stripe's "trialing" is not a word people use (dev walk W12-14).
  if (status === "trialing") return "Free trial";
  const spaced = status.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Shared `expired` computation for `planLabel`'s sidebar caller
 * (`WorkspaceSidebar.tsx`) — without it, its footer
 * `.plan-badge` said "Free tier" even after the window elapsed, while
 * `TrialBanner`/`OutOfCreditsBanner` on the very same screen already showed
 * the blocked/expired state. Same arithmetic as the pre-existing, separately
 * tested `isTrialExpired` local in `settings/page.tsx` (trial_end is the
 * OVERALL free-tier window expiry, stamped once at signup — see that file's
 * comment for the full rationale); left un-extracted there since it already
 * has its own coverage and this helper exists for the two NEW call sites.
 */
export function isFreeTierExpired(
  status: string | null | undefined,
  trialEndSec: number | null | undefined,
  nowMs: number,
): boolean {
  return status === "trial_local" && trialEndSec != null && trialEndSec * 1000 < nowMs;
}

/**
 * Length of the free-tier window, in whole days. The backend stamps `trial_end`
 * as EXACTLY signup + this many days (`trial_cap._is_expired`), so the count
 * below can never legitimately exceed it — see the clamp in `freeTierDaysLeft`.
 */
export const FREE_TIER_DAYS = 30;

/**
 * Whole days left in the free-tier window. Consumed by `FreeTierNote`, which is
 * the one surface that renders this count — `planLabel` deliberately does NOT
 * take it (the badge shares a cramped footer row; see above).
 *
 * Rounds UP, so a window with six hours to run reads "1 day left" rather than
 * "0": the badge is a nudge, and telling someone who still has time that they
 * have none is the one direction worth avoiding. Returns null when there is no
 * window to describe (not on the free tier, or no `trial_end` — legacy dev
 * rows have none, and the backend treats a missing stamp as NOT expired).
 *
 * Clamped to `FREE_TIER_DAYS` (30): `trial_end` is signup + exactly that many days,
 * but `nowMs` is the CLIENT clock — a machine a few minutes slow makes
 * `trial_end - now` exceed the true window and the ceil round to 4, so a fresh
 * signup read "31 days left" on a 30-day trial. The clamp caps it at the promise.
 *
 * Because it rounds up, a 0 is only reachable at exact millisecond equality,
 * and callers check `isFreeTierExpired` first anyway — so 1 is the last day,
 * which is what FreeTierNote keys its "Last day" copy on.
 */
export function freeTierDaysLeft(
  status: string | null | undefined,
  trialEndSec: number | null | undefined,
  nowMs: number,
): number | null {
  if (status !== "trial_local" || trialEndSec == null) return null;
  return Math.min(
    FREE_TIER_DAYS,
    Math.max(0, Math.ceil((trialEndSec * 1000 - nowMs) / 86_400_000)),
  );
}

/**
 * Today's managed-model allowance line for capped owners (trial, Base, paid with no
 * provider). A tripped cap wins over the percentage, which lags it by the backend's
 * ~60 s cache. Null when there's nothing to say (uncapped owner, OpenRouter unreadable).
 */
export function allowanceLine(
  account:
    | { capped_today?: boolean; allowance?: { used_usd: number; limit_usd: number } | null }
    | null
    | undefined,
): string | null {
  if (account?.capped_today) return "Daily allowance used — resets at 00:00 UTC.";
  const a = account?.allowance;
  if (!a || a.limit_usd <= 0) return null;
  const pct = Math.min(100, Math.round((a.used_usd / a.limit_usd) * 100));
  return `Today: ${pct}% of your daily allowance used · resets 00:00 UTC`;
}
