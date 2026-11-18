import type { Metadata } from "next";
import Link from "next/link";

import { Navbar } from "@/components/landing/Navbar";
import { Footer } from "@/components/landing/Footer";
import { getAllPosts } from "@/content/blog";
import { formatPostDate } from "@/content/blog/format";

const TITLE = "Blog — Lightbulb";
const DESCRIPTION =
  "Notes for salon, spa and barbershop owners on answering the phone and getting the booking.";

export const metadata: Metadata = {
  title: TITLE,
  description: DESCRIPTION,
  // An empty index is not worth a search result; it indexes again with its first post.
  ...(getAllPosts().length ? {} : { robots: { index: false, follow: true } }),
  alternates: {
    canonical: "/blog",
    // Renders <link rel="alternate" type="application/rss+xml">, which is how
    // feed readers and several answer-engine crawlers discover new posts.
    types: { "application/rss+xml": [{ url: "/blog/rss.xml", title: TITLE }] },
  },
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    url: "/blog",
    type: "website",
    images: [{ url: "/og-lightbulb.png", width: 1200, height: 630, alt: "Lightbulb" }],
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: DESCRIPTION,
    images: ["/og-lightbulb.png"],
  },
};

export default function BlogIndexPage() {
  const posts = getAllPosts();

  return (
    <main className="ld-page min-h-screen">
      <Navbar />

      <section className="blog-wrap">
        <header className="blog-header">
          <p className="blog-eyebrow">field_notes</p>
          <h1 className="blog-title">The Lightbulb blog</h1>
          <p className="blog-lede">{DESCRIPTION}</p>
        </header>

        {posts.length === 0 && <p className="blog-lede">No posts yet.</p>}

        <ul className="blog-list">
          {posts.map((post) => (
            <li key={post.slug} className="blog-card">
              <Link href={`/blog/${post.slug}`} className="blog-card-link">
                <div className="blog-card-meta">
                  <time dateTime={post.date}>{formatPostDate(post.date)}</time>
                  <span aria-hidden="true">·</span>
                  <span>{post.readingMinutes} min read</span>
                </div>
                <h2 className="blog-card-title">{post.title}</h2>
                <p className="blog-card-desc">{post.description}</p>
                <span className="blog-card-cta" aria-hidden="true">
                  Read
                  <span className="blog-card-arrow">→</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <Footer />
    </main>
  );
}
