import { getAllPosts } from "@/content/blog";
import { COMPARE_PAGES } from "@/content/compare";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { PRODUCT_FACTS, PRODUCT_SUMMARY } from "@/lib/seo";

/**
 * `/llms.txt` — a Markdown index of the public site for answer engines (the
 * llms.txt convention: an H1, a blockquote summary, then link sections).
 *
 * Generated from the same sources as `sitemap.ts` so it cannot drift. The
 * facts come from lib/seo.tsx, where every line is a VERIFIED claim; no price
 * is typed here: the price lives in /pricing.md, which reads the billing API
 * every 5 minutes, while this file is static.
 */
export const dynamic = "force-static";

function line(name: string, path: string, description: string) {
  return `- [${name}](${SITE_URL}${path}): ${description}`;
}

export function GET() {
  const posts = getAllPosts();
  const body = [
    "# Lightbulb",
    "",
    `> ${PRODUCT_SUMMARY}`,
    "",
    "## Key facts",
    "",
    ...PRODUCT_FACTS.map((f) => `- ${f}`),
    "",
    "## Pages",
    "",
    line("Home", "/", "Lightbulb's three products."),
    line("Lightbulb Phone", "/phone", "What the AI receptionist does and how to set it up."),
    line("Lightbulb Meetings", "/meetings", "The Google Meet assistant; waitlist."),
    line("Lightbulb OS", "/os", "A voice app for the Mac; waitlist."),
    line("Pricing", "/pricing", "The monthly Lightbulb Phone plan for one business."),
    line("What happens to your data", "/security", "Where data is kept, which providers see it, and what we don't claim."),
    "",
    "## Comparisons",
    "",
    ...COMPARE_PAGES.map((page) => line(page.h1, page.path, page.description)),
    line("All comparisons", "/compare", "An AI receptionist next to voicemail, a front desk receptionist and a human answering service."),
    "",
    ...(posts.length
      ? ["## Blog", "", ...posts.map((post) => line(post.title, `/blog/${post.slug}`, post.description)), ""]
      : []),
    "## Developer network (a separate, free tool)",
    "",
    line("Lightbulb network", "/network", "A free command that lets coding agents message each other, peer to peer and end-to-end encrypted."),
    ...getAllDocs().map((doc) => line(doc.title, `/docs/${doc.slug}`, doc.description)),
    "",
    "## Optional",
    "",
    line("Full text", "/llms-full.txt", "The facts, comparisons and every network doc in one file."),
    "",
  ].join("\n");

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
