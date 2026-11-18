import { VERTICALS } from "@/components/vertical/verticals";
import { getAllPosts } from "@/content/blog";
import { COMPARE_PAGES } from "@/content/compare";
import { getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { PRODUCT_FACTS, PRODUCT_SUMMARY } from "@/lib/seo";

/**
 * `/llms-full.txt` — the companion to `/llms.txt`: the product facts, each
 * trade page's calls and questions, the comparisons, and the full Markdown of
 * every network doc and blog post. Same sources as `sitemap.ts`.
 */
export const dynamic = "force-static";

const qa = (faq: { q: string; a: string }[]) => faq.flatMap(({ q, a }) => [`**${q}** ${a}`, ""]);

export function GET() {
  const s: string[] = ["# Lightbulb", "", `> ${PRODUCT_SUMMARY}`, "", "## Key facts", "", ...PRODUCT_FACTS.map((f) => `- ${f}`), ""];

  for (const v of VERTICALS) {
    s.push(`## ${v.h1}`, "", `Source: ${SITE_URL}${v.path}`, "", v.intro, "");
    for (const c of v.calls) s.push(`- ${c.ask} ${c.does}`);
    s.push("", v.servicesIntro, "", ...qa(v.faq));
  }

  for (const p of COMPARE_PAGES) {
    s.push(`## ${p.h1}`, "", `Source: ${SITE_URL}${p.path}`, "", p.intro, "");
    for (const r of p.rows) s.push(`- ${r.label}: Lightbulb: ${r.ours} ${p.other}: ${r.other}`);
    s.push("", ...p.better.flatMap((b) => [b, ""]), ...qa(p.faq));
  }

  for (const post of getAllPosts()) {
    s.push(`## ${post.title}`, "", `Source: ${SITE_URL}/blog/${post.slug}`, `Published: ${post.date}`, "", post.description, "", post.content.trim(), "");
  }

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
