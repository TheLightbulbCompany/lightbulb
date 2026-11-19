import { SITE_URL } from "@/lib/domain";
import { VOICE_PLAN_INCLUDES, VOICE_PLAN_NEUTRAL, getPricing, perMinute, pricingFaq, voicePlanLine } from "@/lib/pricing";

/** `/pricing.md`: the /pricing page as Markdown, for answer engines. Prices from the billing API. */
export const revalidate = 300;

export async function GET() {
  const p = await getPricing();
  if (!p) return new Response("Pricing is temporarily unavailable.\n", { status: 503 });
  const body = [
    "# Lightbulb pricing",
    "",
    "Lightbulb is an AI receptionist for salons, spas and barbershops in the US. One plan per business.",
    "",
    `**${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}**`,
    "",
    "## What's included",
    "",
    ...VOICE_PLAN_INCLUDES.map((line) => `- ${line}`),
    "",
    `When the minutes run out, you decide: keep answering at ${perMinute(p) ?? "the per-minute rate"}, or send calls to your phone. A meter in Settings shows what you've used.`,
    "",
    "Month to month. No setup fee, no contract. Cancel yourself in Settings. There's no free trial: you set everything up first and pay when you go live. Not right for you? Tell us in your first 30 days and we refund the month.",
    "",
    "## Questions",
    "",
    ...pricingFaq(p).flatMap(({ q, a }) => [`**${q}** ${a}`, ""]),
    `Set up your assistant: ${SITE_URL}/sign-up`,
    "",
  ].join("\n");
  return new Response(body, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=300, stale-while-revalidate=86400",
    },
  });
}
