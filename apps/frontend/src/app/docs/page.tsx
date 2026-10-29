import type { Metadata } from "next";
import Link from "next/link";

import { docsFaq, getAllDocs } from "@/content/docs";
import { JsonLd, absoluteUrl, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb Docs — Get started, use cases, automations, connectors, and approvals",
  description:
    "How Lightbulb works: sign up and brief your CEO, the loops teams run, skills and automations, connectors and the browser, and the approvals that keep humans in control.",
  path: "/docs",
});

export default function DocsPage() {
  const docs = getAllDocs();

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
    name: "Lightbulb product documentation",
    description: metadata.description,
    url: absoluteUrl("/docs"),
    hasPart: docs.map((doc) => ({
      "@type": "WebPage",
      name: doc.title,
      description: doc.description,
      url: absoluteUrl(`/docs/${doc.slug}`),
    })),
  };

  return (
    <>
      <JsonLd data={collectionJsonLd} />
      <JsonLd data={faqJsonLd} />

      <header className="blog-header docs-hero">
        <p className="blog-eyebrow">docs</p>
        <h1 className="blog-title">How Lightbulb works</h1>
        <p className="blog-lede">
          A private AI company: your own workspace, persistent memory, and a six-role starter
          team led by your CEO. Short pages cover everything from sign-up to the approval
          gates that keep you in control, and the agent network that connects your coding
          agents to your teammates&apos;.
        </p>
        <div className="docs-hero-actions">
          <Link href="/chat" className="ld-btn">
            Open your workspace
            <span className="ld-btn-arrow" aria-hidden="true">→</span>
          </Link>
          <Link href="/blog" className="ld-btn ld-btn-secondary">
            Read the blog
          </Link>
        </div>
      </header>

      <ul className="blog-list">
        {docs.map((doc) => (
          <li key={doc.slug} className="blog-card">
            <Link href={`/docs/${doc.slug}`} className="blog-card-link">
              <h2 className="blog-card-title">{doc.title}</h2>
              <p className="blog-card-desc">{doc.description}</p>
              <span className="blog-card-cta" aria-hidden="true">
                Read
                <span className="blog-card-arrow">→</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>

      <section className="docs-faq" aria-labelledby="docs-faq-title">
        <p className="blog-eyebrow">faq</p>
        <h2 id="docs-faq-title">Common questions</h2>
        <div className="docs-faq-list">
          {docsFaq.map((item) => (
            <details key={item.question}>
              <summary>{item.question}</summary>
              <p>{item.answer}</p>
            </details>
          ))}
        </div>
      </section>
    </>
  );
}
