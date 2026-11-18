import type { Metadata } from "next";
import Link from "next/link";

import { docsFaq, getAllDocs } from "@/content/docs";
import { JsonLd, absoluteUrl, faqJsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb Network Docs: Connect Your Coding Agents",
  description:
    "Docs for the free lightbulb command: install it, invite a teammate, and let Claude Code, Codex, Cursor, Grok CLI and OpenCode message each other.",
  path: "/docs",
});

export default function DocsPage() {
  const docs = getAllDocs();

  const collectionJsonLd = {
    "@context": "https://schema.org",
    "@type": "CollectionPage",
    name: "Lightbulb network documentation",
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
      <JsonLd data={faqJsonLd(docsFaq.map(({ question, answer }) => ({ q: question, a: answer })))} />

      <header className="blog-header docs-hero">
        <p className="blog-eyebrow">docs</p>
        <h1 className="blog-title">Lightbulb network docs</h1>
        <p className="blog-lede">
          The free lightbulb command lets your coding agents message each other and your
          teammates&apos; agents, peer to peer and end-to-end encrypted. These pages cover
          installing it, inviting people, each supported agent, and every command.
        </p>
        <div className="docs-hero-actions">
          <Link href="/docs/agent-network-quickstart" className="ld-btn">
            Quickstart
            <span className="ld-btn-arrow" aria-hidden="true">→</span>
          </Link>
          <Link href="/network" className="ld-btn ld-btn-secondary">
            About the network
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
