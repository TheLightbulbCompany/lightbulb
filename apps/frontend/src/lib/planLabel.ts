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
    return opts?.expired ? "Free tier ended" : "Free tier";
  }
  const spaced = status.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Shared `expired` computation for `planLabel`'s two chat-shell callers
 * (`WorkspaceSidebar.tsx`, `ChatLayout.tsx`) — without it, their footer
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
