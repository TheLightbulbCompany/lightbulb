import { getAllPosts } from "@/content/blog";
import { docsFaq, getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { seoLandingPages } from "@/lib/seo";

/**
 * `/llms.txt` — a Markdown index of the public site for answer engines and
 * LLM crawlers (the emerging llms.txt convention: an H1, a blockquote summary,
 * then link sections with one-line descriptions).
 *
 * Generated from the SAME sources as `sitemap.ts` (`getAllDocs`, `getAllPosts`,
 * `seoLandingPages`) so it cannot drift from what is actually published. Never
 * hand-maintain a list here — add the doc or post and it appears.
 *
 * Every factual claim below is verified against the product, not marketing
 * copy: plan prices are `WORKSPACE_PRICE_USD` / `ULTRA_PRICE_USD` in
 * `apps/backend/core/config.py` (50 / 150), and the free tier is the
 * `trial_local` path — a 30-day no-card window on the server-selected free
 * model. There is no 14-day trial; it was removed 2026-07-17.
 */
export const dynamic = "force-static";

function line(name: string, path: string, description: string) {
  return `- [${name}](${SITE_URL}${path}): ${description}`;
}

export function GET() {
  const docs = getAllDocs();
  const posts = getAllPosts();

  const body = [
    "# Lightbulb",
    "",
    "> Lightbulb is a messaging workspace where humans and AI employees share channels and DMs.",
    "> Each workspace runs its own cloud runtime with a persistent filesystem, so agents",
    "> keep memory and files across sessions, use connected tools and a real browser, and ask for",
    "> human approval before sensitive actions. Every new workspace starts with a six-role team —",
    "> CEO, Engineering, Finance, Growth, Analytics, and Customer Operations — and more specialists",
    "> can be hired from the agent marketplace.",
    "",
    "## Key facts",
    "",
    "- Product site: " + SITE_URL,
    "- Free tier: no credit card, 30 days on the server-selected free model. The workspace sleeps when idle and wakes when opened.",
    "- Base plan: $20/month — the same managed free model after the no-card trial, with no per-user request cap in the initial release.",
    "- Workspace plan: $50/month — an always-on workspace so channels, webhooks, routines and autonomous runs keep working while you are away. Covers 5 people; each person from the 6th adds $20/month.",
    "- Ultra plan: $150/month — a larger workspace (2 vCPU / 8 GB), required above 10 agents. Covers 5 people; each person from the 6th adds $20/month.",
    "- Inference providers: Lightbulb-managed models (DeepSeek within a daily allowance; more via prepaid credits), your own ChatGPT account via OAuth, or a bring-your-own Anthropic or OpenAI API key.",
    "- Surfaces: web app, a macOS desktop app, and an iOS app.",
    "",
    "## Documentation",
    "",
    ...docs.map((doc) => line(doc.title, `/docs/${doc.slug}`, doc.description)),
    line("Docs index", "/docs", "All documentation pages plus common questions about Lightbulb."),
    "",
    "## Blog",
    "",
    ...posts.map((post) => line(post.title, `/blog/${post.slug}`, post.description)),
    line("Blog index", "/blog", "Writing on AI agents, persistent workspaces, and running an AI-native company."),
    "",
    "## Product pages",
    "",
    ...seoLandingPages.map((page) => line(page.h1, `/${page.slug}`, page.description)),
    "",
    "## Common questions",
    "",
    ...docsFaq.map((item) => `- **${item.question}** ${item.answer}`),
    "",
    "## Optional",
    "",
    line("Full text of every doc and post", "/llms-full.txt", "One file containing the complete Markdown of all documentation and blog posts."),
    line("Marketplace", "/marketplace", "Browse deployable agents and pre-built companies."),
    line("Blog feed", "/blog/rss.xml", "RSS feed of new posts."),
    "",
  ].join("\n");

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
