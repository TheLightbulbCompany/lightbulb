import Link from "next/link";

import { VOICE_PLAN_NEUTRAL, voicePlanLine, type Pricing } from "@/lib/pricing";

import { Arrow } from "./Arrow";

/**
 * Homepage price block: the one voice-receptionist plan. Numbers come from the
 * billing API via the page (lib/pricing.getPricing); null shows neutral copy.
 */
export function PricingThreeCard({ pricing: p }: { pricing: Pricing | null }) {
  return (
    <section id="pricing" className="lb-pricing">
      <div className="lb-wrap">
        <h2 className="lb-h2">One plan for your front desk. One price.</h2>
        <p className="lb-price-hero">
          <b>{voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}</b>
        </p>
        <p className="lb-price-free">One plan for your salon, spa or barbershop. No per-seat or per-staff fees.</p>
        <p className="lb-price-free">
          <Link href="/pricing">
            See pricing <Arrow />
          </Link>
        </p>
      </div>
    </section>
  );
}

export default PricingThreeCard;
