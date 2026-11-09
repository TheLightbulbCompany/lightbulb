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
    return { teamPriceUsd: team, includedMembers: included, seatPriceUsd: seat, trialDays: trial, frontDeskPriceUsd: frontDesk, managedMarkup: markup };
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

/** /pricing copy (10 §B), shared by the page, /pricing.md and the FAQ JSON-LD. */
export function pricingFaq(p: Pricing): { q: string; a: string }[] {
  const firstPaid = ordinal(p.includedMembers + 1);
  return [
    {
      q: `Why $${p.teamPriceUsd} flat?`,
      a:
        "What your team gets grows with the work its agents do, not with headcount. So you pay for the workspace " +
        `and the agents' work, and people up to ${p.includedMembers} cost nothing. ` +
        `An eight-person team pays $${teamMonthlyUsd(p, 8)} a month.`,
    },
    {
      q: `What happens at the ${firstPaid} person?`,
      a:
        `Your bill goes up by $${p.seatPriceUsd} a month for each person from the ${firstPaid}, prorated from the ` +
        "day they join. When someone leaves, it goes back down.",
    },
    {
      q: "What does managed AI cost?",
      a:
        `The model provider's price plus ${markupPercent(p.managedMarkup)}%, drawn from prepaid ` +
        "credits you top up. Usage shows in the workspace, and nothing runs past your balance.",
    },
    {
      q: "Which inference providers can we use?",
      a:
        "Choose Lightbulb-managed inference, funded with workspace credits, or sign in with your ChatGPT account. " +
        "ChatGPT bills your account directly; managed inference is charged against workspace credits.",
    },
    { q: "What if it doesn't stick?", a: "Cancel any time. We help you take your work with you." },
  ];
}

export function pricingInference(p: Pricing | null): string {
  const managed = p ? `at the model provider's price × ${p.managedMarkup}, ` : "";
  return (
    `Choose how your agents run AI: Lightbulb-managed inference ${managed}as prepaid workspace credits, ` +
    "or sign in with your ChatGPT account, billed by OpenAI."
  );
}
export const PRICING_TRIAL =
  "No card to start; add one when the month ends. Founding teams put a card on file at their setup session, " +
  "and the first charge is on day 31. Your agents run on Lightbulb-managed AI with a daily allowance meanwhile.";
export const PRICING_FOUNDING = "A founder sets your team up with you, and your price is locked for two years.";
export function teamIncludes(p: Pricing): string {
  return (
    `Up to ${p.includedMembers} people · six AI employees (CEO, Engineering, Finance, Growth, Analytics, Customer Operations) · ` +
    "channels, DMs, task board, Library, shared browser, team email · Mac app; iPhone app on TestFlight"
  );
}
