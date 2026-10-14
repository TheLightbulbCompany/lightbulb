import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { COMPARE_PAGES } from "@/content/compare";
import { JsonLd, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Compare Lightbulb | Lightbulb",
  description: "Lightbulb next to Slack, Microsoft Teams with Copilot, Grok Bot, Bloome and the tools for sharing Claude Code sessions, with every cell sourced and dated.",
  path: "/compare",
});

export default function CompareHub() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={breadcrumbJsonLd([{ name: "Home", path: "/" }, { name: "Compare", path: "/compare" }])} />
      <Navbar />
      <section className="mx-auto max-w-3xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--midnight)]">Compare Lightbulb</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--midnight-soft)]">
          Honest comparisons with the tools teams ask us about. Each says what the other product does better.
        </p>
        <ul className="mt-10 space-y-6">
          {COMPARE_PAGES.map((p) => (
            <li key={p.slug}>
              <Link href={p.path} className="font-editorial-serif text-2xl text-[var(--midnight)] hover:underline">
                {p.h1}
              </Link>
              <p className="mt-1 text-[var(--midnight-soft)]">{p.description}</p>
            </li>
          ))}
        </ul>
      </section>
      <Footer />
    </main>
  );
}
