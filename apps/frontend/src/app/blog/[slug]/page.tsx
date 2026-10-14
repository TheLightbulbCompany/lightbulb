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
  if (!post) return { title: "Not found — Lightbulb" };

  const url = `/blog/${post.slug}`;
  return {
    title: `${post.title} — Lightbulb`,
    description: post.description,
    alternates: {
      canonical: url,
      types: {
        "application/rss+xml": [{ url: "/blog/rss.xml", title: "The Lightbulb blog" }],
      },
    },
    openGraph: {
      title: post.title,
      description: post.description,
      url,
      type: "article",
      publishedTime: post.date,
      authors: [post.author],
      tags: post.tags,
      images: [{ url: "/og-lightbulb.png", width: 1200, height: 630, alt: "Lightbulb" }],
    },
    twitter: {
      card: "summary_large_image",
      title: post.title,
      description: post.description,
      images: ["/og-lightbulb.png"],
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
    "@type": "BlogPosting",
    headline: post.title,
    description: post.description,
    datePublished: post.date,
    dateModified: post.date,
    image: `${SITE_URL}/og-lightbulb.png`,
    author: { "@type": "Person", name: post.author },
    // Inline, not a bare @id: the entities are defined on the homepage only.
    publisher: {
      "@type": "Organization",
      "@id": `${SITE_URL}/#organization`,
      name: "Lightbulb",
      url: SITE_URL,
      logo: { "@type": "ImageObject", url: `${SITE_URL}/icon.png` },
    },
    about: { "@type": "SoftwareApplication", "@id": `${SITE_URL}/#software`, name: "Lightbulb", url: SITE_URL },
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
            <span aria-hidden="true">·</span>
            <span>By {post.author}</span>
          </div>
          <h1 className="blog-article-title">{post.title}</h1>
          <p className="blog-lede">{post.description}</p>
        </header>

        <BlogArticleBody source={post.content} />

        <footer className="blog-article-footer">
          <h2>Bring your team. Your AI employees are already in the room.</h2>
          <p>
            Setting up a team of 3 to 15? A founder sets it up with you in 45 minutes. Or start
            free: 30 days, no card.
          </p>
          <div className="flex flex-wrap gap-3">
            <Link href={`/request-setup?source=blog-${post.slug}`} className="ld-btn">
              Request a guided setup
              <span className="ld-btn-arrow" aria-hidden="true">
                →
              </span>
            </Link>
            <Link href={`/sign-up?source=blog-${post.slug}`} className="ld-btn">
              Start free
            </Link>
          </div>
        </footer>
      </article>

      <Footer />
    </main>
  );
}
