import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { BlogArticleBody } from "@/components/blog/BlogArticleBody";
import { getAllDocs, getDocBySlug } from "@/content/docs";
import { JsonLd, breadcrumbJsonLd, pageMetadata, techArticleJsonLd } from "@/lib/seo";

export const dynamicParams = false;

export function generateStaticParams() {
  return getAllDocs().map((doc) => ({ slug: doc.slug }));
}

type DocRouteProps = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: DocRouteProps): Promise<Metadata> {
  const { slug } = await params;
  const doc = getDocBySlug(slug);
  if (!doc) return {};
  return pageMetadata({
    title: `${doc.title} — Lightbulb Docs`,
    description: doc.description,
    path: `/docs/${doc.slug}`,
  });
}

export default async function DocPage({ params }: DocRouteProps) {
  const { slug } = await params;
  const doc = getDocBySlug(slug);
  if (!doc) notFound();

  return (
    <article className="blog-article">
      <JsonLd
        data={techArticleJsonLd({
          title: doc.title,
          description: doc.description,
          path: `/docs/${doc.slug}`,
          section: "Documentation",
        })}
      />
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Docs", path: "/docs" },
          { name: doc.title, path: `/docs/${doc.slug}` },
        ])}
      />
      <header className="blog-article-header">
        <p className="blog-eyebrow">docs</p>
        <h1 className="blog-article-title">{doc.title}</h1>
        <p className="blog-lede">{doc.description}</p>
      </header>
      <BlogArticleBody source={doc.content} />
    </article>
  );
}
