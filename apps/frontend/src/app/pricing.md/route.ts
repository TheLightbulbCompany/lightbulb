import { SITE_URL } from "@/lib/domain";
import {
  PRICING_FAQ,
  PRICING_FOUNDING,
  PRICING_INFERENCE,
  PRICING_TRIAL,
  SEAT_PRICE_USD,
  TEAM_INCLUDES,
  TEAM_PRICE_USD,
  TRIAL_DAYS,
} from "@/lib/pricing";

/** `/pricing.md`: the /pricing page as Markdown, for answer engines. Same copy source as the page. */
export const dynamic = "force-static";

export function GET() {
  const body = [
    "# Lightbulb pricing",
    "",
    "One price for the workspace. The first five people are free.",
    "",
    "| Plan | Price | Includes |",
    "|---|---|---|",
    `| Team | $${TEAM_PRICE_USD} a month, flat | ${TEAM_INCLUDES} |`,
    `| Each person from the 6th | +$${SEAT_PRICE_USD} a month | The same workspace, one more person |`,
    "| Enterprise | Talk to us | Custom pricing for special cases |",
    "",
    PRICING_INFERENCE,
    "",
    `**${TRIAL_DAYS} days free.** ${PRICING_TRIAL}`,
    "",
    `**Founding teams: ten spots.** ${PRICING_FOUNDING} Request a guided setup: ${SITE_URL}/request-setup`,
    "",
    "## Questions",
    "",
    ...PRICING_FAQ.flatMap(({ q, a }) => [`**${q}** ${a}`, ""]),
    `Start free: ${SITE_URL}/sign-up`,
    "",
  ].join("\n");
  return new Response(body, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
