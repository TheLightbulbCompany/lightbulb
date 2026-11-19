import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { COMPARE_PAGES } from "@/content/compare";
import { JsonLd, SITE_URL, absoluteUrl, breadcrumbJsonLd, faqJsonLd, pageMetadata } from "@/lib/seo";

import { VERTICALS, type Vertical } from "./verticals";

export function verticalMetadata(v: Vertical) {
  return pageMetadata({ title: v.title, description: v.description, path: v.path });
}

/** Shared by all three trades: the five setup steps and the honest limits. */
const STEPS = [
  "Give it your website. It drafts your hours, services and prices.",
  "Check the draft and fix anything it got wrong.",
  "Choose where bookings go: its own calendar or your Google Calendar.",
  "Keep your number with call forwarding, or get a new local number.",
  "Name your assistant. Choose the plan and go live.",
];

const LIMITS = [
  "It books into its own calendar or Google Calendar, not into Square, Vagaro, Fresha, Boulevard, GlossGenius, Booksy, Mindbody or Acuity.",
  "It doesn't send text messages, reminders or confirmations, and it doesn't make outbound calls.",
  "It doesn't take payments.",
  "It's for businesses in the US, with US local numbers.",
];

const H2 = "font-editorial-serif text-3xl text-[var(--midnight)]";
const BODY = "text-lg leading-8 text-[var(--midnight-soft)]";

export function VerticalPage({ v }: { v: Vertical }) {
  const others = VERTICALS.filter((o) => o.slug !== v.slug);
  return (
    <main className="ld-page min-h-screen">
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "WebPage",
          "@id": `${absoluteUrl(v.path)}#webpage`,
          url: absoluteUrl(v.path),
          name: v.title,
          description: v.description,
          inLanguage: "en",
          isPartOf: { "@id": `${SITE_URL}/#website` },
          about: { "@id": `${SITE_URL}/#software` },
        }}
      />
      <JsonLd data={breadcrumbJsonLd([{ name: "Home", path: "/" }, { name: v.h1, path: v.path }])} />
      <JsonLd data={faqJsonLd(v.faq)} />
      <Navbar />
      <article className="mx-auto max-w-3xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-4xl leading-tight text-[var(--midnight)] md:text-6xl">{v.h1}</h1>
        <p className={`mt-6 ${BODY}`}>{v.intro}</p>
        <div className="mt-8 flex flex-wrap gap-4">
          <Link href={`/sign-up?source=vertical-${v.slug}`} className="lb-btn lb-btn-p lb-btn-xl">
            Set up your assistant
          </Link>
        </div>

        <section className="mt-16">
          <h2 className={H2}>The calls it answers</h2>
          <dl className="mt-6 space-y-6">
            {v.calls.map((c) => (
              <div key={c.ask}>
                <dt className="font-semibold text-[var(--midnight)]">{c.ask}</dt>
                <dd className={`mt-1 ${BODY}`}>{c.does}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="mt-16">
          <h2 className={H2}>When your phone rings</h2>
          <p className={`mt-4 ${BODY}`}>{v.busy}</p>
          <p className={`mt-4 ${BODY}`}>
            Your assistant answers day and night and handles several calls at once. When a caller wants a person, it can
            transfer the call to your mobile. Anything it can&rsquo;t decide comes to you as a Needs you card, and every
            call shows a one-line summary, the steps it took and the transcript.
          </p>
        </section>

        <section className="mt-16">
          <h2 className={H2}>Your services, your words</h2>
          <p className={`mt-4 ${BODY}`}>{v.servicesIntro}</p>
          <ul className={`mt-4 list-disc pl-6 ${BODY}`}>
            {v.services.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </section>

        <section className="mt-16">
          <h2 className={H2}>Set up in five steps</h2>
          <ol className={`mt-4 list-decimal pl-6 ${BODY}`}>
            {STEPS.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
        </section>

        <section className="mt-16">
          <h2 className={H2}>What it doesn&rsquo;t do</h2>
          <ul className={`mt-4 list-disc pl-6 ${BODY}`}>
            {LIMITS.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </section>

        <section className="mt-16">
          <h2 className={H2}>Questions</h2>
          <dl className="mt-4 space-y-6">
            {v.faq.map(({ q, a }) => (
              <div key={q}>
                <dt className="font-semibold text-[var(--midnight)]">{q}</dt>
                <dd className="mt-2 leading-7 text-[var(--midnight-soft)]">{a}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="mt-16">
          <h2 className={H2}>Compare and keep reading</h2>
          <ul className={`mt-4 space-y-2 ${BODY}`}>
            {others.map((o) => (
              <li key={o.slug}>
                <Link href={o.path} className="underline">
                  Calls answered and booked for {o.linkLabel}
                </Link>
              </li>
            ))}
            {COMPARE_PAGES.map((p) => (
              <li key={p.slug}>
                <Link href={p.path} className="underline">
                  {p.h1}
                </Link>
              </li>
            ))}
          </ul>
        </section>

        <div className="mt-14 flex flex-wrap gap-4">
          <Link href={`/sign-up?source=vertical-${v.slug}`} className="lb-btn lb-btn-p lb-btn-xl">
            Set up your assistant
          </Link>
        </div>
      </article>
      <Footer />
    </main>
  );
}
