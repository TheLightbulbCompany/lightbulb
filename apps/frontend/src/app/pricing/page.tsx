import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import {
  VOICE_PLAN_INCLUDES,
  VOICE_PLAN_NEUTRAL,
  getPricing,
  perMinute,
  pricingFaq,
  voicePlanLine,
  type Pricing,
} from "@/lib/pricing";
import { JsonLd, absoluteUrl, breadcrumbJsonLd, faqJsonLd, pageMetadata } from "@/lib/seo";

import { PricingCta } from "./PricingCta";

/** Prices come from the billing API (lib/pricing); re-read every 5 minutes. */
export const revalidate = 300;

export async function generateMetadata() {
  const p = await getPricing();
  return pageMetadata({
    title: "Lightbulb Phone Pricing for Salons & Spas | Lightbulb",
    description: `One monthly plan for your AI receptionist. ${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL} Month to month, no setup fee.`,
    path: "/pricing",
  });
}

// A Product with no offers is a Rich Results error, so it is emitted only with a price.
function jsonLd(p: Pricing | null) {
  const monthly = p?.voiceMonthlyUsd ?? null;
  const faq = faqJsonLd(pricingFaq(p));
  if (monthly === null) return [faq];
  return [
    {
      "@context": "https://schema.org",
      "@type": "Product",
      name: "Lightbulb AI receptionist",
      url: absoluteUrl("/pricing"),
      offers: {
        "@type": "Offer",
        name: "Monthly plan, minutes included",
        price: String(monthly),
        priceCurrency: "USD",
        priceSpecification: { "@type": "UnitPriceSpecification", price: monthly, priceCurrency: "USD", unitText: "MONTH" },
      },
    },
    faq,
  ];
}

export default async function PricingPage() {
  const p = await getPricing();
  return (
    <main className="ld-page min-h-screen">
      {jsonLd(p).map((data, i) => (
        <JsonLd key={i} data={data} />
      ))}
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: "Pricing", path: "/pricing" },
        ])}
      />
      <Navbar />
      <section className="mx-auto max-w-5xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-[0.98] text-[var(--midnight)] md:text-7xl">
          One plan. Your phone answered and booked.
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">
          {voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL}
        </p>

        <div className="editorial-card mt-10 max-w-3xl p-6 text-base leading-7 text-[var(--midnight-soft)]">
          <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">What&apos;s included</p>
          <ul className="mt-4 list-disc space-y-1 pl-5">
            {VOICE_PLAN_INCLUDES.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p className="mt-6">
            <strong className="text-[var(--midnight)]">When the minutes run out, you decide.</strong> Keep answering at{" "}
            {perMinute(p) ?? "the per-minute rate"}, or send calls to your phone. A meter in Settings shows what you&apos;ve used.
          </p>
          <p className="mt-4">
            <strong className="text-[var(--midnight)]">Month to month.</strong> No setup fee, no contract. Cancel yourself in
            Settings. There&apos;s no free trial: you set everything up first and pay when you go live.
            {" "}Not right for you? Tell us in your first 30 days and we refund the month.
          </p>
        </div>

        <PricingCta />

        <h2 className="mt-20 font-editorial-serif text-3xl text-[var(--midnight)]">Questions</h2>
        <dl className="mt-6 max-w-3xl space-y-6">
          {pricingFaq(p).map(({ q, a }) => (
            <div key={q}>
              <dt className="font-medium text-[var(--midnight)]">{q}</dt>
              <dd className="mt-1 text-[var(--midnight-soft)]">{a}</dd>
            </div>
          ))}
        </dl>
      </section>
      <Footer />
    </main>
  );
}
