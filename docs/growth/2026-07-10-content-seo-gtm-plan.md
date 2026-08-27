# Lightbulb AI — Content & SEO-Led Growth Plan

_Date: 2026-07-10 · Owner: Prez · Status: draft for discussion_

Framing: Lightbulb AI is **already launched** (live product, $50/mo, 14-day trial, marketplace,
Tauri desktop app, Friday/Atlas seed agents). This is a **post-launch, organic-led growth
plan** weighted toward content/SEO — the channel the founder asked about — with the honest
caveat that SEO is the slow-compounding lever, not the this-quarter trial lever.

---

## 0. Reality check (the "launch readiness" equivalent)

- **Product is sufficient.** Lead with the actual on-site positioning: **"your own private AI
  pod"** — an isolated container that's yours alone, seeded with a *team* (Friday the
  orchestrator + Atlas the recruiter), persistent memory + workspace, Slack/Discord/Telegram
  channels, cron, skills, MCP. Sharper than "AI agent builder." (Note: the site says "pod" and
  "team," not "AI CEO/company" — content should match that language.)
- **Audience exists but is niche.** Realistic near-term ICP: technical founders / indie
  hackers / solopreneurs who already believe in agents, plus the **OpenClaw community** (warm —
  Lightbulb AI is literally built on their fork). This is thousands, not millions — fine at this stage.
- **The activation bottleneck is now being actively fixed (updated 2026-07-15).** Two big things
  shipped since this plan was drafted:
  - **No-card free tier is LIVE** (#1201/#1231, flag removed #1240) — signup auto-provisions a
    private trial pod on Bedrock, no credit card. This removes the single biggest top-of-funnel
    friction and makes *every* incremental visitor far more valuable.
  - **"Aha" first-run onboarding shipped** (#1240/#1242/#1243/#1244) — research-and-docs-first
    run, visible auto-kickoff, "you pick" fallback.
  - **Implication for sequencing:** the "fix activation before pouring traffic in" caveat is now
    largely being addressed in parallel by the product team. That **de-risks investing in
    content/SEO now** — the funnel can absorb traffic. Keep watching the trial→activated metric,
    but content work no longer has to wait behind it.

---

## 1. The SEO structural decision (settle this first)

**Use `flickmeon.ai/blog` (subfolder), NOT `blog.flickmeon.ai` (subdomain).**

- Subfolder consolidates all link equity on one young domain; a subdomain silos it.
- Lightbulb AI is Next.js on Vercel → `/blog` is a trivial App Router route with free SSG/SSR.
- No technical reason to reach for a subdomain (that's only for un-mergeable hosted CMSs).
- Ship with: per-post `<title>`/meta, OpenGraph, `Article` JSON-LD, `sitemap.xml`, fast LCP.

---

## 2. Channels ranked by expected impact (first 90 days)

| # | Channel | CAC | Time to results | First action this week |
|---|---------|-----|-----------------|------------------------|
| 1 | **OpenClaw community** (Discord/GitHub/subreddit) | ~$0 | Days | Post a genuine "here's what we built on top of OpenClaw" show-and-tell; be a contributor, not a billboard |
| 2 | **Reddit** (r/AI_Agents, r/LocalLLaMA, r/SideProject, r/selfhosted) | ~$0 | Days–weeks | One high-effort build-log post in r/AI_Agents (not a launch ad) |
| 3 | **Content/SEO blog** (`flickmeon.ai/blog`) | ~$0 marginal | 3–9 months | Publish the 2 highest-intent posts below; wire sitemap + schema |
| 4 | **Hacker News (Show HN)** | ~$0 | One-shot spike | Prep a "Show HN: Lightbulb AI — give an AI agent its own persistent workspace + Slack" |
| 5 | **X/Twitter build-in-public** | ~$0 | Weeks–months | Start a weekly thread cadence: agent demos + trial metrics |

Deliberately **not** prioritizing Product Hunt (one-time spike, you've likely spent that card
or it's better saved for a bigger milestone like desktop app GA) or paid ads (CAC unproven,
skip until activation is solid).

---

## 3. Content strategy — posts that target real search intent

Lead with **long-tail, problem-specific** queries you can actually rank for. Avoid head terms
("AI agent platform") — bloodbath. Each post below = search intent + a natural Lightbulb AI payoff.

1. **"How to give an AI agent its own persistent workspace (that survives restarts)"**
   — maps to the per-user container/EFS differentiator. Distribute: r/AI_Agents, r/LocalLLaMA,
   HN comment when relevant, X thread.
2. **"How to connect an AI agent to a Slack / Discord / Telegram channel"**
   — high intent, tutorial-shaped, directly demos the channels feature. Distribute:
   r/selfhosted, r/automation, dev.to cross-post.
3. **"Self-hosted vs managed AI agents: when to run your own OpenClaw"**
   — captures OpenClaw-curious searchers, positions Lightbulb AI as the managed path. Distribute:
   OpenClaw Discord, r/LocalLLaMA, HN.
4. **"Lightbulb AI vs [competitor: Lindy / Relevance AI / CrewAI]"** comparison page
   — bottom-funnel, buyers comparing. One per competitor over time. Distribute: organic search
   + answer relevant Reddit/HN threads (disclosed).
5. **"I gave an AI 'CEO' a team of agents for 14 days — here's what happened"**
   — narrative/build-log using your own Friday+Atlas trial. Distribute: X thread, r/SideProject,
   Indie Hackers, repurpose into a YouTube/Loom demo.

**Repurposing rule:** every blog post → 1 X thread + 1 Reddit post + 1 LinkedIn post +
snippets for the landing page. Write once, distribute five ways.

---

## 4. Landing / conversion assets (specific, not "hero + features")

- **Above fold:** the Friday story — "Meet Friday, your AI CEO. She hires the team." + live
  demo GIF of an agent doing real work (not a static screenshot).
- **"Watch it work" section:** 60–90s Loom of provisioning → chat → agent completes a task →
  posts result to Slack. This is the single highest-leverage asset (see §7).
- **Roster/marketplace strip:** show Atlas recruiting + a few marketplace agents — proves it's
  a *team*, not one bot.
- **Objection-killer FAQ:** "Is my data isolated?" (yes — own container), "Can I use my own
  API key?" (yes — BYOK/ChatGPT OAuth/Bedrock), "What happens after 14 days?"
- **Social proof:** even 2–3 trial-user quotes > zero. Ask your most active trial users today.

---

## 5. Communities to engage (be a member, not a spammer)

- **OpenClaw Discord + GitHub Discussions** — warmest possible audience; contribute fixes/plugins
  (you already upstream patches), mention Lightbulb AI contextually.
- **r/AI_Agents** — the single best subreddit for this ICP; build-logs and honest teardowns win.
- **Indie Hackers** — post revenue/activation milestones; the build-in-public crowd converts.

Partnerships / integrations worth chasing:
- **OpenClaw itself** — a "powered by / hosted OpenClaw" nod or listing; symbiotic.
- **A marketplace-agent creator** — co-market a killer published agent as a distribution wedge.

**Creative growth hack (Lightbulb AI-specific):** let Friday/Atlas write the build-in-public content.
"My AI CEO wrote this week's changelog" — the product dogfoods its own marketing, which is both
proof and content. Nobody else can copy it because nobody else ships a Friday.

---

## 6. Metrics (first 90 days)

| Metric | D30 | D60 | D90 | Tool | If below target |
|--------|-----|-----|-----|------|-----------------|
| Trial signups / wk | 10 | 25 | 50 | PostHog | Volume problem → double down on Reddit/OpenClaw |
| **Trial→activated** (first real agent task) | 40% | 50% | 60% | PostHog funnel | **Stop traffic spend, fix onboarding** |
| Trial→paid | 8% | 12% | 15% | Stripe | Pricing/value gap → user interviews |
| Blog organic sessions | 50 | 300 | 1000 | GA/Plausible | Thin content → more depth, backlinks |
| Reddit/HN qualified clicks | 200 | 500 | 800 | UTM + PostHog | Wrong sub or salesy tone → adjust |

Activation is the metric that gates everything — it's first for a reason.

---

## 7. Budget ($0–$1K/mo)

- **Free (90% of effort):** all content, all community, blog on existing Vercel, PostHog free
  tier, organic X/Reddit/HN.
- **Worth paying for:** a **$100–300 one-time investment in one great demo video** (or a
  Descript/Loom Pro sub) — the "watch it work" asset feeds the landing page, every Reddit post,
  X, and PH. Highest ROI dollar you can spend at this stage.
- **Skip for now:** paid search/social ads (CAC unproven until activation is fixed), PR,
  influencer sponsorships.

---

## Immediate next actions (this week)

1. **Decide:** `flickmeon.ai/blog` subfolder — I can scaffold the Next.js route + SEO plumbing.
2. **Write** posts #1 and #2 (highest intent, fastest to draft).
3. **Record** the 60–90s "watch it work" demo.
4. **Post** one honest build-log in r/AI_Agents + the OpenClaw Discord.
5. **Instrument** the trial→activated funnel in PostHog if not already clean.
