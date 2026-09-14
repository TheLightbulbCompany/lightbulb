import { getAllPosts } from "@/content/blog";
import { SITE_URL } from "@/lib/domain";

/**
 * `/blog/rss.xml` — RSS 2.0 feed for the blog, linked from `/blog` and every
 * post via `alternates.types` (which Next renders as
 * `<link rel="alternate" type="application/rss+xml">`).
 *
 * Built from `getAllPosts()`, the same source as `sitemap.ts` and `llms.txt`,
 * so a new post appears in all three without a second edit.
 */
export const dynamic = "force-static";

const FEED_TITLE = "The Lightbulb blog";
const FEED_DESCRIPTION =
  "Practical writing on AI agents, persistent workspaces, and building an AI team you actually own.";

/** XML text escaping. Post titles and descriptions are author-written prose. */
function esc(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function GET() {
  const posts = getAllPosts();
  const updated = posts[0]?.date;

  const items = posts
    .map((post) => {
      const url = `${SITE_URL}/blog/${post.slug}`;
      return [
        "    <item>",
        `      <title>${esc(post.title)}</title>`,
        `      <link>${url}</link>`,
        `      <guid isPermaLink="true">${url}</guid>`,
        `      <pubDate>${new Date(`${post.date}T00:00:00Z`).toUTCString()}</pubDate>`,
        `      <description>${esc(post.description)}</description>`,
        ...post.tags.map((tag) => `      <category>${esc(tag)}</category>`),
        "    </item>",
      ].join("\n");
    })
    .join("\n");

  const xml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
    "  <channel>",
    `    <title>${esc(FEED_TITLE)}</title>`,
    `    <link>${SITE_URL}/blog</link>`,
    `    <description>${esc(FEED_DESCRIPTION)}</description>`,
    "    <language>en</language>",
    `    <atom:link href="${SITE_URL}/blog/rss.xml" rel="self" type="application/rss+xml" />`,
    ...(updated
      ? [`    <lastBuildDate>${new Date(`${updated}T00:00:00Z`).toUTCString()}</lastBuildDate>`]
      : []),
    items,
    "  </channel>",
    "</rss>",
    "",
  ].join("\n");

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
