// Its own module, not lib/pricing: tests that stub lib/pricing for the Phone plan keep Spark's real fetch.
const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

/** Spark's price and trial (`GET /os/config`), the same facts the Mac app shows. null on any failure. */
export type SparkPrice = { priceText: string; trialDays: number } | null;

export async function getSparkPrice(): Promise<SparkPrice> {
  try {
    // The same bound as the Phone plan's fetch (lib/pricing.ts): a slow backend must not hold the page.
    const r = await fetch(`${BACKEND_URL}/os/config`, { next: { revalidate: 300 }, signal: AbortSignal.timeout(2500) });
    if (!r.ok) return null;
    const c = await r.json();
    return typeof c?.priceText === "string" && Number.isInteger(c?.trialDays) && c.trialDays > 0
      ? { priceText: c.priceText, trialDays: c.trialDays }
      : null;
  } catch {
    return null;
  }
}

/** "Free for 3 days, then $20/month", or null without the config. */
export function sparkPriceLine(p: SparkPrice): string | null {
  return p ? `Free for ${p.trialDays} days, then ${p.priceText}` : null;
}
