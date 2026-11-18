/**
 * Public prices, read from the backend that bills them (`GET /billing/prices`),
 * so the site never carries a second copy of a price. The voice* fields are the
 * receptionist plan the site sells; the team fields belong to the retired plan.
 * Server side only, cached 5 minutes. `null` when the API cannot be read: callers
 * show neutral copy rather than present a number as current terms.
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
  /** The voice receptionist plan: a month, the minutes it includes, the rate past them. null while it can't be bought. */
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
    const markup = typeof body.managed_markup === "string" ? body.managed_markup : null;
    if (team === null || included === null || seat === null || trial === null || markup === null) return null;
    return {
      teamPriceUsd: team,
      includedMembers: included,
      seatPriceUsd: seat,
      trialDays: trial,
      frontDeskPriceUsd: frontDesk,
      managedMarkup: markup,
      voiceMonthlyUsd: n("voice_plan_monthly_usd"),
      voiceIncludedMinutes: n("voice_included_minutes"),
      voiceOverageUsd: n("voice_overage_usd_per_minute"),
    };
  } catch {
    return null;
  }
}

/** "$0.35 a minute", or null until the billing API sends the voice plan. */
export function perMinute(p: Pricing | null): string | null {
  return p?.voiceOverageUsd == null ? null : `$${p.voiceOverageUsd.toFixed(2)} a minute`;
}

/** "$250 a month includes 1,500 minutes, then $0.35 a minute.", or null (callers show VOICE_PLAN_NEUTRAL). */
export function voicePlanLine(p: Pricing | null): string | null {
  const rate = perMinute(p);
  if (!p || p.voiceMonthlyUsd === null || p.voiceIncludedMinutes === null || !rate) return null;
  return `$${p.voiceMonthlyUsd} a month includes ${p.voiceIncludedMinutes.toLocaleString("en-US")} minutes, then ${rate}.`;
}

export const VOICE_PLAN_NEUTRAL = "One monthly price with minutes included, then a per-minute rate.";

/** What the plan includes: VERIFIED claims only (.agents/product-marketing.md). */
export const VOICE_PLAN_INCLUDES = [
  "Answers your phone day and night, several calls at once",
  "Books appointments in its own calendar or your Google Calendar, inside your hours",
  "Moves and cancels bookings for your clients",
  "Answers questions about your hours, services and prices",
  "Takes messages, and hands callers to your mobile when they want a person",
  "Shows you every call: a summary, what it did and the transcript",
  "Speaks English, or English and Spanish",
  "Keep your number with call forwarding, or get a new local number",
  "Joins your Google Meet when you invite it",
] as const;

/** /pricing FAQ: the price objections, shared by the page, /pricing.md and the FAQ JSON-LD. Neutral when `p` is null. */
export function pricingFaq(p: Pricing | null): { q: string; a: string }[] {
  return [
    {
      q: "What does it cost?",
      a: `${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL} One plan per business, paid monthly by card. No setup fee and no contract.`,
    },
    {
      q: "What counts as a minute?",
      a:
        "The time your assistant spends on calls, plus time in Google Meet meetings you invite it to. " +
        "Settings shows how many minutes you've used so far.",
    },
    {
      q: "What happens if I use up my minutes?",
      a: `You choose in Settings: keep answering at ${perMinute(p) ?? "the per-minute rate"}, or send calls to your phone instead.`,
    },
    {
      q: "Is there a free trial?",
      a:
        "No. You can set everything up first: your business info, how bookings work, your phone and your " +
        "assistant's name. You pay when you go live, and that's when its line turns on.",
    },
    {
      q: "Can I cancel?",
      a:
        "Yes, yourself, in Settings. Your assistant keeps working until the end of the period you paid for. " +
        "To start again later, contact support.",
    },
    {
      q: "Why pay more than a cheaper AI receptionist?",
      a:
        "It books the appointment during the call, shows you what it said and did on every call, and asks you " +
        "when it isn't sure instead of guessing. And the bill has no surprises.",
    },
    {
      q: "How does it compare with hiring a receptionist?",
      a:
        "A receptionist's median pay in the US is $38,010 a year (US Bureau of Labor Statistics, May 2025), " +
        "and they go home at night. Your assistant answers at 2 am too.",
    },
  ];
}
