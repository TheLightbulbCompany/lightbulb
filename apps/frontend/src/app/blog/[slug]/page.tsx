import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { Navbar } from "@/components/landing/Navbar";
import { Footer } from "@/components/landing/Footer";
import { BlogArticleBody } from "@/components/blog/BlogArticleBody";
import { getAllPosts, getPostBySlug } from "@/content/blog";
import { formatPostDate } from "@/content/blog/format";
import { SITE_URL } from "@/lib/domain";

export const dynamicParams = false;

export function generateStaticParams() {
  return getAllPosts().map((post) => ({ slug: post.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const post = getPostBySlug(slug);
  if (!post) return { title: "Not found — Lightbulb AI" };

  const url = `/blog/${post.slug}`;
  return {
    title: `${post.title} — Lightbulb AI`,
    description: post.description,
    alternates: { canonical: url },
    openGraph: {
      title: post.title,
      description: post.description,
      url,
      type: "article",
      publishedTime: post.date,
      authors: [post.author],
      tags: post.tags,
      images: [{ url: "/og-image.png", width: 1200, height: 630, alt: "Lightbulb AI" }],
    },
    twitter: {
      card: "summary_large_image",
      title: post.title,
      description: post.description,
      images: ["/og-image.png"],
    },
  };
}

export default async function BlogPostPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const post = getPostBySlug(slug);
  if (!post) notFound();

  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: post.title,
    description: post.description,
    datePublished: post.date,
    dateModified: post.date,
    author: { "@type": "Organization", name: post.author },
    publisher: {
      "@type": "Organization",
      name: "Lightbulb AI",
      logo: { "@type": "ImageObject", url: `${SITE_URL}/og-image.png` },
    },
    mainEntityOfPage: { "@type": "WebPage", "@id": `${SITE_URL}/blog/${post.slug}` },
    keywords: post.tags.join(", "),
  };

  return (
    <main className="ld-page min-h-screen">
      <Navbar />

      <article className="blog-wrap blog-article">
        <script
          type="application/ld+json"
          // Escape `<` so a value containing `</script>` can't break out of the tag.
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
          }}
        />

        <nav className="blog-back">
          <Link href="/blog">← All posts</Link>
        </nav>

        <header className="blog-article-header">
          <div className="blog-card-meta">
            <time dateTime={post.date}>{formatPostDate(post.date)}</time>
            <span aria-hidden="true">·</span>
            <span>{post.readingMinutes} min read</span>
          </div>
          <h1 className="blog-article-title">{post.title}</h1>
          <p className="blog-lede">{post.description}</p>
        </header>

        <BlogArticleBody source={post.content} />

        <footer className="blog-article-footer">
          <h2>Start your own AI pod</h2>
          <p>
            A private, isolated pod with a team of agents and persistent memory —
            free to start, no credit card required.
          </p>
          <Link href="/chat" className="ld-btn">
            Start your pod
            <span className="ld-btn-arrow" aria-hidden="true">
              →
            </span>
          </Link>
        </footer>
      </article>

      <Footer />
    </main>
  );
}
