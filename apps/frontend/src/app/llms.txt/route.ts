import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { PRODUCT_FACTS, PRODUCT_SUMMARY } from "@/lib/seo";

/**
 * `/llms.txt` — a Markdown index of the public site for answer engines (the
 * llms.txt convention: an H1, a blockquote summary, then link sections).
 *
 * Generated from the same sources as `sitemap.ts` so it cannot drift. The
 * facts come from lib/seo.tsx, where every line is a VERIFIED claim; no price
 * is typed here.
 */
export const dynamic = "force-static";

function line(name: string, path: string, description: string) {
  return `- [${name}](${SITE_URL}${path}): ${description}`;
}

export function GET() {
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
    line("Spark", "/os", "A voice app for the Mac; download."),
    "",
    "## Developer network (a separate, free tool)",
    "",
    line("Lightbulb network", "/network", "A free command that lets coding agents message each other, peer to peer and end-to-end encrypted."),
    ...getAllDocs().map((doc) => line(doc.title, `/docs/${doc.slug}`, doc.description)),
    "",
    "## Optional",
    "",
    line("Full text", "/llms-full.txt", "The facts and every network doc in one file."),
    "",
  ].join("\n");

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
