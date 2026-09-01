// Formatting shared by the three Money tabs.
//
// Stripe amounts are in the currency's MINOR unit, and how many minor units
// make a major one is currency-specific. Dividing by 100 is right for USD and
// EUR and 100x WRONG for the zero-decimal currencies (JPY, KRW, VND…): a ¥1000
// charge arrives as `amount: 1000` and rendering "10.00 JPY" understates it by
// two orders of magnitude, on the one surface whose entire value is that it
// reconciles against the Stripe Dashboard line for line.
//
// `Intl.NumberFormat` already knows every currency's minor-unit count, so the
// stdlib answers this — no table of our own to drift. It also gives grouping
// separators, which the hand-rolled path did not ($1,234.50, not $1234.50).

/** Minor units per major unit, resolved from the runtime's own CLDR data. */
function minorUnits(currency: string): number {
  try {
    const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return fmt.resolvedOptions().maximumFractionDigits ?? 2;
  } catch {
    // Unknown/absent currency code — treat as 2, the overwhelming default.
    return 2;
  }
}

/**
 * A Stripe minor-unit amount as text. Falls back to a bare number plus the
 * raw code if the currency is missing or unrecognised, rather than silently
 * formatting it as dollars.
 */
export function formatStripeAmount(amount: number, currency: string | null | undefined): string {
  const code = (currency || "").toUpperCase();
  if (!code) return String(amount);
  const major = amount / 10 ** minorUnits(code);
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: code }).format(major);
  } catch {
    return `${major} ${code}`;
  }
}

/** Unix seconds as a short date. Locale pinned so the same row does not read
 *  differently on two machines — and so a test asserting it is not
 *  runner-dependent. */
export function formatUnixDay(unix: number): string {
  return new Date(unix * 1000).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
