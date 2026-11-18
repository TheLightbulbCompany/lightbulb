import Link from "next/link";

import { VOICE_PLAN_NEUTRAL, voicePlanLine, type Pricing } from "@/lib/pricing";

import { Arrow } from "./Arrow";

/**
 * Homepage price block: the one receptionist plan. Numbers come from the
 * billing API via the page (lib/pricing.getPricing); null shows neutral copy.
 */
export function PricingThreeCard({ pricing: p }: { pricing: Pricing | null }) {
  return (
    <section id="pricing" className="lb-pricing">
      <div className="lb-wrap">
        <h2 className="lb-h2">One plan. No surprises on the bill.</h2>
        <p className="lb-price-hero">
          <b>{voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}</b>
        </p>
        <p className="lb-price-free">
          Month to month, no setup fee. A meter shows your minutes, and you choose what happens when they run out. Cancel
          yourself in Settings.
        </p>
        <p className="lb-price-free">
          <Link href="/pricing">
            See what&apos;s included <Arrow />
          </Link>
        </p>
      </div>
    </section>
  );
}

export default PricingThreeCard;
