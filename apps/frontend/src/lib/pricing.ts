/**
 * The one plan's public price, read from the backend that bills it
 * (`GET /billing/prices`: WORKSPACE_PRICE_USD, SEATS_INCLUDED, SEAT_PRICE_USD,
 * the trial length), so the site never carries a second copy of a price. Server
 * side only, cached 5 minutes. `null` when the API cannot be read: callers show
 * neutral copy rather than present a number as current terms.
 */
export type Pricing = {
  teamPriceUsd: number;
  includedMembers: number;
  seatPriceUsd: number;
  trialDays: number;
  /** The Front desk add-on (a texting number), a month. null while the backend rolls and has not sent it yet. */
  frontDeskPriceUsd: number | null;
  /** Managed AI is billed at the model provider's price times this (openrouter-pricing.json). */
  managedMarkup: string;
  /** The voice receptionist plan: a monthly price with minutes included, then a per-minute rate. null until the backend sends them. */
  voiceMonthlyUsd: number | null;
  voiceIncludedMinutes: number | null;
  voiceOverageUsd: number | null;
};

export const PRICING_REVALIDATE_S = 300;

// A stalled billing API must not hold the marketing pages: give up fast and render neutral copy.
const PRICING_FETCH_TIMEOUT_MS = 2500;

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

export async function getPricing(): Promise<Pricing | null> {
  try {
    const res = await fetch(`${BACKEND_URL}/billing/prices`, {
      next: { revalidate: PRICING_REVALIDATE_S },
      signal: AbortSignal.timeout(PRICING_FETCH_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as Record<string, unknown>;
    const n = (k: string) => (typeof body[k] === "number" && Number.isFinite(body[k]) ? (body[k] as number) : null);
    const [team, included, seat, trial, frontDesk] = [
      n("workspace_price_usd"),
      n("included_members"),
      n("seat_price_usd"),
      n("trial_days"),
      n("front_desk_price_usd"),
    ];
    const [voiceMonthly, voiceMinutes, voiceOverage] = [n("voice_plan_monthly_usd"), n("voice_included_minutes"), n("voice_overage_usd_per_minute")];
    const markup = typeof body.managed_markup === "string" ? body.managed_markup : null;
    if (team === null || included === null || seat === null || trial === null || markup === null) return null;
    return { teamPriceUsd: team, includedMembers: included, seatPriceUsd: seat, trialDays: trial, frontDeskPriceUsd: frontDesk, managedMarkup: markup, voiceMonthlyUsd: voiceMonthly, voiceIncludedMinutes: voiceMinutes, voiceOverageUsd: voiceOverage };
  } catch {
    return null;
  }
}

/** What a team of `people` pays a month on the one plan. */
export function teamMonthlyUsd(p: Pricing, people: number): number {
  return p.teamPriceUsd + Math.max(0, people - p.includedMembers) * p.seatPriceUsd;
}

/** 6 -> "6th": the first paid member's ordinal. */
export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th"}`;
}

/** "plus 20%" for a markup of "1.2", "plus 2.5%" for "1.025": exact to 0.01%, never rounded to whole. */
export function markupPercent(markup: string): number {
  return Number(((Number(markup) - 1) * 100).toFixed(2));
}

/** The voice plan in one sentence, or null while the billing API has not sent its numbers (callers then show neutral copy). */
export function voicePlanLine(p: Pricing | null): string | null {
  if (!p || p.voiceMonthlyUsd === null || p.voiceIncludedMinutes === null || p.voiceOverageUsd === null) return null;
  return `$${p.voiceMonthlyUsd} a month includes ${p.voiceIncludedMinutes.toLocaleString("en-US")} call minutes, then $${p.voiceOverageUsd.toFixed(2)} a minute.`;
}

export const VOICE_PLAN_NEUTRAL = "One flat monthly price with call minutes included, then a per-minute rate.";

/** /pricing copy, shared by the page, /pricing.md and the FAQ JSON-LD. */
export function pricingFaq(p: Pricing): { q: string; a: string }[] {
  return [
    {
      q: "What does the voice receptionist cost?",
      a: `${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL} One plan for your salon, spa or barbershop; no per-seat or per-staff fees.`,
    },
    {
      q: "What counts as a call minute?",
      a: "The time a caller is on the line with your receptionist. Minutes are tallied over the month; usage past the included minutes is billed at the per-minute rate.",
    },
    { q: "What if it doesn't stick?", a: "Cancel any time." },
  ];
}

export const VOICE_PLAN_INCLUDES =
  "A receptionist that answers every call, books and reschedules appointments, answers questions about your services and hours, and texts confirmations.";
