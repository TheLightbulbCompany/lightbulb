import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { PRODUCT_FACTS, PRODUCT_SUMMARY } from "@/lib/seo";

/**
 * `/llms-full.txt` — the companion to `/llms.txt`: the product facts and
 * the full Markdown of every network doc. Same sources as `sitemap.ts`.
 */
export const dynamic = "force-static";

export function GET() {
  const s: string[] = ["# Lightbulb", "", `> ${PRODUCT_SUMMARY}`, "", "## Key facts", "", ...PRODUCT_FACTS.map((f) => `- ${f}`), ""];

  s.push("---", "", "# Developer network docs (a separate, free tool)", "");
  for (const doc of getAllDocs()) {
    s.push(`## ${doc.title}`, "", `Source: ${SITE_URL}/docs/${doc.slug}`, "", doc.description, "", doc.content.trim(), "");
  }

  return new Response(s.join("\n"), {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
