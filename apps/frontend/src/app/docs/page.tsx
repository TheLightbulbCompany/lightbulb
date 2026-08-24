import type { Metadata } from "next";
import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { docsFaq, docsSections } from "@/content/docs";
import { JsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Isol8 Docs — AI Agents, Automations, Connectors, Paperclip, and Company Brain",
  description:
    "Learn how Isol8 works: Paperclip goals and tasks, marketplace agents, connectors, browser automation, webhooks, inbox, company brain, models, pricing, and human approval gates.",
  path: "/docs",
});

export default function DocsPage() {
  const faqJsonLd = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: docsFaq.map((item) => ({
      "@type": "Question",
      name: item.question,
      acceptedAnswer: { "@type": "Answer", text: item.answer },
    })),
  };

  const collectionJsonLd = {
    "@context": "https://schema.org",
    "@type": "CollectionPage",
    name: "Isol8 product documentation",
    description:
      "Public documentation for Isol8's AI company workspace, agents, automations, connectors, docs, model providers, and approval gates.",
    url: "https://isol8.co/docs",
    hasPart: docsSections.map((section) => ({
      "@type": "WebPageElement",
      name: section.title,
      description: section.description,
      url: `https://isol8.co/docs#${section.id}`,
      keywords: section.keywords.join(", "),
    })),
  };

  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={collectionJsonLd} />
      <JsonLd data={faqJsonLd} />
      <Navbar />

      <section className="blog-wrap docs-wrap">
        <header className="blog-header docs-hero">
          <p className="blog-eyebrow">product_docs</p>
          <h1 className="blog-title">How Isol8 works</h1>
          <p className="blog-lede">
            A practical guide to running an AI-native company with agents, Paperclip,
            marketplace hires, connectors, automations, webhooks, browser workflows,
            the company brain, model providers, channels, pricing, and human approval gates.
          </p>
          <div className="docs-hero-actions">
            <Link href="/chat" className="ld-btn">
              Start your pod
              <span className="ld-btn-arrow" aria-hidden="true">→</span>
            </Link>
            <Link href="/blog" className="ld-btn ld-btn-secondary">
              Read SEO guides
            </Link>
          </div>
        </header>

        <nav className="docs-toc" aria-label="Documentation sections">
          {docsSections.map((section) => (
            <a key={section.id} href={`#${section.id}`}>
              {section.title}
            </a>
          ))}
        </nav>

        <div className="docs-grid">
          {docsSections.map((section) => (
            <article key={section.id} id={section.id} className="docs-card">
              <p className="blog-eyebrow">{section.keywords[0]}</p>
              <h2>{section.title}</h2>
              <p>{section.description}</p>
              <h3>What it does</h3>
              <ul>
                {section.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}
              </ul>
              <h3>How to use it</h3>
              <ol>
                {section.howTo.map((step) => <li key={step}>{step}</li>)}
              </ol>
            </article>
          ))}
        </div>

        <section className="docs-faq" aria-labelledby="docs-faq-title">
          <p className="blog-eyebrow">faq</p>
          <h2 id="docs-faq-title">Questions answer engines should understand</h2>
          <div className="docs-faq-list">
            {docsFaq.map((item) => (
              <details key={item.question}>
                <summary>{item.question}</summary>
                <p>{item.answer}</p>
              </details>
            ))}
          </div>
        </section>
      </section>

      <Footer />
    </main>
  );
}
