import { getAllPosts } from "@/content/blog";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";

/**
 * `/llms-full.txt` — the complete Markdown of every doc and blog post in one
 * file, the companion to `/llms.txt`'s index. An answer engine that wants the
 * source rather than a link list fetches this once instead of crawling each
 * page and stripping the app shell.
 *
 * Same generation rule as `llms.txt` and `sitemap.ts`: driven by `getAllDocs`
 * and `getAllPosts`, never a hand-kept list.
 */
export const dynamic = "force-static";

export function GET() {
  const sections: string[] = [
    "# Lightbulb — full documentation and blog",
    "",
    `> Every published documentation page and blog post from ${SITE_URL}, as Markdown.`,
    "> See /llms.txt for the short index.",
    "",
  ];

  sections.push("---", "", "# Documentation", "");
  for (const doc of getAllDocs()) {
    sections.push(
      `## ${doc.title}`,
      "",
      `Source: ${SITE_URL}/docs/${doc.slug}`,
      "",
      doc.description,
      "",
      doc.content.trim(),
      "",
    );
  }

  sections.push("---", "", "# Blog", "");
  for (const post of getAllPosts()) {
    sections.push(
      `## ${post.title}`,
      "",
      `Source: ${SITE_URL}/blog/${post.slug}`,
      `Published: ${post.date}`,
      "",
      post.description,
      "",
      post.content.trim(),
      "",
    );
  }

  return new Response(sections.join("\n"), {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
