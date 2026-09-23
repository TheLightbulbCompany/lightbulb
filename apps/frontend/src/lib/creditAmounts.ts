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
