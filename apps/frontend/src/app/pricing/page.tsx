import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { PricingCtas } from "@/components/pricing/PricingCtas";
import {
  VOICE_PLAN_INCLUDES,
  VOICE_PLAN_NEUTRAL,
  getPricing,
  pricingFaq,
  voicePlanLine,
  type Pricing,
} from "@/lib/pricing";
import { JsonLd, absoluteUrl, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

/** Prices come from the billing API (lib/pricing); re-read every 5 minutes. */
export const revalidate = 300;

export async function generateMetadata() {
  const p = await getPricing();
  return pageMetadata({
    title: "Pricing — Lightbulb",
    description: `A voice receptionist for salons, spas and barbershops. ${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}`,
    path: "/pricing",
  });
}

function jsonLd(p: Pricing) {
  const monthly = (price: number) => ({
    "@type": "UnitPriceSpecification",
    price,
    priceCurrency: "USD",
    unitText: "MONTH",
  });
  return [
    {
      "@context": "https://schema.org",
      "@type": "Product",
      name: "Lightbulb voice receptionist",
      url: absoluteUrl("/pricing"),
      offers:
        p.voiceMonthlyUsd === null
          ? undefined
          : {
              "@type": "Offer",
              name: "Voice receptionist, calls included",
              price: String(p.voiceMonthlyUsd),
              priceCurrency: "USD",
              priceSpecification: monthly(p.voiceMonthlyUsd),
            },
    },
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      mainEntity: pricingFaq(p).map(({ q, a }) => ({ "@type": "Question", name: q, acceptedAnswer: { "@type": "Answer", text: a } })),
    },
  ];
}

export default async function PricingPage() {
  const p = await getPricing();
  const network = {
    name: "Network",
    price: "$0",
    note: "free, open source",
    includes:
      "Your coding agents message each other and the agents of people you invite. Peer to peer, end-to-end encrypted, no account.",
  };
  const plans = [
    network,
    { name: "Voice receptionist", price: voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL, note: "", includes: VOICE_PLAN_INCLUDES },
    { name: "Enterprise", price: "Talk to us", note: "", includes: "Custom pricing for special cases" },
  ];
  return (
    <main className="ld-page min-h-screen">
      {p && jsonLd(p).map((data, i) => <JsonLd key={i} data={data} />)}
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: "Pricing", path: "/pricing" },
        ])}
      />
      <Navbar />
      <section className="mx-auto max-w-5xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-[0.98] text-[var(--midnight)] md:text-7xl">
          One plan for your front desk.
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">
          Your receptionist answers every call and books the appointment. No per-seat or per-staff fees.
        </p>

        <div className="mt-10 grid gap-4 md:grid-cols-3">
          {plans.map((plan) => (
            <div key={plan.name} className="editorial-card p-5 text-sm leading-6 text-[var(--midnight-soft)]">
              <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">{plan.name}</p>
              <p className="mt-2 font-editorial-serif text-2xl text-[var(--midnight)]">{plan.price}</p>
              <p className="mt-3">{plan.includes}</p>
            </div>
          ))}
        </div>

        <PricingCtas />

        {p && (
          <>
            <h2 className="mt-20 font-editorial-serif text-3xl text-[var(--midnight)]">Questions</h2>
            <dl className="mt-6 space-y-6">
              {pricingFaq(p).map(({ q, a }) => (
                <div key={q}>
                  <dt className="font-medium text-[var(--midnight)]">{q}</dt>
                  <dd className="mt-1 text-[var(--midnight-soft)]">{a}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </section>
      <Footer />
    </main>
  );
}
