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

import { formatUsdCents } from "@/lib/creditAmounts";
import type { RollupDay } from "@/hooks/useCompanyRollup";

// Constructing an Intl.NumberFormat is the expensive half (CLDR resolution);
// formatting is cheap. A 25-row Payments page has two money columns, so an
// uncached pair-per-cell built ~100 formatters per render and doubled on every
// "Load more" and every SWR revalidation. One per distinct currency instead.
const FORMATTERS = new Map<string, Intl.NumberFormat | null>();

function formatterFor(currency: string): Intl.NumberFormat | null {
  if (!FORMATTERS.has(currency)) {
    try {
      FORMATTERS.set(currency, new Intl.NumberFormat("en-US", { style: "currency", currency }));
    } catch {
      FORMATTERS.set(currency, null);
    }
  }
  return FORMATTERS.get(currency) ?? null;
}

/**
 * A Stripe minor-unit amount as text. Falls back to a bare number plus the
 * raw code if the currency is missing or unrecognised, rather than silently
 * formatting it as dollars.
 */
export function formatStripeAmount(amount: number, currency: string | null | undefined): string {
  const code = (currency || "").toUpperCase();
  if (!code) return String(amount);
  const fmt = formatterFor(code);
  // Unknown/absent currency — treat as 2 minor units, the overwhelming default,
  // and never silently render it as dollars.
  const digits = fmt?.resolvedOptions().maximumFractionDigits ?? 2;
  const major = amount / 10 ** digits;
  return fmt ? fmt.format(major) : `${major} ${code}`;
}

/** Unix seconds as a short UTC date.
 *
 *  Locale AND timezone are pinned. The timezone is the half that actually
 *  matters here: a charge at 23:30Z renders "Aug 31" in London and "Sep 1" in
 *  Tokyo, and a table whose whole purpose is matching the Stripe Dashboard line
 *  for line cannot have its dates move with the viewer. Stripe reports in UTC,
 *  so this does too. */
export function formatUnixDay(unix: number): string {
  return new Date(unix * 1000).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** "2026-09-05" → "Sep 5, 2026", pinned to UTC like formatUnixDay. */
export function formatIsoDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
  });
}

/** Microcents → dollars with a typographic minus, the strip's convention. */
export function formatMicrocents(v: number): string {
  const cents = Math.round(v / 10_000);
  return cents < 0 ? `−${formatUsdCents(-cents)}` : formatUsdCents(cents);
}

/** Revenue landed in today's UTC month, from the stored day rows. Null when
 *  the month has no rows at all — that is "no reading", not "$0". */
export function monthToDate(days: RollupDay[], today: string): { month: string; microcents: number } | null {
  const month = today.slice(0, 7);
  const rows = days.filter((d) => d.day.startsWith(month));
  if (rows.length === 0) return null;
  return { month, microcents: rows.reduce((s, d) => s + (d.revenue_microcents ?? 0), 0) };
}

