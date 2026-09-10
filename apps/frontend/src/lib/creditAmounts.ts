/**
 * ONE definition of the credit-purchase amount ladder, shared by the two
 * place an owner buys Claude credits: CreditsStep's standalone top-up.
 *
 * The bounds MIRROR the server, which is the authority and re-clamps whatever
 * arrives: billing_service.MIN/MAX_STARTER_CREDITS_CENTS and the ge=500 /
 * le=100_000 on the top-up request model.
 */
export const MIN_CREDITS_CENTS = 500; // $5
export const MAX_CREDITS_CENTS = 100_000; // $1,000
export const DEFAULT_CREDITS_CENTS = 2000; // $20
export const CREDIT_PRESETS_CENTS = [500, 2000, 5000, 10000]; // $5 / $20 / $50 / $100

/** Money for display. Integer cents in — never float dollars, which is how a
 *  price line becomes "$60.230000000000004".
 *
 *  `toLocaleString`, not string concatenation: the hand-rolled version had no
 *  thousands separator, so Home's revenue hero rendered "$1234567.89". Locale
 *  pinned to en-US — `undefined` resolves the HOST default and renders
 *  "1.234.567,89" on a de-DE machine. Whole dollars keep no decimals ("$14"),
 *  fractional ones keep exactly two ("$14.50"). */
export function formatUsdCents(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", {
    minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
    maximumFractionDigits: 2,
  })}`;
}
