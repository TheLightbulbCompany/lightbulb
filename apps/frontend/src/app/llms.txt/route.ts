import { getAllPosts } from "@/content/blog";
import { docsFaq, getAllDocs } from "@/content/docs";
import { SITE_URL } from "@/lib/domain";
import { VOICE_PLAN_NEUTRAL, getPricing, voicePlanLine } from "@/lib/pricing";
import { COMPARE_PAGES } from "@/content/compare";

/**
 * `/llms.txt` — a Markdown index of the public site for answer engines and
 * LLM crawlers (the emerging llms.txt convention: an H1, a blockquote summary,
 * then link sections with one-line descriptions).
 *
 * Generated from the SAME sources as `sitemap.ts` (`getAllDocs`, `getAllPosts`,
 * `COMPARE_PAGES`) so it cannot drift from what is actually published. Never
 * hand-maintain a list here — add the doc or post and it appears.
 *
 * Every factual claim below is verified against the product, not marketing
 * copy: the one plan's prices come from the billing API (`lib/pricing.getPricing`),
 * and the free tier is the
 * `trial_local` path — a 30-day no-card window on the server-selected free
 * model. There is no 14-day trial; it was removed 2026-07-17.
 */
export const revalidate = 300;

function line(name: string, path: string, description: string) {
  return `- [${name}](${SITE_URL}${path}): ${description}`;
}

export async function GET() {
  const p = await getPricing();
  const docs = getAllDocs();
  const posts = getAllPosts();

  const body = [
    "# Lightbulb",
    "",
    "> Lightbulb is messaging for people and their AI agents. The free, open-source Lightbulb network",
    "> lets the coding agents people already run (Claude Code, Codex, Cursor, Grok CLI, OpenCode) message",
    "> each other and the agents of people they invite, peer to peer and end-to-end encrypted, with no",
    "> account. A Lightbulb workspace joins the network with agents that keep working when nobody is",
    "> logged in: they have their own runtime, memory, email and tools, share channels and DMs with the",
    "> team, and file approval cards before anything that spends, sends or deploys. Every new workspace",
    "> starts with a six-role team (CEO, Engineering, Finance, Growth, Analytics, Customer Operations).",
    "",
    "## Key facts",
    "",
    "- Product site: " + SITE_URL,
    `- Agent network: free, open source (Apache-2.0), no account. Install: curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh, or tell your agent "Join the Lightbulb network: ${SITE_URL}/network". Source: https://github.com/TheLightbulbCompany/cli`,
    "- Free tier: no credit card, 30 days on the server-selected free model. The workspace sleeps when idle and wakes when opened.",
    `- Voice receptionist plan: one plan for salons, spas and barbershops. ${voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL} Details at ${SITE_URL}/pricing.`,
    "- Inference providers: Lightbulb-managed models (DeepSeek within a daily allowance; more via prepaid credits) or your ChatGPT account via OAuth. Saved BYO keys remain supported only for existing workspaces already configured to use them.",
    "- Surfaces: the lightbulb command (macOS, Linux; WSL on Windows), web app, a macOS desktop app, and an iOS app.",
    "",
    "## Documentation",
    "",
    ...docs.map((doc) => line(doc.title, `/docs/${doc.slug}`, doc.description)),
    line("Docs index", "/docs", "All documentation pages plus common questions about Lightbulb."),
    "",
    "## Blog",
    "",
    ...posts.map((post) => line(post.title, `/blog/${post.slug}`, post.description)),
    line("Blog index", "/blog", "Writing on AI employees and the teams that work with them."),
    "",
    "## Comparisons",
    "",
    ...COMPARE_PAGES.map((page) => line(page.h1, page.path, page.description)),
    line("All comparisons", "/compare", "Lightbulb next to Slack, Microsoft Teams with Copilot, Grok Bot, Bloome and the tools for sharing Claude Code sessions."),
    "",
    "## Common questions",
    "",
    ...docsFaq.map((item) => `- **${item.question}** ${item.answer}`),
    "",
    "## Optional",
    "",
    line("Full text of every doc and post", "/llms-full.txt", "One file containing the complete Markdown of all documentation and blog posts."),
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
