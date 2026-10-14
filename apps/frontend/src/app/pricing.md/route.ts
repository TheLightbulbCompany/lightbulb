import { SITE_URL } from "@/lib/domain";
import {
  PRICING_FOUNDING,
  PRICING_TRIAL,
  getPricing,
  pricingInference,
  ordinal,
  pricingFaq,
  teamIncludes,
} from "@/lib/pricing";

/** `/pricing.md`: the /pricing page as Markdown, for answer engines. Prices from the billing API. */
export const revalidate = 300;

export async function GET() {
  const p = await getPricing();
  if (!p) return new Response("Pricing is temporarily unavailable.\n", { status: 503 });
  const body = [
    "# Lightbulb pricing",
    "",
    `One price for the workspace. The first ${p.includedMembers} people are free.`,
    "",
    "| Plan | Price | Includes |",
    "|---|---|---|",
    `| Team | $${p.teamPriceUsd} a month, flat | ${teamIncludes(p)} |`,
    `| Each person from the ${ordinal(p.includedMembers + 1)} | +$${p.seatPriceUsd} a month | The same workspace, one more person |`,
    "| Enterprise | Talk to us | Custom pricing for special cases |",
    "",
      "",
    `**${p.trialDays} days free.** ${PRICING_TRIAL}`,
    "",
    `**Founding teams: ten spots.** ${PRICING_FOUNDING} Request a guided setup: ${SITE_URL}/request-setup`,
    "",
    "## Questions",
    "",
    ...pricingFaq(p).flatMap(({ q, a }) => [`**${q}** ${a}`, ""]),
    `Start free: ${SITE_URL}/sign-up`,
    "",
  ].join("\n");
  return new Response(body, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=300, stale-while-revalidate=86400",
    },
  });
}
