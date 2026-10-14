import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
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
import { JsonLd, absoluteUrl, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Pricing — Lightbulb",
  description:
    `$${TEAM_PRICE_USD} a month for your workspace, up to five people, then $${SEAT_PRICE_USD} a person. ` +
    `Managed AI at provider price × 1.2, or bring your own. ${TRIAL_DAYS} days free.`,
  path: "/pricing",
});

const faqJsonLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: PRICING_FAQ.map(({ q, a }) => ({
    "@type": "Question",
    name: q,
    acceptedAnswer: { "@type": "Answer", text: a },
  })),
};

const offerJsonLd = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: "Lightbulb Team",
  url: absoluteUrl("/pricing"),
  offers: [
    {
      "@type": "Offer",
      name: "Team, up to 5 people",
      price: String(TEAM_PRICE_USD),
      priceCurrency: "USD",
      priceSpecification: { "@type": "UnitPriceSpecification", price: TEAM_PRICE_USD, priceCurrency: "USD", unitText: "MONTH" },
    },
    {
      "@type": "Offer",
      name: "Each person from the 6th",
      price: String(SEAT_PRICE_USD),
      priceCurrency: "USD",
      priceSpecification: { "@type": "UnitPriceSpecification", price: SEAT_PRICE_USD, priceCurrency: "USD", unitText: "MONTH" },
    },
  ],
};

const PLANS = [
  { name: "Team", price: `$${TEAM_PRICE_USD} a month`, note: "flat", includes: TEAM_INCLUDES },
  { name: "Each person from the 6th", price: `+$${SEAT_PRICE_USD} a month`, note: "", includes: "The same workspace, one more person" },
  { name: "Enterprise", price: "Talk to us", note: "", includes: "Custom pricing for special cases" },
];

export default function PricingPage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={offerJsonLd} />
      <JsonLd data={faqJsonLd} />
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: "Pricing", path: "/pricing" },
        ])}
      />
      <Navbar />
      <section className="mx-auto max-w-5xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-[0.98] text-[var(--midnight)] md:text-7xl">
          One price for the workspace. The first five people are free.
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">
          Your team pays for the workspace and the work its agents do, not for each seat.
        </p>

        <div className="mt-10 grid gap-4 md:grid-cols-3">
          {PLANS.map((p) => (
            <div key={p.name} className="editorial-card p-5 text-sm leading-6 text-[var(--midnight-soft)]">
              <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">{p.name}</p>
              <p className="mt-2 font-editorial-serif text-3xl text-[var(--midnight)]">
                {p.price}
                {p.note && <span className="text-base text-[var(--midnight-muted)]">, {p.note}</span>}
              </p>
              <p className="mt-3">{p.includes}</p>
            </div>
          ))}
        </div>

        <div className="mt-10 space-y-4 text-base leading-7 text-[var(--midnight-soft)]">
          <p>{PRICING_INFERENCE}</p>
          <p>
            <strong className="text-[var(--midnight)]">{TRIAL_DAYS} days free.</strong> {PRICING_TRIAL}
          </p>
          <p>
            <strong className="text-[var(--midnight)]">Founding teams: ten spots.</strong> {PRICING_FOUNDING}{" "}
            <Link href="/request-setup" className="underline">
              Request a guided setup
            </Link>
          </p>
        </div>

        <div className="mt-10 flex flex-wrap gap-4">
          <Link href="/request-setup" className="editorial-btn editorial-btn--primary h-11 px-6">
            Request a guided setup
          </Link>
          <Link href="/sign-up" className="editorial-btn h-11 px-6">
            Start free
          </Link>
        </div>

        <h2 className="mt-20 font-editorial-serif text-3xl text-[var(--midnight)]">Questions</h2>
        <dl className="mt-6 space-y-6">
          {PRICING_FAQ.map(({ q, a }) => (
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
