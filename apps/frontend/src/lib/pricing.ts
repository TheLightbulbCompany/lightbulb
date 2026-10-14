/**
 * The one plan's public price (T06/T30): every price line on the marketing site
 * reads these. In-app surfaces read the same numbers from /billing/account
 * (workspace_price_usd, seat_price_usd, included_members), which is what Stripe
 * actually bills; keep these in lockstep with the backend settings
 * WORKSPACE_PRICE_USD / SEAT_PRICE_USD and billing_service.SEATS_INCLUDED.
 */
export const TEAM_PRICE_USD = 50;
export const SEAT_PRICE_USD = 20;
export const INCLUDED_MEMBERS = 5;
export const TRIAL_DAYS = 30;
/** Managed AI is billed at the model provider's price times this. */
export const MANAGED_MARKUP = 1.2;
export const PRICE_LOCK_YEARS = 2;

/** What a team of `people` pays a month on the one plan. */
export function teamMonthlyUsd(people: number): number {
  return TEAM_PRICE_USD + Math.max(0, people - INCLUDED_MEMBERS) * SEAT_PRICE_USD;
}

/** /pricing copy (10 §B), shared by the page, /pricing.md and the FAQ JSON-LD. */
export const PRICING_FAQ: { q: string; a: string }[] = [
  {
    q: `Why $${TEAM_PRICE_USD} flat?`,
    a:
      "What your team gets grows with the work its agents do, not with headcount. So you pay for the workspace " +
      `and the agents' work, and people up to five cost nothing. An eight-person team pays $${teamMonthlyUsd(8)} a month.`,
  },
  {
    q: "What happens at the 6th person?",
    a:
      `Your bill goes up by $${SEAT_PRICE_USD} a month for each person from the sixth, prorated from the day they join. ` +
      "When someone leaves, it goes back down.",
  },
  {
    q: "What does managed AI cost?",
    a:
      "The model provider's price plus 20%, drawn from prepaid credits you top up. Usage shows in the workspace, " +
      "and nothing runs past your balance.",
  },
  {
    q: "Can we bring our own key?",
    a:
      "Yes. Sign in with your ChatGPT account, or add an OpenAI or Anthropic key, after your plan is active. " +
      "Your provider bills you directly and we charge nothing for inference.",
  },
  { q: "What if it doesn't stick?", a: "Cancel any time. We help you take your work with you." },
];

export const PRICING_INFERENCE =
  "Your agents' AI is billed separately, your choice: Lightbulb-managed at the model provider's price × 1.2, " +
  "as prepaid credits, or bring your own: your ChatGPT account or an OpenAI or Anthropic key, billed by them.";
export const PRICING_TRIAL =
  "No card to start; add one when the month ends. Founding teams put a card on file at their setup session, " +
  "and the first charge is on day 31. Your agents run on Lightbulb-managed AI with a daily allowance meanwhile.";
export const PRICING_FOUNDING =
  "A founder sets your team up with you, and your price is locked for two years.";
export const TEAM_INCLUDES =
  "Up to 5 people · six AI employees (CEO, Engineering, Finance, Growth, Analytics, Customer Operations) · " +
  "channels, DMs, task board, Library, shared browser, team email · Mac app; iPhone app on TestFlight";
