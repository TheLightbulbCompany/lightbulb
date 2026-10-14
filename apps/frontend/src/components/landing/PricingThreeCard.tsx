import Link from "next/link";

import { INCLUDED_MEMBERS, SEAT_PRICE_USD, TEAM_PRICE_USD, TRIAL_DAYS } from "@/lib/pricing";

import { Arrow } from "./Arrow";

/**
 * Homepage price block (10 §A.5): one price, then the three ways to power the
 * agents as a plain row (no pill, no per-card buttons, no art). Every number
 * comes from lib/pricing.
 */
const OPTIONS = [
  {
    title: "Lightbulb-managed.",
    body: "Nothing to set up. Billed as credits at the model provider's price plus 20%.",
  },
  { title: "Your ChatGPT account.", body: "Sign in once; your agents run on your ChatGPT plan." },
  { title: "Your own key.", body: "OpenAI or Anthropic. Your provider bills you directly." },
];

export function PricingThreeCard() {
  return (
    <section id="pricing" className="lb-pricing">
      <div className="lb-wrap">
        <h2 className="lb-h2">Any model. One price.</h2>
        <p className="lb-price-hero">
          <b>
            ${TEAM_PRICE_USD} a month for your workspace, up to {numberWord(INCLUDED_MEMBERS)} people.
          </b>
        </p>
        <p className="lb-price-free">
          Then ${SEAT_PRICE_USD} a month for each person from the sixth. {TRIAL_DAYS} days free, no card to start;
          founding teams put a card on file at their setup session and the first charge is on day 31.
        </p>
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
