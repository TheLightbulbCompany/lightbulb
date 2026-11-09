import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { PricingCtas } from "@/components/pricing/PricingCtas";
import {
  PRICING_FOUNDING,
  PRICING_TRIAL,
  getPricing,
  pricingInference,
  ordinal,
  pricingFaq,
  teamIncludes,
  type Pricing,
} from "@/lib/pricing";
import { JsonLd, absoluteUrl, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

/** Prices come from the billing API (lib/pricing); re-read every 5 minutes. */
export const revalidate = 300;

export async function generateMetadata() {
  const p = await getPricing();
  return pageMetadata({
    title: "Pricing — Lightbulb",
    description: p
      ? `$${p.teamPriceUsd} a month for your workspace, up to ${p.includedMembers} people, then $${p.seatPriceUsd} a person. ` +
        `Lightbulb-managed AI at provider price × ${p.managedMarkup}, or sign in with ChatGPT. ${p.trialDays} days free.`
      : "One price for the workspace and your first people, then a per-person price. Choose Lightbulb-managed AI or sign in with ChatGPT.",
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
      name: "Lightbulb Team",
      url: absoluteUrl("/pricing"),
      offers: [
        {
          "@type": "Offer",
          name: `Team, up to ${p.includedMembers} people`,
          price: String(p.teamPriceUsd),
          priceCurrency: "USD",
          priceSpecification: monthly(p.teamPriceUsd),
        },
        {
          "@type": "Offer",
          name: `Each person from the ${ordinal(p.includedMembers + 1)}`,
          price: String(p.seatPriceUsd),
          priceCurrency: "USD",
          priceSpecification: monthly(p.seatPriceUsd),
        },
      ],
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
  const plans = p
    ? [
        network,
        { name: "Team", price: `$${p.teamPriceUsd} a month`, note: "flat", includes: teamIncludes(p) },
        {
          name: `Each person from the ${ordinal(p.includedMembers + 1)}`,
          price: `+$${p.seatPriceUsd} a month`,
          note: "",
          includes: "The same workspace, one more person",
        },
        { name: "Enterprise", price: "Talk to us", note: "", includes: "Custom pricing for special cases" },
      ]
    : [network];
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
          One price for the workspace. The first {p ? p.includedMembers : "few"} people are free.
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--midnight-soft)]">
          Your team pays for the workspace and the work its agents do, not for each seat.
        </p>

        {p ? (
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            {plans.map((plan) => (
              <div key={plan.name} className="editorial-card p-5 text-sm leading-6 text-[var(--midnight-soft)]">
                <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">{plan.name}</p>
                <p className="mt-2 font-editorial-serif text-3xl text-[var(--midnight)]">
                  {plan.price}
                  {plan.note && <span className="text-base text-[var(--midnight-muted)]">, {plan.note}</span>}
                </p>
                <p className="mt-3">{plan.includes}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-10 text-[var(--midnight-soft)]">Prices are loading. Please refresh in a moment.</p>
        )}

        <div className="mt-10 space-y-4 text-base leading-7 text-[var(--midnight-soft)]">
          <p>{pricingInference(p)}</p>
          <p>
            <strong className="text-[var(--midnight)]">{p ? `${p.trialDays} days free.` : "A free month."}</strong>{" "}
            {PRICING_TRIAL}
          </p>
          <p>
            <strong className="text-[var(--midnight)]">Founding teams: ten spots.</strong> {PRICING_FOUNDING}{" "}
            <Link href="/request-setup" className="underline">
              Request a guided setup
            </Link>
          </p>
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
