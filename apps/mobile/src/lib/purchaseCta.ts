/**
 * Which backend gates are about money, and what iOS says instead.
 *
 * The app sells plans through In-App Purchase (3.1.1); these gates therefore
 * show the StoreKit paywall to admins when the server offers products, and
 * neutral local copy otherwise. Never the server's web-purchase copy. (The
 * build Apple rejected on 2026-08-12 carried five "Upgrade on thelightbulb.company"
 * CTAs — a call to action for purchase outside the app.)
 *
 * Deleting the hardcoded button labels is NOT enough, and that is why this
 * module exists rather than a few inline edits. The gate copy itself comes
 * from the server (`provision_gate.py`) and mobile renders it verbatim:
 * `trial_daily_cap` ships "You've used today's free allowance — Upgrade to
 * keep going", `trial_expired` ships "Pick a plan to keep your agent". Those
 * strings are calls to action, and the web frontend needs them, so the fix
 * cannot be to change what the backend sends. iOS has to decline the server's
 * copy for these codes and use its own.
 *
 * Hence the rule for a purchase gate on iOS: no action button, no admin note
 * pointing at the billing site, and LOCAL copy — never `status.message`. The
 * codes are listed rather than pattern-matched so a new backend gate defaults
 * to the current behaviour and shows up in review as a deliberate choice; the
 * unit test pins both this list and provision_gate.py's full set of codes, so
 * a new money-shaped gate that nobody classified fails there.
 *
 * Provider-setup gates (`provider_required`, `oauth_required`,
 * `byo_provider_required`, `invitation_pending`) are deliberately NOT here.
 * They cost nothing and direct nobody to a purchase.
 */

/**
 * Gates that mean "this account has NO subscription", and are therefore the
 * only ones a plan paywall can actually resolve.
 *
 * The distinction is not cosmetic. `credits_required` and `payment_past_due`
 * both fire for owners who ALREADY PAY: the first has an active plan and an
 * empty prepaid balance, the second has an active plan whose card failed.
 * Selling either of them another monthly subscription takes a second payment,
 * adds no credits, fixes no card, and leaves the very same gate in place — so
 * the customer is billed twice and still blocked.
 *
 * Kept separate from PURCHASE_GATE_COPY below, which is a different question:
 * that one asks "would the server's copy read as a call to action on iOS",
 * which is true of every money-shaped gate including these two.
 */
export const SUBSCRIPTION_GATES = new Set([
  "trial_daily_cap",
  "trial_expired",
  "signup_required",
  "subscription_required",
]);

/** True when a plan purchase is what actually unblocks this owner. */
export function isSubscriptionGate(code: string | null | undefined): boolean {
  return code != null && SUBSCRIPTION_GATES.has(code);
}

/** Gate codes where following the CTA would lead to a purchase. */
const PURCHASE_GATE_COPY: Record<string, { title: string; body: string }> = {
  trial_daily_cap: {
    title: "Daily free limit reached",
    body: "You've used today's free allowance. It resets tomorrow.",
  },
  trial_expired: {
    title: "This workspace is reading-only",
    body: "Your team's record is all here, and people can still message. Agents start again when an admin picks the plan.",
  },
  signup_required: {
    title: "No active plan",
    body: "This account doesn't have an active plan yet.",
  },
  subscription_required: {
    title: "No active plan",
    body: "This account doesn't have an active plan right now.",
  },
  payment_past_due: {
    title: "Payment problem",
    body: "There's a problem with the payment method on this account.",
  },
  team_billing_required: {
    title: "Workspace not set up",
    body: "This workspace doesn't have an active plan yet.",
  },
  credits_required: {
    title: "Out of credits",
    body: "This account has run out of inference credits.",
  },
  // A paying owner at today's included usage — never trial wording.
  included_daily_cap: {
    title: "Today's included usage is used up",
    body: "You've used today's included usage. It resets at 00:00 UTC.",
  },
  // Teams routines specifically (`/teams/routines*`), not a provision_gate.py
  // code — same "no CTA on iOS" rule applies to its 402 body.
  routines_require_subscription: {
    title: "Not available on this plan",
    body: "Routines aren't available on this plan.",
  },
};

export function isPurchaseGate(code: string | null | undefined): boolean {
  return code != null && code in PURCHASE_GATE_COPY;
}

/**
 * iOS-local copy for a purchase gate — state only, no instruction to buy.
 * Returns null for every other code, whose server copy is fine to render.
 */
export function purchaseGateCopy(
  code: string | null | undefined,
): { title: string; body: string } | null {
  if (code == null) return null;
  return PURCHASE_GATE_COPY[code] ?? null;
}

