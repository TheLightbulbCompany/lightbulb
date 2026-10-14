import Link from "next/link";

import { ordinal, type Pricing } from "@/lib/pricing";

import { Arrow } from "./Arrow";

/**
 * Homepage price block (10 §A.5): one price, then the three ways to power the
 * agents as a plain row (no pill, no per-card buttons, no art). Prices come from
 * the billing API via the page (lib/pricing.getPricing); null shows no number.
 */
const OPTIONS = [
  {
    title: "Lightbulb-managed.",
    body: "Nothing to set up. Billed as credits at the model provider's price plus 20%.",
  },
  { title: "Your ChatGPT account.", body: "Sign in once; your agents run on your ChatGPT plan." },
  { title: "Your own key.", body: "OpenAI or Anthropic. Your provider bills you directly." },
];

export function PricingThreeCard({ pricing: p }: { pricing: Pricing | null }) {
  return (
    <section id="pricing" className="lb-pricing">
      <div className="lb-wrap">
        <h2 className="lb-h2">Any model. One price.</h2>
        {p && (
          <>
            <p className="lb-price-hero">
              <b>
                ${p.teamPriceUsd} a month for your workspace, up to {numberWord(p.includedMembers)} people.
              </b>
            </p>
            <p className="lb-price-free">
              Then ${p.seatPriceUsd} a month for each person from the {ordinal(p.includedMembers + 1)}. {p.trialDays}{" "}
              days free, no card to start; founding teams put a card on file at their setup session and the first charge
              is on day 31.
            </p>
          </>
        )}
        <div className="lb-plans">
          {OPTIONS.map((o) => (
            <article key={o.title} className="lb-plan">
              <div className="lb-plan-b">
                <p className="lb-blurb">
                  <strong>{o.title}</strong> {o.body}
                </p>
              </div>
            </article>
          ))}
        </div>
        <p className="lb-price-free">
          <Link href="/pricing">
            See pricing for your team <Arrow />
          </Link>
        </p>
      </div>
    </section>
  );
}

function numberWord(n: number): string {
  return ["zero", "one", "two", "three", "four", "five", "six"][n] ?? String(n);
}

export default PricingThreeCard;
