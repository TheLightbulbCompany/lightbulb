import { SITE_URL } from "@/lib/domain";
import { VOICE_PLAN_INCLUDES, VOICE_PLAN_NEUTRAL, getPricing, pricingFaq, voicePlanLine } from "@/lib/pricing";

/** `/pricing.md`: the /pricing page as Markdown, for answer engines. Prices from the billing API. */
export const revalidate = 300;

export async function GET() {
  const p = await getPricing();
  if (!p) return new Response("Pricing is temporarily unavailable.\n", { status: 503 });
  const body = [
    "# Lightbulb pricing",
    "",
    `One plan for your salon, spa or barbershop. ${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}`,
    "",
    "| Plan | Price | Includes |",
    "|---|---|---|",
    `| Network | $0 | Your coding agents message each other and the agents of people you invite; peer to peer, end-to-end encrypted; open source (Apache-2.0); no account. ${SITE_URL}/network |`,
    `| Voice receptionist | ${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL} | ${VOICE_PLAN_INCLUDES} |`,
    "| Enterprise | Talk to us | Custom pricing for special cases |",
    "",
    "## Questions",
    "",
    ...pricingFaq(p).flatMap(({ q, a }) => [`**${q}** ${a}`, ""]),
    `Get started: ${SITE_URL}/sign-up`,
    "",
  ].join("\n");
  return new Response(body, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=300, stale-while-revalidate=86400",
    },
  });
}
