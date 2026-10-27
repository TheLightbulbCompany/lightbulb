# 05 · SEO, structured data and AI search

## Purpose

Moves the stage-1 goal "inbound launched": a founder who searches for, or asks an AI about, the problem we solve should land on a page that describes Lightbulb correctly and offers the setup door. Serves the business plan's Go To Market, "The website", and §6 positioning.

## What we know

Checked on 13 Oct 2026 against `origin/main` at `6249f614d` and the live site. All findings below are first-party (code or a live fetch) unless marked.

| Fact | Evidence | Confidence |
|---|---|---|
| Canonical host is `https://thelightbulb.company`; `http://` and `www.` 308 to it | `curl` on both; `SITE_URL` in `apps/frontend/src/lib/domain.ts` | high |
| `flickmeon.ai` and `www.flickmeon.ai` root serve the launch competition (200, canonical `https://flickmeon.ai`); every other path 308s to the same path on the product domain (`/docs`, `/blog/<slug>`, `/robots.txt`, `/sitemap.xml` checked) | `curl` | high |
| `/pricing` and `/request-setup` return 404 (with `noindex`) | `curl` | high |
| Nav and footer "Pricing" link to `/#pricing`, an anchor, not a page | `Navbar.tsx`, `Footer.tsx` | high |
| Sitemap lists 34 URLs: 13 static, 5 docs, 10 `[seoSlug]` landing pages, 6 blog posts | live `/sitemap.xml`, `app/sitemap.ts` | high |
| robots.txt allows everything public, blocks app routes, names the sitemap; no AI crawler is blocked | live `/robots.txt`, `app/robots.ts` | high |
| `/llms.txt` and `/llms-full.txt` exist (200), generated from the same sources as the sitemap | `app/llms.txt/route.ts` | high |
| Homepage JSON-LD: Organization, WebSite, SoftwareApplication (offer price `0`); docs: CollectionPage + FAQPage; doc pages: TechArticle + BreadcrumbList; blog posts: Article; landing pages and /marketplace: SoftwareApplication + BreadcrumbList; /blog and /sign-up: none | parsed from live HTML (server-rendered, so `curl` sees it) | high |
| Search Console: 3 indexed / 34 excluded before #3840 | brief; not re-checked | unverified |
| Lightbulb does not appear for any of the five test queries, and a brand search returns two unrelated companies with our name | WebSearch, 13 Oct (see Part C) | medium (one engine, one run each) |
| Google stopped showing FAQ rich results on 7 May 2026 | [Google FAQPage docs changelog](https://developers.google.com/search/docs/appearance/structured-data/faqpage) | medium (one first-party source) |
| A SoftwareApplication rich result needs `aggregateRating` or `review` | [Google software-app docs](https://developers.google.com/search/docs/appearance/structured-data/software-app) | medium |

## Findings

The site is technically sound (fast, server-rendered, HTTPS, clean redirects, sitemap, robots, llms.txt). It is not found because of what it says and what it lacks, not because of crawl errors. Three things matter most:

1. **The pages describe a different product.** The root metadata, WebSite JSON-LD, llms.txt and docs FAQ still sell "AI agents for autonomous businesses", "AI-native company" and a three-plan menu with stale specs. The plan's product is a team workspace for founder-led teams. AI engines and Google read the old story.
2. **There is no pricing page and nothing to rank for the wedge's queries.** The founder questions in Part C have no page on our site that answers them. Ten thin landing pages target other queries and are linked from nowhere.
3. **The brand is ambiguous and unreferenced.** "Lightbulb" collides with TheLightbulb.ai (an emotion-AI startup) and The Light Bulb Company (a UK bulb retailer). No third-party page describes us. One `sameAs` link 404s.

---

## Part A — The audit, ranked by impact

| # | Issue | Impact | Evidence | File to change |
|---|---|---|---|---|
| A1 | `/pricing` 404. Nav "Pricing" is `/#pricing`, and the homepage plan cards show provider paths, not prices. No crawlable answer to "what does it cost". | High | `curl /pricing` → 404 | new `apps/frontend/src/app/pricing/page.tsx`; `Navbar.tsx`, `Footer.tsx`; `app/sitemap.ts` |
| A2 | Old positioning in every machine-read surface. Root title "AI Agents for Autonomous Businesses"; WebSite description "autonomous operations"; docs FAQ "run an AI-native company"; blog index "AI-native company"; homepage H1 "Your team and your agents" vs the approved "Your team and your AI employees, in one place." | High | parsed titles/JSON-LD above | `app/layout.tsx` (`SITE_TITLE`, `SITE_DESCRIPTION`), `lib/seo.tsx` (`HOME_*`, `websiteJsonLd`, `softwareApplicationJsonLd`), `content/docs/index.ts` (`docsFaq`), `app/blog/page.tsx`, `components/landing/Hero.tsx` |
| A3 | Wrong facts on crawlable pages: Ultra "2 vCPU / 8 GB, required above 10 agents" (there is no per-workspace compute size any more, per CLAUDE.md "Billing System"); "isolated cloud container with a persistent filesystem"; homepage plan cards list "All channels (Telegram, Discord, WhatsApp)"; prices stated in three hand-written places. | High (AI engines quote these verbatim) | live `/llms.txt`, `/docs/get-started`, homepage HTML | `app/llms.txt/route.ts`, `content/docs/index.ts`, `content/docs/pages/get-started.md`, `components/landing/PricingThreeCard.tsx` |
| A4 | Ten `[seoSlug]` pages (`/ai-agent-workspace`, `/ai-agents-for-telegram`, `/autonomous-ai-company`, …) are thin (one paragraph, three bullets, one H2 from the footer), orphaned (no internal link from home, nav, footer, docs or blog) and off-wedge. Each also emits a SoftwareApplication with a different `name`, which splits the product entity into ten. The most likely cause of "34 excluded". | High | internal-link parse of home/docs/blog; `lib/seo.tsx` `seoLandingPages` | `lib/seo.tsx`, `app/[seoSlug]/page.tsx`, `app/sitemap.ts`, `next.config.ts` (redirects) |
| A5 | No pages for the wedge's queries: "share a Claude Code session with a teammate", "Slack alternative with AI agents", "AI employees for a small team", comparison pages. | High | Part C | new pages (see Build tasks B5–B6; comparison copy from workstream 11) |
| A6 | `/sign-up`, `/sign-in`, `/join/*` and the 404 page have no canonical and inherit the root title "Lightbulb — AI Agents for Autonomous Businesses"; `/sign-up` H1 "Your AI company starts here." | Medium | parsed `/sign-up`, `/pricing` 404 | `app/sign-up/layout.tsx`, `app/sign-in/layout.tsx`, `app/join/layout.tsx` (add `metadata`); root default in `app/layout.tsx` |
| A7 | Entity graph errors: Organization `sameAs` LinkedIn `/company/isol8ai/` returns 404 to an anonymous fetch; no `disambiguatingDescription`; blog Article uses the OG image as publisher logo, has no `image`, author is a free-text organization ("Lightbulb" vs "The Lightbulb team") with no `@id`; homepage SoftwareApplication offer is `price: 0` only. | Medium | live JSON-LD; `curl` on sameAs | `lib/seo.tsx`, `app/blog/[slug]/page.tsx`, `content/blog/posts/*.md` |
| A8 | Blog is off-wedge and uses internal names: tags include `paperclip`; posts on Slack/Discord/Telegram channel bots and "AI-native company OS"; all dated July–August; no named human author. | Medium | post front matter | `content/blog/posts/*.md` |
| A9 | `dev.thelightbulb.company` serves the full site with `robots: allow` and no `noindex`; its canonical points at prod, which limits the damage. | Medium-low | `curl` dev home + robots | `app/robots.ts` (env-aware), or `X-Robots-Tag: noindex` header for non-prod hosts in `next.config.ts` |
| A10 | `/launch` on the product domain is a duplicate of `flickmeon.ai` (canonical to flickmeon.ai, so acceptable); the launch page has no H1 and no link to the product or setup door. Until the competition is set up, `flickmeon.ai` redirects to the product site. | Low | parsed | `app/launch/page.tsx` |
| A11 | No `/blog` JSON-LD; docs FAQPage no longer earns a Google rich result (keep it for AI engines and Bing). | Low | parsed; Google changelog | `app/blog/page.tsx` |
| A12 | Measurement is blind: Vercel Web Analytics off; Search Console state unverified; Bing Webmaster Tools not known to be set up. | High for decisions, zero for ranking | brief | Search Console / Bing (founder access) |

What is fine and should not be touched: HTTPS + HSTS, 308s from http/www/flickmeon.ai paths, server-rendered HTML, self-canonicals on docs and blog, one H1 per page, decorative images carrying `alt=""`, robots.txt open to GPTBot/ClaudeBot/PerplexityBot, the RSS feed, llms.txt generated from source.

---

## Part B — The schema plan

Rule: mark up only what the page shows. One product entity (`#software`) and one organization (`#organization`), referenced by `@id` everywhere. Do not add ratings or reviews until real ones exist on the page; that means no SoftwareApplication rich result for now, which is the right trade.

| Page | Types |
|---|---|
| Home `/` | `@graph`: Organization, WebSite, SoftwareApplication (with the plan's offer), FAQPage (the homepage FAQ from the website brief), WebPage |
| `/pricing` (new) | `@graph`: WebPage, Product with `offers` (the $50 plan + member add-on), FAQPage (pricing questions), BreadcrumbList |
| `/request-setup` (new) | WebPage + BreadcrumbList; ContactPoint on the Organization |
| `/docs` | CollectionPage + FAQPage (keep; fix answers per A2/A3) |
| `/docs/<slug>` | TechArticle + BreadcrumbList (keep); add `about: {"@id": "#software"}` |
| `/blog` | Blog with `blogPost` list + BreadcrumbList |
| `/blog/<slug>` | BlogPosting (subtype of Article) with `image`, `author` as a Person, `publisher` → `#organization`, `about` → `#software`; BreadcrumbList |
| Comparison pages (`/compare/<x>`) | WebPage + BreadcrumbList; a visible comparison table, no Product for competitors |
| Landing pages kept from A4 | WebPage + BreadcrumbList, `about` → `#software` (not a new SoftwareApplication) |
| `/marketplace`, listings | CollectionPage; each listing: Product with its real price only if shown |
| `/sign-up`, `/sign-in`, `/join` | none |

### Homepage JSON-LD (replaces the three blocks in `app/page.tsx`)

The FAQ answers must appear word for word on the page (the website brief's FAQ, workstream 10 owns final copy). The `offers` block ships with `/pricing`, after seat billing and the trial checkout are built, and says what the visible pricing says. There is no interim variant.

```json
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": "https://thelightbulb.company/#organization",
      "name": "Lightbulb",
      "alternateName": ["Lightbulb AI", "The Lightbulb Company"],
      "legalName": "Isol8 Inc.",
      "url": "https://thelightbulb.company",
      "logo": "https://thelightbulb.company/icon.png",
      "description": "Lightbulb makes a team workspace where people and AI employees work together in channels.",
      "disambiguatingDescription": "Lightbulb (thelightbulb.company) is team software for people and AI employees. Not TheLightbulb.ai, the emotion-AI company, and not The Light Bulb Company (UK), the lighting retailer.",
      "email": "support@thelightbulb.company",
      "sameAs": ["https://x.com/lightbulbai"]
    },
    {
      "@type": "WebSite",
      "@id": "https://thelightbulb.company/#website",
      "name": "Lightbulb",
      "url": "https://thelightbulb.company",
      "inLanguage": "en",
      "publisher": { "@id": "https://thelightbulb.company/#organization" }
    },
    {
      "@type": "SoftwareApplication",
      "@id": "https://thelightbulb.company/#software",
      "name": "Lightbulb",
      "applicationCategory": "BusinessApplication",
      "applicationSubCategory": "Team communication",
      "operatingSystem": "Web, macOS, iOS",
      "url": "https://thelightbulb.company",
      "description": "A team workspace where AI employees are members: put them in a channel, mention one, and the work comes back where the whole team can see it, approve it or pick it up.",
      "featureList": [
        "Six AI employees in every new workspace: CEO, Engineering, Finance, Growth, Analytics, Customer Operations",
        "Channels, direct messages and group chats shared by people and AI employees",
        "Approval cards for spend, sends, deploys and hires",
        "Shared Claude Code and Codex sessions a teammate can watch and pick up",
        "Any model: Lightbulb-managed, your ChatGPT account, or your own API key"
      ],
      "publisher": { "@id": "https://thelightbulb.company/#organization" },
      "offers": {
        "@type": "Offer",
        "name": "Lightbulb workspace",
        "price": "50.00",
        "priceCurrency": "USD",
        "url": "https://thelightbulb.company/pricing",
        "availability": "https://schema.org/InStock",
        "priceSpecification": {
          "@type": "UnitPriceSpecification",
          "price": "50.00",
          "priceCurrency": "USD",
          "unitCode": "MON",
          "referenceQuantity": { "@type": "QuantitativeValue", "value": 1, "unitCode": "MON" },
          "description": "Up to 5 members, then $20 a member. 30 days free, no card to start."
        }
      }
    },
    {
      "@type": "WebPage",
      "@id": "https://thelightbulb.company/#webpage",
      "url": "https://thelightbulb.company",
      "name": "Lightbulb: your team and your AI employees, in one place",
      "isPartOf": { "@id": "https://thelightbulb.company/#website" },
      "about": { "@id": "https://thelightbulb.company/#software" }
    },
    {
      "@type": "FAQPage",
      "@id": "https://thelightbulb.company/#faq",
      "mainEntity": [
        {
          "@type": "Question",
          "name": "We already have Slack. Why add Lightbulb?",
          "acceptedAnswer": { "@type": "Answer", "text": "Keep Slack. Lightbulb is where your AI employees do the work: each has its own tools, memory, email and approval rules, and the team joins the channels where an agent is working." }
        },
        {
          "@type": "Question",
          "name": "Claude already does this for me. What changes?",
          "acceptedAnswer": { "@type": "Answer", "text": "For one person, nothing. Lightbulb earns its place when a second person needs to see, continue or approve the work, including a shared Claude Code or Codex session." }
        },
        {
          "@type": "Question",
          "name": "Will the agents do things without asking?",
          "acceptedAnswer": { "@type": "Answer", "text": "No. Spend, sends, deploys and hires wait on an approval card for a person, and every action is on the team's shared record." }
        },
        {
          "@type": "Question",
          "name": "What happens to our data?",
          "acceptedAnswer": { "@type": "Answer", "text": "Each workspace runs in its own isolated cloud environment. Agents only see the channels you add them to." }
        }
      ]
    }
  ]
}
```

### Pricing page JSON-LD (`app/pricing/page.tsx`)

```json
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebPage",
      "@id": "https://thelightbulb.company/pricing#webpage",
      "url": "https://thelightbulb.company/pricing",
      "name": "Lightbulb pricing: $50 a month for your team",
      "isPartOf": { "@id": "https://thelightbulb.company/#website" },
      "about": { "@id": "https://thelightbulb.company/#software" }
    },
    {
      "@type": "Product",
      "@id": "https://thelightbulb.company/pricing#product",
      "name": "Lightbulb workspace",
      "description": "One team workspace with six AI employees. Up to 5 members included; each member after the fifth is $20 a month. Price locked for two years. AI usage is billed as credits at provider price plus 20%, or bring your own ChatGPT account or API key.",
      "brand": { "@id": "https://thelightbulb.company/#organization" },
      "image": "https://thelightbulb.company/og-lightbulb-v2.png",
      "category": "Team communication software",
      "offers": [
        {
          "@type": "Offer",
          "name": "Workspace, up to 5 members",
          "price": "50.00",
          "priceCurrency": "USD",
          "availability": "https://schema.org/InStock",
          "url": "https://thelightbulb.company/pricing",
          "priceSpecification": {
            "@type": "UnitPriceSpecification",
            "price": "50.00",
            "priceCurrency": "USD",
            "unitCode": "MON",
            "referenceQuantity": { "@type": "QuantitativeValue", "value": 1, "unitCode": "MON" }
          },
          "eligibleQuantity": { "@type": "QuantitativeValue", "maxValue": 5, "unitText": "members" }
        },
        {
          "@type": "Offer",
          "name": "Each member after the fifth",
          "price": "20.00",
          "priceCurrency": "USD",
          "availability": "https://schema.org/InStock",
          "url": "https://thelightbulb.company/pricing",
          "priceSpecification": {
            "@type": "UnitPriceSpecification",
            "price": "20.00",
            "priceCurrency": "USD",
            "unitCode": "MON",
            "referenceQuantity": { "@type": "QuantitativeValue", "value": 1, "unitText": "member per month" }
          }
        },
        {
          "@type": "Offer",
          "name": "30 days free",
          "price": "0",
          "priceCurrency": "USD",
          "description": "No card to start. Founding teams put a card on file at their setup session; first charge on day 31. Lightbulb-managed AI within a daily allowance.",
          "availability": "https://schema.org/InStock",
          "url": "https://thelightbulb.company/request-setup"
        }
      ]
    },
    {
      "@type": "FAQPage",
      "mainEntity": [
        { "@type": "Question", "name": "Do I pay per person?", "acceptedAnswer": { "@type": "Answer", "text": "No. $50 a month covers the workspace and up to five people. Each person after the fifth is $20 a month." } },
        { "@type": "Question", "name": "How is AI usage billed?", "acceptedAnswer": { "@type": "Answer", "text": "Use Lightbulb-managed AI as prepaid credits at the provider's price plus 20%, or connect your own ChatGPT account or API key and pay your provider directly." } },
        { "@type": "Question", "name": "Is there a free trial?", "acceptedAnswer": { "@type": "Answer", "text": "Yes. 30 days free, no card to start; add one when the month ends. Founding teams put a card on file at their setup session, and the first charge is on day 31." } }
      ]
    },
    {
      "@type": "BreadcrumbList",
      "itemListElement": [
        { "@type": "ListItem", "position": 1, "name": "Home", "item": "https://thelightbulb.company/" },
        { "@type": "ListItem", "position": 2, "name": "Pricing", "item": "https://thelightbulb.company/pricing" }
      ]
    }
  ]
}
```

`unitCode` `MON` is UN/CEFACT for month. Product without `review`/`aggregateRating` will not earn a Google merchant rich result; it still gives AI engines and Bing a parseable price, which is the goal. Validate both blocks in the [Rich Results Test](https://search.google.com/test/rich-results) and the [Schema.org validator](https://validator.schema.org/) before merge.

---

## Part C — AI search

### What the engines say today (13 Oct 2026, one run each via WebSearch; ChatGPT and Perplexity could not be queried directly, Perplexity returned 403 to a fetch)

| Query | Lightbulb mentioned? | Who is described instead |
|---|---|---|
| "Lightbulb AI team workspace AI employees" | No | TheLightbulb.ai, an Indian emotion-AI startup ([Inc42](https://inc42.com/company/thelightbulb-ai/)); TeamAI, Taskade, Wonderchat |
| "thelightbulb.company" | No | The Light Bulb Company (UK), a lighting retailer ([Companies House](https://find-and-update.company-information.service.gov.uk/company/02855611)) |
| "Slack alternative with AI agents as teammates" | No | Slack itself ([Slack blog](https://slack.com/blog/news/slack-is-where-agents-work)), Runbear, eesel |
| "share a Claude Code session with my cofounder" | No | Lody ([blog](https://lody.ai/blog/handoff-claude-code-session/)), ccshare, Drev, claude-share |
| "AI employees for a small team" | No | Sista, Superpal, Kuse; vendor listicles |
| Extended: "team workspace where AI agents and people share channels, Claude Code sessions shared with teammates" | No | Bloome ([guide](https://bloome.im/guides/claude-code-team)), Nimbalyst ([comparison](https://nimbalyst.com/blog/claude-code-agent-teams-vs-parallel-sessions-vs-team-workspaces/)), Claude Code Agent Teams |
| Site-restricted search on thelightbulb.company + flickmeon.ai | No results | — |

Reading: Lightbulb is not retrieved, not cited and not recommended for any wedge query (medium confidence: one engine, one run; repeat 3–5 runs per platform for a rate). The brand query is lost to two namesakes. Two new adjacent products surfaced that the plan does not list: **Bloome** (Claude Code in a group chat) and **Nimbalyst** (shared workspace for people and their Claude Code/Codex agents). Workstream 11 should profile both.

### What would get Lightbulb described correctly

1. **One definition, everywhere.** A single 40–60 word definition, used verbatim as the homepage subhead, the first paragraph of `/docs`, the top of `llms.txt`, the Organization/SoftwareApplication `description`, the X bio and every directory listing: *"Lightbulb is a team workspace where people and AI employees work together. Every workspace starts with six AI employees. Mention one in a channel, it does the work, and the result comes back where the whole team can see it, approve it or pick it up."*
2. **Disambiguate the name.** Always "Lightbulb, the team workspace for people and AI employees" on first mention; `disambiguatingDescription` in the graph; a real LinkedIn company page under the Lightbulb name in `sameAs`.
3. **Pages that answer the wedge's questions, owned and official.** `/pricing`; `/claude-code-session-sharing` ("Share a Claude Code or Codex session with your team"); `/compare/slack`, `/compare/microsoft-teams`, `/compare/lody`, `/compare/grok-bot` and `/compare/bloome` (six pages in all) from workstream 11, each opening with a one-paragraph answer and a table. ChatGPT's recent shift toward official and `site:` retrieval favours owned product, docs and pricing pages (per the ai-seo skill's format-volatility note; unverified beyond that).
4. **Fix llms.txt** to the new definition, the plan's pricing, the founder-team audience, and links to pricing, setup and the comparison pages; add `/pricing.md`.
5. **Third-party presence.** AI engines cite third parties far more than a brand's own site. The cheapest real ones for us: the plugin listings in the Claude Code and Codex marketplaces (workstream 14), Show HN/Product Hunt (workstream 07), founder posts on r/ClaudeAI and HN (workstream 13), and the Anna case study. No fabricated reviews or mentions.

---

## Recommendation

Do the content fixes before any new SEO page: rewrite every machine-read description to the plan's positioning and delete the ten thin landing pages with redirects. Build seat billing and the trial checkout first; then ship `/pricing` with the schema above and the wedge pages (session sharing plus the six comparison pages). Measure AI visibility monthly.

Rejected: keeping the ten `[seoSlug]` pages and "thickening" them (they target autonomous-company and Telegram queries the wedge does not ask, and maintaining ten thin pages costs more than it earns); programmatic SEO at scale (scaled-content risk, no authority to carry it); buying an AI-visibility tool now (a monthly manual check of 20 queries is enough at our volume).

## Deliverable

Parts A, B and C above: the ranked audit, the schema plan with production JSON-LD for home and pricing, and the AI-search baseline with the definition to ship.

## Build tasks (Forge-ready)

Order: seat billing and the trial checkout come first. B1, B6 and any copy that quotes the price wait for them. B2, B3, B4, B7, B8 and B9 do not.

**B1 · Ship /pricing.** Today: 404; nav links `/#pricing`. Change: add `app/pricing/page.tsx` with the website brief's copy (workstream 10), `pageMetadata` title "Lightbulb pricing: $50 a month for your team", the pricing JSON-LD (Part B), add to `sitemap.ts`, point `Navbar.tsx`/`Footer.tsx` to `/pricing`. One plan, one price; no Base or Ultra line anywhere. Ships after seat billing and the trial checkout. Why: the most-asked buying question has no crawlable answer. Measure: `/pricing` indexed in Search Console within 14 days; visits → setup requests.

**B2 · One positioning source for metadata.** Today: root title "AI Agents for Autonomous Businesses"; WebSite and SoftwareApplication descriptions say "autonomous operations". Change: in `lib/seo.tsx` define `PRODUCT_DEFINITION` (Part C text) and `HOME_TITLE = "Lightbulb: your team and your AI employees, in one place"`; the homepage subhead reads "in your team's channels"; `app/layout.tsx` uses them as the default title/description with a `title.template` of `"%s · Lightbulb"`; replace the three homepage JSON-LD blocks with the `@graph` in Part B. Why: Google, AI engines and link previews read these first. Measure: Rich Results Test passes; title in SERP matches within 30 days.

**B3 · Correct the facts AI engines quote.** Today: llms.txt, docs FAQ and `get-started.md` state Ultra "2 vCPU / 8 GB", "AI-native company", three hand-written price lists; homepage cards list Telegram/Discord/WhatsApp. Change: generate every price line from one exported constant (the one plan's price; Base and Ultra are retired, so no `BASE_PRICE_USD` or `ULTRA_PRICE_USD` line survives); delete the compute-size claim; rewrite `docsFaq` answers to the plan's words; take Telegram, Discord and WhatsApp off the homepage now (it lists the workspace on web, desktop and phone, Claude Code and Codex sessions, email, Meet); they stay in product and docs if Forge verifies them. Add `/pricing.md` as a route beside `llms.txt`. Why: wrong facts get repeated verbatim. Measure: `grep` for "2 vCPU", "AI-native", "autonomous" in `apps/frontend/src` public pages returns zero.

**B4 · Retire the ten thin landing pages.** Today: ten orphaned `[seoSlug]` pages, each a separate SoftwareApplication. Change: delete `seoLandingPages` entries and `app/[seoSlug]`; add 308 redirects in `next.config.ts` (`/ai-agent-workspace` → `/`, `/ai-agent-marketplace` → `/marketplace`, `/ai-agents-for-slack` → `/compare/slack`, shipped in the same release as that page; the rest → the nearest real page, else `/`); drop them from sitemap and llms.txt. Why: thin, off-wedge, entity-splitting. Measure: Search Console excluded count falls; no 404s for those URLs.

**B5 · Session-sharing page.** Today: none; the query returns Lody and CLI tools. Change: `/claude-code-session-sharing`, H1 "Share a Claude Code or Codex session with your team", definition paragraph, three steps with screenshots, what a teammate can do (watch, join, pick up), FAQ, WebPage + FAQPage + BreadcrumbList, linked from home and `/docs`. Why: it is the wedge's most specific query and our clearest differentiator. Measure: impressions for "share claude code session" in Search Console; AI check monthly.

**B6 · Comparison pages.** Today: none. Change: `/compare/[slug]` for slack, microsoft-teams, lody, grok-bot and bloome (six pages, Leo signs off edits) using workstream 11's copy, after seat billing ships; each opens with a two-sentence answer, a table, a dated "last checked" line, links to competitors' own pages; WebPage + BreadcrumbList; footer "Compare" group. Why: "X vs Y" and "alternative" queries are where AI answers shortlist products. Measure: indexed; cited in at least 1 of 5 runs for "Slack alternative with AI agents" within 90 days.

**B7 · Metadata for app entry pages.** Today: `/sign-up`, `/sign-in`, `/join/*` inherit the old root title and have no canonical; sign-up H1 "Your AI company starts here." Change: add `metadata` (title "Create your team's workspace", canonical) to `sign-up/layout.tsx`, `sign-in/layout.tsx` (`robots: noindex`), `join/layout.tsx` (`noindex`). Sign-up headline is workstream 08/10's. Measure: parsed titles.

**B8 · Blog structured data and authorship.** Today: Article without `image`, organization author, OG image as logo, `paperclip` tag. Change: `BlogPosting` with `image`, `author` as a Person (Prasiddha for product and engineering, Leo for comparisons, Aden for customer stories; add `author` + `authorUrl` to front matter), `publisher: {"@id": "#organization"}`, `about: {"@id": "#software"}`, BreadcrumbList; Blog JSON-LD on `/blog`; remove internal-name tags; show "Updated" date when edited. Measure: Rich Results Test passes.

**B9 · Keep dev out of the index.** Today: `dev.thelightbulb.company` crawlable. Change: in `next.config.ts` `headers()`, send `X-Robots-Tag: noindex` when `VERCEL_ENV !== "production"` or host is not `thelightbulb.company`. Check first that the dev host does not share the production deployment (see the dev-host-as-Vercel-production-domain note); if it does, key on host, not env. Measure: `curl -I` shows the header on dev only.

**B10 · Entity cleanup.** Today: LinkedIn `sameAs` 404s. Change: Aden confirms (or creates) the Lightbulb LinkedIn page and Crunchbase/Product Hunt profiles, add them to `sameAs` only once they resolve; add the launch page H1 and one link to the product and `/request-setup` (`app/launch/page.tsx`). Measure: every `sameAs` URL returns 200.

**B11 · Measurement on.** Today: Vercel Analytics off; Search Console state unknown. Change (Prasiddha grants access to Search Console and Bing): verify the `thelightbulb.company` Domain property in Search Console and Bing Webmaster Tools, submit the sitemap, use Change of Address from the `flickmeon.ai` property if it exists; turn on Vercel Web Analytics or rely on PostHog per workstream 09. Measure: baseline indexed/excluded counts recorded in the Monday readout.

## Measures

- Indexed pages in Search Console (baseline after B11; target: every sitemap URL indexed, excluded count under 5).
- Organic and AI-referrer sessions to `/`, `/pricing`, wedge pages, tagged by source (feeds the Monday "visitors → setup requests / trial starts" line).
- Monthly AI check: the five queries plus "Lightbulb vs Slack", "Lightbulb pricing", run 3 times each on ChatGPT, Perplexity, Google; record mention rate ("2/9") and whether the description matches the definition.
- Setup requests and trial starts whose first page was a search or AI landing.

## Founder decisions (13 Oct 2026)

- A8 / E6: seat billing first, then `/pricing` with the plan's price. No interim line; comparison pages wait for billing.
- A9: Base and Ultra are retired fully. One plan, one price, including the JSON-LD offers.
- A1 / A4: no guarantee. Self-serve: 30 days free, no card. Founding teams: card on file at the setup session, first charge on day 31.
- E1: delete the ten thin `[seoSlug]` pages with redirects to the nearest real page.
- E2: `/ai-agents-for-slack` redirects to the Slack comparison page.
- E3: authors are Prasiddha (product, engineering), Leo (comparisons), Aden (customer stories).
- E4: Telegram, Discord and WhatsApp come off the homepage now; Forge verifies them for product and docs.
- E5: Aden confirms the LinkedIn page; Prasiddha grants Search Console and Bing.
- E7 / E8: Bloome gets its own page (six pages); Leo signs off comparison edits.
- B7: subhead becomes "in your team's channels".

## Sources

- Code: `origin/main` `6249f614d` — `apps/frontend/src/lib/seo.tsx`, `lib/domain.ts`, `app/layout.tsx`, `app/page.tsx`, `app/sitemap.ts`, `app/robots.ts`, `app/llms.txt/route.ts`, `app/[seoSlug]/page.tsx`, `app/blog/[slug]/page.tsx`, `app/docs/page.tsx`, `content/docs/index.ts`, `content/docs/pages/get-started.md`, `content/blog/posts/*`, `components/landing/{Navbar,Footer,PricingThreeCard}.tsx`; PR #3840 (`a461178bf`).
- Live fetches, 13 Oct 2026: thelightbulb.company `/`, `/docs`, `/docs/get-started`, `/blog`, `/blog/human-in-the-loop-ai-agent-approvals`, `/sign-up`, `/pricing`, `/request-setup`, `/ai-agent-workspace`, `/marketplace`, `/robots.txt`, `/sitemap.xml`, `/llms.txt`; flickmeon.ai root and paths; dev.thelightbulb.company; about.thelightbulb.company.
- Plan and context: `docs/business-plan.md` §6 and Go To Market "The website"; `.agents/product-marketing.md` v7.
- Google: [FAQPage docs](https://developers.google.com/search/docs/appearance/structured-data/faqpage), [Software app docs](https://developers.google.com/search/docs/appearance/structured-data/software-app).
- AI search results: [Inc42 TheLightbulb.ai](https://inc42.com/company/thelightbulb-ai/), [Companies House 02855611](https://find-and-update.company-information.service.gov.uk/company/02855611), [Slack blog](https://slack.com/blog/news/slack-is-where-agents-work), [Lody](https://lody.ai/blog/handoff-claude-code-session/), [Bloome](https://bloome.im/guides/claude-code-team), [Nimbalyst](https://nimbalyst.com/blog/claude-code-agent-teams-vs-parallel-sessions-vs-team-workspaces/), [Runbear](https://runbear.io/use-cases/Building-AI-Teammates-That-Actively-Join-Conversations).
