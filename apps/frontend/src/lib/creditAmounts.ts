/**
 * ONE definition of the credit-purchase amount ladder, shared by the two
 * places an owner buys Claude credits:
 *   - CreditsStep — the standalone top-up
 *   - ProviderPicker's StarterCreditsPicker — bundled into the subscription
 *
 * They were separate arrays that had already drifted: the top-up offered
 * [$10,$20,$50,$100] and the bundled picker [$5,$20,$50,$100], so the same
 * owner buying the same balance saw a different ladder depending on the door,
 * and any future change (a new tier, a floor change) had to be made twice.
 *
 * The bounds MIRROR the server, which is the authority and re-clamps whatever
 * arrives: billing_service.MIN/MAX_STARTER_CREDITS_CENTS and the ge=500 /
 * le=100_000 on the top-up + trial-checkout request models.
 */
export const MIN_CREDITS_CENTS = 500; // $5
export const MAX_CREDITS_CENTS = 100_000; // $1,000
export const DEFAULT_CREDITS_CENTS = 2000; // $20
export const CREDIT_PRESETS_CENTS = [500, 2000, 5000, 10000]; // $5 / $20 / $50 / $100

/** Money for display. Integer cents in — never float dollars, which is how a
 *  price line becomes "$60.230000000000004". */
export function formatUsdCents(cents: number): string {
  return cents % 100 === 0 ? `$${cents / 100}` : `$${(cents / 100).toFixed(2)}`;
}
