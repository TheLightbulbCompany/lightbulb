# Templates, kinds of app, design quality and personalization across the AI app builders

Research note, 29 Oct 2026. Tags: **[P]** primary (vendor docs, vendor GitHub, vendor product pages), **[S]** secondary, **[U]** unverified. Every [P]/[S] cites a numbered source at the end; every source was fetched on 2026-10-29 by web search, web fetch or GitHub raw/API. No browser.

This note does not repeat `2026-10-29-app-builder-workflows.md` (stages, publish, rollback, how bolt.diy and Stripe pick a template, the one-time-payment `starter-to-product.md`), `2026-10-28-small-software-stripe-projects-read.md` (the CLI and Provisioning API) or `2026-10-28-small-software-dyad-read.md` (Dyad's engine and licence). Where it must touch them it cites the earlier note.

## Short answer

There is no "generative software in general". Every builder surveyed collapses to **one default web starter** (React + Tailwind, usually shadcn) and then branches by **kind of request** in three places only: whether a database and sign-in are created, whether design questions are asked, and whether the result is a page or a record-managing app [P4][P24][P45][P30]. Templates are copies of finished projects (Lovable 228, Glide 540, v0 13 categories, Stripe 32 manifests), chosen by the person, never by the agent [P1][P52][P23][P30]. Personalization is done by **reading, not asking**: a URL, a screenshot, a spreadsheet, a Figma frame, or a saved design system; the only question every builder keeps is the product name [P9][P38][P40][P33]. Design quality comes from a design-system file the agent is checked against, not from prompting [P5][P22][P41]. Mobile is a separate project type that web projects cannot become; the honest default for a business app is a responsive web app on the home screen [P13][P19][P43]. Marketing sites are the one kind where a website tool beats an app builder on CMS and content workflow, though SSR has closed the SEO gap [P11][P48][P47].

## 1. Taxonomy: the kinds people ask for, and what builders do differently

The builders' own category lists converge. Lovable's gallery splits **Websites / Apps** with sub-categories Landing Page, Portfolio, Blog, SaaS, Internal Tools, Dashboards, Ecommerce, Project Management and 13 more [P1]. v0's gallery has 13: Apps & Games, Landing Pages, Dashboards, Components, Login & Sign Up, Blog & Portfolio, E-commerce, AI, Animations, Design Systems, Layouts, Website Templates, Agents [P23]. Stripe's guided flow has three: SaaS, One-time payment, Static site, else "Something else" [P30]. Softr's use cases: Portals, Business Operations, Knowledge Management, Dashboards & Reporting, CRM, Directories, Forms, Project Management [P50]. Replit's project types: web app, mobile app, slides, design, data visualization [P12]. Base44's: apps, websites, AI agents [P40].

What each builder changes per kind, from the docs:

| Kind | Starter / stack | Backend | Auth | Design defaults | What the person is asked |
|---|---|---|---|---|---|
| **Marketing site / landing page** | Same web starter (Lovable TanStack Start; Base44 web; Stripe `static_site` = Next.js + Vercel + PostHog, no DB) [P11][P41][P31] | None by default; Stripe's static prompt: "Keep the site lightweight and static unless the user requests dynamic features" [P34] | None | Lovable asks its 3 design questions **only** for "landing pages, portfolios, blogs, marketing sites, and other visually expressive apps" [P4] | Palette, type pair, layout (preselected, skippable) [P4]; Stripe: product name, audience, visual and color direction, references [P34] |
| **Internal tool / CRUD on a table** | Same starter; Retool generates "a React-based app consisting of components, themes, and functions that connect to your resources" [P54]; Appsmith "Generate New Page" from a datasource (2023) [S55] | Yes: Softr builds "Tables, fields, and relationships… plus sample data" [P49]; Glide Agent "will populate data for you that you can later replace" [P52] | Yes, with roles: Softr creates "User groups based on the roles you described, with permissions set per group" [P49] | Lovable skips design questions: "the app type has standard defaults (dashboards, admin panels, record-managing apps)" [P4] | Softr: "2-3 short multiple-choice questions" only "when it needs more information" [P49]; Retool: none, data source via `@` mention, else "the agent inspects available resource schemas and suggests a resource" [P56] |
| **Dashboard over data** | Same starter; Replit "data visualization" type [P12] | Reads a file or connector, no writes | Replit: "For private business or customer data, add login before sharing widely" [P17] | Replit prescribes summary cards, line/bar/stacked-area/scatter charts, filters updating every card, an empty state "not misleading zero values", an insights summary [P17] | Replit: none; "Replit summarizes the data first, so you can check the findings before anything is built" [P18] |
| **Form / intake** | Same starter; Lovable's only forms page is the Tally connector [P15] | A table per form (Base44: "create a new record in my Applications table") [P39] | None for the submitter | Standard | Where submissions go |
| **SaaS with auth + payments** | Stripe `nextjs_saas` (Next 16, Clerk, Neon, Stripe, Tailwind 4; no shadcn, no ORM) [P32]; Vercel Next.js SaaS Starter (Drizzle, shadcn, JWT auth, Stripe) [P27]; Open SaaS (Wasp, Prisma) [P35] | Yes | Yes | Stripe: landing page first "sets the visual anchor"; "avoid generic AI-starter styling" [P33] | Stripe: one question at a time, never invent the name, 2–4 palette options, approval before coding [P33] |
| **Customer portal** | Softr's top template category; Lovable Airtable example "Show each customer only their own orders in a portal" [P50][P39] | Yes | Per-user, scoped to own rows | Standard | Who the customers are, what they see |
| **Mobile app** | **Different project type**: Bolt "Say 'mobile app' in your very first prompt… Projects created for web do not easily switch over to mobile" [P13]; Replit "select Mobile app… if you skip this, Agent may build a web app instead" [P19]; Rork picks iPhone/Android/web before the first message [P43] | Replit next steps: login, App Storage, push, payments [P20] | Later | Expo default template = Expo Router tabs [P25] | Replit (plan mode): screens and navigation [P19] |
| **Automation / bot** | Not an app: GlideOS "Workflows… run on a schedule, from a webhook, or on demand" [P51]; Airtable Omni "leaves the automation turned off so you can review it first" [P57]; Replit "Build in background" [earlier note] | Shares the app's DB | n/a | n/a | Trigger and schedule |

Two rules fall out. First, **the kind decides whether a database and sign-in exist, not which stack** [P4][P49][P34]. Second, **only marketing sites get design questions**; everything else inherits a default system [P4].

## 2. Templates in detail

### Lovable
- **Count and curation.** `lovable.dev/templates` shows "All templates (228)", filterable by Category (Websites / Apps), 21 sub-categories and 14 personas, described as "Production-ready apps from the Lovable community" [P1]. The dashboard has a "Lovable templates" tab [P2].
- **Chosen by the person.** Each card offers Remix or Preview → Use template; templates require a security acknowledgement ("Acknowledge and remix") [P2].
- **What a remix carries.** Code, database **structure** ("tables and schema, not records"), and optionally chat history, Files and project instructions. Not carried: data, secrets, custom domains, publish state, collaborators, "Service connections, including Git sync and connectors"; the copy "gets new API keys" [P2].
- **Design templates (Business/Enterprise).** "a Lovable project that has been marked as reusable"; the copy includes "File structure", "Components and styles", "Configuration files", "Project setup and scaffolding"; one workspace default applied automatically; "Your prompt still controls what gets generated" [P3]. Design systems are the other half: "Templates suit initial project scaffolding, while design systems provide ongoing UI and design guidance" [P5].
- **Freshness.** Remixes are snapshots; nothing pins or updates [P2].

### Bolt (bolt.new and bolt.diy)
- **bolt.diy starters.** The 14 `STARTER_TEMPLATES` are GitHub repos under `blueberry-llm/*` with tags such as `website, app`, `mobile, expo`, `blog, performance` [P6]. The default `bolt-vite-react-ts-template` is 17 files: Vite, React, TS, Tailwind, ESLint, `.bolt/config.json` and `.bolt/prompt` [P7]. The Expo starter is 16 files with `app/(tabs)/_layout.tsx`, `app/(tabs)/index.tsx`, a `useFrameworkReady` hook and **no** `.bolt/prompt` [P8].
- **What `.bolt/prompt` carries** (injected as template instructions): designs "visually distinctive rather than generic", "fully featured and ready for production"; stack "JSX with Tailwind CSS, React hooks, and Lucide React icons"; no other UI/icon packages "unless they're truly necessary or requested"; "Logos should use Lucide React icons"; "Unsplash stock photos… Only use URLs you're confident exist" [P7a].
- **bolt.new.** Starting points: blank prompt, marketplace template ("Use template" makes an unlinked copy), uploaded files, a Figma frame, Google Stitch, GitHub or Lovable import, team template (Teams plan), design system "before sending your first prompt" [P9]. Team templates "can include files, configuration, and structure", nothing said about env or databases [P10].
- **How picked.** Person picks from the marketplace; bolt.diy's regex-and-default picker is in the earlier note.

### Replit
- **Templates are design mockups, not code.** The gallery at replit.com/design filters by "Landing Pages", "Mobile", "Prototypes" (also posters, social posts) [P14][P15]. "A template starts as a design mockup, not a working app"; Use template puts the frame on the Design canvas; you then switch to Build and ask Agent to "Preserve its visual design and make its navigation and contact actions work" [P14]. Building from a frame requires Core or Pro [P16]. Mockups "don't store data or connect to external services yet"; when you add a database, "Agent will prompt you to convert" [P16].
- **No code starter is documented** [U]; Agent "figures out the right setup automatically" from the first message [P12].

### v0 and Vercel
- **v0 templates**: 13 categories, "Duplicate a template", "from the community" [P23]. **Design systems** behave as starters: every design system saves "A small app… that every app you build with the design system later starts from", replacing "the default shadcn/ui and Tailwind setup" for new chats; a team owner can set a default [P22].
- **Vercel templates**: several hundred, each a Git repo plus a Deploy Button [P26]; the use-case filter (AI, Authentication, Blog, CMS, Ecommerce, Marketing Sites, Multi-Tenant Apps, Portfolio, SaaS, Starter, Web3…) comes from mirrored copies of the page, not the live one [S26a]. A template page lists the stack and its env vars: the Next.js SaaS Starter needs `AUTH_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BASE_URL`, `POSTGRES_URL`, and its deploy link is "pre-filled with the repository, environment variables, and Postgres integration" via the `products` parameter [P27]. The Supabase Starter's deploy flow "assigns the relevant environment variables to the Vercel project automatically" when the integration is installed [P28]. So a Vercel template carries code + env var **names** + a provisioning hook; the Deploy Button parameter reference itself did not fetch [U].
- **Vercel's CLI defaults** are the closest thing to an agreed baseline: `create-next-app` "recommended defaults - TypeScript, ESLint, No React Compiler, Tailwind CSS, No src/ directory, App Router, Cache Components, AGENTS.md" [P29]; `shadcn init` defaults to `--template=next --preset=nova`, templates next/vite/start/react-router/laravel/astro, base library base/radix/aria, presets as short codes, `apply --only theme|font` [P29a].

### Stripe projects-template-registry
- **32 manifests, no code** (earlier note). The tree: 6 `nextjs_saas` variants, 6 `nextjs_one_time_payment`, `simple_landing_page`, `simple_storefront`, `astro_portfolio_and_blog`, `nextjs_auth`, `nextjs_mcp`, 4 Customer.io, Daytona, E2B, 2 Kernel, HeyGen, Shopify, PostalForm, 2 TanStack/Netlify, `example` [P30a].
- **A manifest carries**: id, variant, `default`, guided category + framework, name/description/owner/tags, repo + pinned `ref`, `install_command`, `services`, `next_steps`, `tier_plans` [P30b]. Examples: `stripe/simple-landing-page` = `vercel/project` + `posthog/analytics`, tiers Hobby/Pro and Free/Pay-as-you-go [P31]; `posthog/astro-portfolio-blog` = PostHog + Algolia + Supabase + Netlify, with a next step `supabase db execute --file supabase/schema.sql` for the contact form [P31a]; `clerk/nextjs-auth` sits in category `auth`, which guided mode shows as "Something else" [P31b]; `nextjs_saas/crystal-peak` = amber-fox + Mixpanel [P31c]; `mixpanel/storefront` = Vercel + Mixpanel + Supabase [P31d]; `postalform/gym-cancellation-saas` = Cloudflare Workers + PostalForm, next step `codex "…Keep the existing Stripe Checkout, Cloudflare Workers, and PostalForm integrations working."` [P31e].
- **What the template repo carries.** `nextjs_saas_amber-fox`: routes `/`, `/dashboard`, `/checkout`, `/success`, `/cancel`, `/api/health`, `/api/billing-portal`, `/api/db`, `/api/webhooks/stripe`, `/auth/sync`; env `STRIPE_PRODUCT_ID`, `STRIPE_PRICE_ID`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `VERCEL_*`; scripts `setup:stripe`, `deploy` [P32]. Dependencies: Next 16, React 19, `@clerk/nextjs`, `@neondatabase/serverless`, `posthog-js`, `stripe`, Tailwind 4; **no shadcn, no ORM, no migrations** [P32a]. `AGENTS.md` plus `prompts/starter-to-product.md` carry the workflow [P33]; the static-site version audits only `app/page.tsx`, `layout.tsx`, `globals.css`, `scripts/deploy-vercel.mjs`, phases landing → content → "Keep the site lightweight and static" [P34]. `api_backend_iron-harbor` is "a local bare-bones template fixture for registry development" [P32b].
- **Freshness.** Pinned commits; a new ref is a registry PR [P30b].

### Dyad, Open SaaS, Base44
- **Dyad scaffold**: Vite + React 19 + Tailwind 3 + 28 Radix packages + react-router 6 + TanStack Query + recharts + react-hook-form + zod, `components.json`, `vercel.json`, `AI_RULES.md` [P36][P36a]. `AI_RULES.md` is the whole contract: routes in `src/App.tsx`, pages in `src/pages/`, components in `src/components/`, `Index.tsx` must show new components, "Use shadcn/ui components where possible… don't edit the shadcn files", Tailwind for all styling [P37].
- **Open SaaS (Wasp)**: `app/src/{admin,analytics,auth,client,demo-ai-app,file-upload,landing-page,payment,server,shared,user}`; auth with email + social, Stripe/Lemon Squeezy/Polar, SendGrid/Mailgun/SMTP, Plausible/GA, S3, Astro blog; recommended order: decide features → `main.wasp.ts` → look and style → analytics/admin → API keys → repo, deploy, domain [P35][P35a]. A human checklist, not an agent prompt.
- **Base44**: marketplace tabs Base44 / Community / Workspace, Type filter Apps or Websites; "backend functions and secrets do not transfer"; paid templates unlisted after 30 Sep 2026 [P38].

### What a template carries, compared

| | Code | Design tokens | Env var names | Provisioning manifest | Agent rules | Next-step prompt |
|---|---|---|---|---|---|---|
| Lovable remix | yes | in code | no (new keys, secrets dropped) [P2] | no | optional instructions [P2] | no |
| bolt.diy starter | yes | no | no | no | `.bolt/prompt` [P7a] | no |
| Bolt team template | yes | no | not stated [P10] | no | not stated | no |
| Replit design template | **no** (mockup) [P14] | the mockup | no | no | no | the Build prompt [P14] |
| v0 design system | starter app [P22] | yes, verified only [P22] | no | no | `SKILL.md` [P22] | no |
| Vercel template | yes | in code | yes [P27] | Deploy Button `products` [P27] | `AGENTS.md` if create-next-app [P29] | README |
| Stripe registry + repo | yes (pinned) | in code | `.env.example` [P32] | `services`, `tier_plans` [P30b] | `AGENTS.md` [P33] | `prompts/starter-to-product.md` [P33] |
| Dyad scaffold | yes | shadcn defaults | no | no | `AI_RULES.md` [P37] | no |
| Base44 template | app copy | theme | secrets dropped [P38] | no | workspace skills | no |

Only Stripe's shape carries all six, and it is the only one built for an agent rather than a person [P30b][P33].

## 3. Personalization: what they read, what they ask

**Read, not asked.**
- **A URL.** Lovable's website import rebuilds a page "with its fonts, images, colors, and spacing", up to 10 pages per request, forms keep their look but not their handling, logins and databases are not copied, no sync [P40]. Base44 can start "from a URL" with "Design only" or "Content & Design" [P38a]. Framer's agent "can rebuild a site from a live URL, screenshot, export, or the user's WordPress or Webflow site" [P45]. Lovable's Build-with-URL links accept `prompt`, `images` and `html` reference pages (10 references max) [P40a].
- **A screenshot.** v0 "analyze[s] the layout, colors, and components in the image" and "infers likely functionality based on visible UI elements" [P24]. Base44: "Upload a screenshot of a design you admire and ask the AI to match its style" [P38b]. Replit attaches selected frames "as snapshots so Agent has visual context" and "reads your markup" [P16a].
- **A spreadsheet.** Base44 reads attached CSV/XLSX "for their columns and sample data, so Base44 can build around your data" [P38a]. Glide Agent starts from a spreadsheet [P52]. Airtable Omni (6 Oct 2026) "works out how your data is structured and creates the necessary tables and fields" from up to 10 files of 10 MB [P57].
- **A Figma frame.** Lovable: plugin, MCP (desktop app, Dev seat) or `.fig` upload carrying "variable collections, colors, typography, and frame structure" [P6a]. v0: paste a link; reads variables and styles, auto layout, exact text, exports icons/logos/images [P24a]. Bolt: single frame via Anima [P9a].
- **A saved design system.** Lovable: npm/git/upload, PDFs and screenshots, "Lovable can crawl websites for context", releases `design-system.json` [P5]. v0 Design Systems 2.0: GitHub, npm/`.tgz`, Figma frames (screenshot + context), uploads, Storybook/docs links, "up to three reference sources", `logo.png` beside `SKILL.md`, and a starter app [P22]. Bolt: files, docs sites, GitHub, Storybook, npm → a generated Storybook; "documentation websites alone produce a theme rather than a true design system"; 45–60 minutes [P9b]. Base44: logo (or "Generate logo"), brand name, tagline, tone of voice, colors (or "Generate palette"), fonts, radius, shadow, spacing scale, components; "Base44 uses it to keep everything it generates on brand"; applying it to a built app "rebuilds the app to match, rather than restyling it in place" [P41].
- **Standing instructions.** Lovable Instructions (was Knowledge; renamed 27 Oct 2026): workspace + project, 10,000 characters each, plus `AGENTS.md`/`CLAUDE.md` "always read" [P42]. Replit: Memories (private, "do not retain personal or sensitive information"), Custom Instructions (always apply), Skills (when relevant) [P21]. v0: account-level instructions, example "Mobile first" [P24b]. Base44: AI Controls, e.g. "Prefer calm, minimal visuals with plenty of white space" [P38b].
- **Connected tools.** Base44: connected tools let it build "with your data and services in mind" from the first prompt [P38a]; Replit lists BigQuery, Linear, Slack, Notion [P12]; Lovable connectors: Airtable, Notion, HubSpot, Slack, Google Workspace [P15].

**Asked.**
- **Lovable**: one card, up to four questions, three of them design (palette, type pair, layout) with the first option preselected and "Skip all" [P4]; the chat card for scope is in the earlier Lovable note.
- **Base44**: "asks about it in the chat before it builds" only when the prompt leaves out "what the app does or who it's for"; one question at a time; options or free text; Skip; and **"Use a placeholder"** for details "only you know, such as your business name" [P38a]. Plan mode tracks four sections (Intent and goal, Audience and roles, Core flows, Design) and offers **four preview screens** of the main screen instead of asking for a style, each refinable up to 3 times [P38a].
- **Bolt**: Enhance prompt is manual; a "Help us enhance your prompt" dialog asks questions and returns an editable prompt; the example got "a more engaging homepage and more pages" [P9c]. With enhancement on, "Bolt can ask clarifying questions when it needs more information to get started" [P9].
- **Softr**: "2-3 short multiple-choice questions" only when needed; roles come from the prompt [P49].
- **Stripe**: the interview (name first, one question at a time, 2–4 palette and direction choices, approval) [P33].
- **Framer, Wix, Durable, Webflow**: a description. Wix: "Describe your vision", preferred style [P46]; Durable: "Get online in 30 seconds", industry paths [P46a]; Framer: "Describe the site, audience, or page you need" [P44a]; Webflow (launched 5 Feb 2026) generates up to five pages from one prompt on its Flowkit design system [S47].
- **Never asked**: stack, hosting, region (Lovable asks once), database engine. Nobody asks for a logo up front; Base44 offers to generate one [P41].

## 4. Design quality: how generic output is avoided

1. **A default system, enforced.** shadcn + Tailwind is the baseline for v0 ("Expect shadcn/ui components by default") [P24], Dyad [P37], Lovable's workspace example [P42], the Vercel SaaS starter [P27] and create-next-app/shadcn presets [P29a]. On top of it, Lovable scans "every generation… for violations of the design system" and retries; adherence checks "flag raw colors, one-off values, inline style overrides, and local copies of design system components" [P5]. Framer's agent "flags hardcoded colors outside the design system and replaces them with tokens" [P45]. v0 uses "only components and props it can verify from the sources" [P22].
2. **Structured taste questions instead of adjectives.** Lovable's palettes run "dark and moody" to "light and clean", type pairs are shown in their own fonts, layouts as wireframes (hero grid, sidebar, masonry, bento, dashboard), and "spacing, corner radius, and animation intensity" are derived from the picks [P4]. Base44 shows four rendered previews [P38a]. Stripe offers named directions: "polished B2B dashboard, premium editorial, playful consumer, developer tool, or bold futuristic launch page" [P33].
3. **Rules in the starter prompt.** bolt.diy: "visually distinctive rather than generic", Lucide for logos, Unsplash for photos [P7a]. Stripe: "Treat typography, color, motion, spacing, and layout as one system", "prefer local or system fonts unless font loading is confirmed", "Remove starter/template status content… from the product landing page" [P33].
4. **Pixel tools after generation.** v0 Design Mode edits typography, color, spacing, border, radius, shadow and text in a panel, Tailwind-aware, and Apply "generates an updated project version" [P24c]. Lovable's preview toolbar (select, inline text, draw, comment) and Replit's Visual Editor do the same [earlier note][P16].
5. **Critique loops.** Base44: "Ask the AI to critique your page. It takes a screenshot of your page and reviews what is actually there" [P38b]. Framer audits "low-contrast or small text, and inconsistent heading, button, and spacing styles" [P45].
6. **Real content.** Lovable: "use real content instead of placeholders"; "Design is a foundation, not a polish layer" [P43a]. Stripe: "Leave starter copy in place" is on the never list [P33].

What the docs do not say: none publishes an evaluation of design quality; the mechanisms above are the whole of it [U].

## 5. Data: "build this on my existing data"

- **File import (one-shot).** Base44 chat accepts CSV/XLSX/JSON and "can create entities and fields from your file, map columns to fields, and adjust field types"; dashboard import is CSV-only, "add new rows only"; "Imports never update existing records" [P39a]. GlideOS import "adds rows to the end of an existing table"; Google Sheets is pulled once, "GlideOS doesn't write back to Google Sheets" [P51a]. Replit attaches a CSV in chat and "summarizes the data first" [P18]. Airtable Omni infers tables from uploads [P57].
- **Live connectors (two-way).** Glide Classic: Glide Tables, Google Sheets, Excel, Airtable, Big Tables read+write; BigQuery read-only (Enterprise); SQL (Postgres, MySQL, SQL Server, Cloud SQL) read+write (Enterprise); "If the data source you connect is read and write, Glide and your data remain in two-way sync" [P53]. Lovable's Google Workspace connector: "Build a simple CRM that reads contacts from this Google Sheet and lets me add and edit rows", one shared Google account per connection [P15a]; Airtable connector discovers schema via the Metadata API, CRUD via the Record API, no webhooks so "apps must poll" [P39]. Retool: resources with Use/Edit/Own permissions, production/staging environments, HITL approval "Before the agent runs a function that adds, deletes, or changes data" [P56]. Replit: warehouse connectors BigQuery, Databricks, Snowflake, Microsoft Fabric [P15b].
- **Agent workflow for a CRUD app over a table, as the docs describe it.** Softr: infer tables and relationships from the domain → seed sample data → pages with navigation from native blocks → user groups with permissions → theme → edit in chat; multi-step form branching is manual [P49]. Retool: `@`-mention a resource or let the agent inspect schemas → prompt describes rows, filters, search, status labels, buttons, write-backs ("write back to a review_status column") and a "confirmation step" → Selection mode edits → approve function runs → Publish checklist [P54a]. Appsmith's non-AI generator does the same from a datasource in one click: "all the widgets you'd need to perform CRUD operations" [S55]. Glide: screens from components + data tables, then Data/Layout/Workflow editors; limits include "Importing large data files" and "Accessing external APIs" [P52].
- **Pattern.** Every builder copies the data into its own store and treats the original as a seed or a read-through; none makes the spreadsheet the system of record except Glide Classic [P53]. Lovable's "frontend-first" rule says why: "Start with sample data and get the screens and flows right", add the database "once information has to survive a refresh or be shared between users" [P43a].

## 6. The website case

- **Do app builders take website requests?** Yes, as a first-class kind: Lovable's gallery is split Websites / Apps [P1]; Base44 has "Build a website" and says it "builds it for you, including the design, pages, content, hosting"; "Simple sites may start as a single page" [P40]; Stripe has `static_site` [P30]; Replit's templates are mostly landing pages [P15].
- **What website tools do that app builders do not.** Framer's agent "creates pages, navigation, sections, typography, colors, images, and layout from a description or reference", creates CMS collections (Blog, Team Members, Jobs), imports from CSV/JSON/Notion/WordPress with hourly sync, runs `seo-check`, and has staging branches [P45][P44]. Framer's own comparison: "Collections, detail pages, metadata, and internal links are part of the website project"; "keeps metadata, headings, FAQs, and answer-ready copy in the same workflow used to edit the page" [P48]. Wix adds industry business tools: "automated scheduling, booking systems and lead generation forms" [P46]. Durable adds a CRM and payments [P46a]. Lovable's own guide concedes the split: "Lovable builds products people use", Framer builds "sites people look at"; "Framer is purpose-built for that workflow" for marketing sites and microsites [P47].
- **SEO is no longer the differentiator.** Lovable moved new apps to TanStack Start with SSR on 13 May 2026 ("Every request returns fully rendered HTML, for humans and crawlers"); older Vite apps get on-request pre-rendering for verified crawlers; `robots.txt` always, "not always a sitemap"; an SEO review with Try to fix, Search Console submission from chat, Semrush data [P11]. Base44 pre-renders for crawlers, generates `/sitemap.xml`, titles, canonical, OG and BreadcrumbList, but "does not set a custom meta description for each page by default", and free URLs "are not intended for long-term, production SEO" [P41a]. Replit recommends Static Deployments for content sites and sells an SEO Agent on paid plans [P11a]. A third-party comparison rates the Lovable+Vercel stack best on HTML quality and Core Web Vitals while also claiming "Google often sees nothing" for AI builders; its own table contradicts that, so [S] at best [S48a].
- **What is still missing in app builders**: a CMS (Lovable's import turns blog posts into "fixed content, not as manageable records" [P40]; Base44's website guide has no CMS or blog [P40]), form handling as a product (Lovable points at Tally [P15]), per-page metadata by default [P41a], and a content calendar/sync [P45].
- **Should "Apps" take website requests?** The evidence says take **landing pages and small brochure sites** (one to five pages, a form, a booking link) because the same starter renders them server-side and the customer already has the app [P11][P40][P34]; decline **content sites** (blog, docs, multilingual marketing, campaign microsites) with a pointer to Framer or Webflow, because the gap is the CMS and content workflow, which no app builder has closed [P45][P47]. Stripe draws the same line in its static template: static unless asked [P34].

## 7. Mobile

- **Everyone generates Expo, except Rork now.** Bolt: say "mobile app" first; "Bolt then uses Expo automatically"; preview by Expo Go QR ("The first time you do this, it will take some time to build"); stores via EAS CLI by hand: `eas init`, `eas build --platform ios --auto-submit`, "You can't change the slug after you create your first build" [P13]. Replit: Mobile app type; Expo Go QR or cloud iOS Simulator/Android Emulator; Launch "builds the native app in the cloud and submits it to App Store Connect for TestFlight"; bundle ID "cannot be changed after the first build"; "Publishing to Google Play is not yet supported"; push, IAP and Sign in with Apple need TestFlight, not Expo Go [P19][P19a]. a0.dev: QR for "the a0 app or Expo Go" (Expo Go v52 on Android), Convex backend, OTA updates, automatic App Store Connect setup; "Payment processing: the UI appears, but transactions fail" without a native build [P43b][P43c]. Rork: new projects are SwiftUI (iPhone) and Kotlin + Compose (Android) because "the agent wrote better Swift apps"; React Native "no longer does for new projects"; cloud simulators "use credits per minute"; install via Rork Max app [P43][P43a]. Expo's own templates: `default` = Expo Router + TypeScript multi-screen, plus `blank`, `blank-typescript`, `tabs`, `bare-minimum` [P25].
- **What it takes to get a phone app**: a separate project type chosen before the first prompt [P13][P19]; an Apple Developer Program membership with 2FA [P19]; an immutable slug/bundle ID [P13][P19]; TestFlight review for the first build [P19]; a native build for payments [P43b]; Google Play still manual or unsupported [P13][P19a].
- **The PWA/responsive default.** Base44 does not build native: apps "automatically adjust[] [their] layout for any device", a `manifest.json` is generated, users "Add to Home Screen", store builds are "a secure web view" wrapper with generated IPA/AAB, and push "only reach[es] people who installed your app from the App Store or Google Play" [P42a]. Lovable ships no native output; secondary sources describe PWA or Capacitor wrapping [S43d]. Base44's design systems are "Web apps only, not mobile apps" [P41].
- **Read**: for a business app in a room, a responsive web app with a home-screen icon is the honest default; a phone app is a second starter and a second project, never a conversion [P13][P42a].

## A starter and workflow catalogue for Lightbulb

Read against `docs/superpowers/specs/2026-10-28-apps-design.md` (one starter: Next.js, Tailwind, shadcn, Drizzle, Neon, Vercel, `lib/door.ts`, `AGENTS.md`).

### Starters: one now, one later, none for websites

1. **`app` (the only starter on day one).** Next.js App Router + Tailwind + shadcn (the shared baseline [P24][P29a][P37]) + Drizzle migrations + Neon + the sign-in door. Services as flags in the manifest, Stripe-style: `neon/postgres` on by default, `stripe` and email off until a kind needs them [P30b]. Carry all six things Stripe's shape carries: pinned code, tokens in `globals.css` as shadcn variables [P22a], `.env.example` names [P32], `services` + `tier_plans` [P30b], `AGENTS.md` [P33], `prompts/starter-to-product.md` [P33]. Add bolt.diy's two lines to `AGENTS.md`: Lucide for logos, no new UI/icon packages unless asked [P7a]. Add Dyad's file contract: pages in one folder, components in one folder, never edit shadcn files [P37].
2. **`phone` (later, only on demand).** Expo `default` template (Router tabs) [P25], its own project, chosen before the first message, never converted from `app` [P13][P19]. Do not build it until a room asks twice.
3. **No `site` starter.** A landing page is a page in `app` rendered server-side [P11]; content sites are declined with a pointer (§6).

### The fixed workflow per kind

Common to all kinds, borrowed: the request is the authorization and the preview link is the question (bolt.diy, earlier note); at most one question before the preview, and it is the one Stripe keeps: the name, never invented [P33]; "Use a placeholder" as the answer when the person is not there (Base44) [P38a]; read the room, the workspace brand and any URL or file before asking anything (Lovable import, Base44 attachments) [P40][P38a]; frontend first with sample data, database when data must persist, sign-in when data is private (Lovable) [P43a]; one design system file, violations retried (Lovable, Framer) [P5][P45]; verify `/api/health` and the main flow before posting (Stripe) [P33].

| Kind | Builds first | Asks (≤1) | Never asks | Provisions | Done when | Shows |
|---|---|---|---|---|---|---|
| **Landing page / brochure site** | The page itself, from the workspace brand or the company's existing URL (Lovable import reads fonts, colors, spacing [P40]) | Palette/type/layout card with the first option preselected, only if no brand exists (Lovable) [P4] | Stack, hosting, SEO settings (set titles, OG, `robots.txt`, sitemap by default [P11][P41a]) | Vercel only; no DB unless a form exists (Stripe `static_site`) [P31][P34] | Build passes, every link works, phone width checked (Replit's visitor test) [P14] | Preview link + a screenshot card |
| **Form / intake** | The form and the table behind it, submissions page gated by the door | Where submissions go (a room thread, an email, a sheet) | Field types (infer from the request) | Neon table; email only if asked [P30b] | A test submission appears in the gated list (Lovable: "refresh after each save") [P43a] | Preview + the public form URL |
| **Internal tool / CRUD on a table** | If a file or sheet was attached: import it, infer columns and types, seed from it (Base44, Omni) [P39a][P57]; else sample data (Softr, Glide) [P49][P52]. Then list → detail → edit, filters, search, status labels (Retool's prompt shape) [P54a] | Who edits vs who reads (Softr's role groups) [P49] | Design questions (Lovable: record-managing apps have standard defaults) [P4] | Neon; door with roles | Two accounts cannot see each other's rows (Lovable's two-tab, two-account test) [P43a]; writes behind a confirm step for destructive actions (Retool HITL) [P56] | Preview + the imported row count and the columns it guessed |
| **Dashboard over data** | Summarize the data first and post the summary (Replit) [P18]; then summary cards, trend/comparison/parts charts, filters that update everything, empty states, an insights note that says how each metric is calculated [P17] | The decision the dashboard supports [P17] | Chart types | Neon if the data is a file; a read-only connection if it is a database (Glide/GlideOS read-only connections) [P53][P51a] | One figure checked against the source [P18]; door on if the data is private [P17] | Preview + "show the calculation" for one number |
| **Customer portal** | The customer's view of their own rows, scoped by the door (Lovable Airtable example) [P39] | Which customers (a list, a sheet, an existing table) | Branding (reuse the workspace's) | Neon; door with a customer role; email for invites | A customer account sees only its rows | Preview as a customer + the invite link |
| **SaaS with auth + payments** | Landing page first ("sets the visual anchor"), then dashboard/sign-in/checkout, then the business logic (Stripe's phases) [P33] | The offer (one price, one plan) | Stripe object setup at runtime (durable objects via a setup script only) [P33] | Neon, door, Stripe via `scripts/setup-stripe` [P32]; sandbox in preview, live on publish (Lovable payments, earlier note) | `/api/health` true, a test checkout completes, webhook 200 [P33] | Preview + the test card |
| **Mobile app** | Not in `app`. Offer the responsive app with Add to Home Screen (Base44) [P42a]; if the room insists, open a `phone` project | Apple account exists? [P19] | Nothing else before the Expo Go QR [P13][P19] | Expo project; EAS later | Expo Go QR loads on a phone | QR + the list of what needs TestFlight (push, IAP, Apple sign-in) [P19a] |
| **Automation / bot** | A scheduled or webhook function in the same app, **off by default** (Omni) [P57] | Trigger and schedule | n/a | Nothing new; reuse the app's DB | A dry run posts what it would have done | The dry-run output + a one-tap "turn on" |

### What this means for the spec

- Keep the one starter; make `services` the only switch (§2 Stripe) [P30b]. The agent never picks a template; the kind picks the flags.
- Put the per-kind rows above into `prompts/starter-to-product.md` as named phases, one per kind, so the same file runs for Forge and for a laptop Claude Code user [P33].
- Add a `design/` folder to the starter with `tokens.css` (shadcn variables) and a `DESIGN.md` the room's brand fills in; scan generations against it and retry (Lovable, Framer) [P5][P45][P22a].
- Personalize by reading: the workspace name and logo, any URL in the request, any attached file. Ask for nothing except the name, and accept a placeholder [P38a][P40].
- Websites: take one-to-five-page sites, decline CMS sites with a sentence (§6). Mobile: decline conversions, offer the home-screen app, open `phone` only on a second ask (§7).

## Sources (all fetched 2026-10-29)

- P1 https://lovable.dev/templates
- P2 https://docs.lovable.dev/features/projects/remix.md
- P3 https://docs.lovable.dev/features/business/design-templates.md
- P4 https://docs.lovable.dev/features/design-guidance.md
- P5 https://docs.lovable.dev/features/design-systems.md
- P6 https://raw.githubusercontent.com/stackblitz-labs/bolt.diy/main/app/utils/constants.ts
- P6a https://docs.lovable.dev/integrations/figma.md
- P7 https://api.github.com/repos/blueberry-llm/bolt-vite-react-ts-template/git/trees/main?recursive=1
- P7a https://raw.githubusercontent.com/blueberry-llm/bolt-vite-react-ts-template/main/.bolt/prompt
- P8 https://api.github.com/repos/blueberry-llm/bolt-expo-template/git/trees/main?recursive=1 (no `.bolt/prompt`; raw fetch 404)
- P9 https://support.bolt.new/building/start-project.md
- P9a https://support.bolt.new/integrations/figma.md
- P9b https://support.bolt.new/design-system/add-design-system.md
- P9c https://support.bolt.new/prompting/prompting-effectively.md
- P10 https://support.bolt.new/teams/team-templates.md
- P11 https://docs.lovable.dev/features/seo-aeo.md
- P11a https://docs.replit.com/build/improve-seo.md
- P12 https://docs.replit.com/replitai/agent
- P13 https://support.bolt.new/integrations/expo.md
- P14 https://docs.replit.com/build/build-and-publish-from-template.md
- P15 https://docs.lovable.dev/llms.txt (page index: connectors, Tally, SEO)
- P15a https://docs.lovable.dev/integrations/google-workspace.md
- P15b https://docs.replit.com/llms.txt (page index: warehouse connectors, templates, mobile)
- P16 https://docs.replit.com/design/design-vs-build.md
- P16a https://docs.replit.com/design/canvas.md
- P17 https://docs.replit.com/build/dashboard.md
- P18 https://docs.replit.com/use-cases/spreadsheet-to-team-dashboard.md
- P19 https://docs.replit.com/build/mobile-app.md
- P19a https://docs.replit.com/build/mobile-preview-testing.md
- P20 https://docs.replit.com/build/mobile-first-app.md
- P21 https://docs.replit.com/chat/memories-custom-instructions-and-skills.md
- P22 https://v0.app/docs/design-systems-2
- P22a https://v0.app/docs/design-systems (legacy registry: `tokens.css`, `next/font`, `registry.json`)
- P23 https://v0.app/templates
- P24 https://v0.app/docs/screenshots
- P24a https://v0.app/docs/figma
- P24b https://v0.app/docs/instructions
- P24c https://v0.app/docs/design-mode
- P25 https://docs.expo.dev/more/create-expo/
- P26 https://vercel.com/templates
- S26a Use-case filter list from mirrored copies of the templates page (web search, 2026-10-29); live page did not render filters
- P27 https://vercel.com/templates/next.js/next-js-saas-starter
- P28 https://vercel.com/templates/next.js/supabase
- P29 https://nextjs.org/docs/app/api-reference/cli/create-next-app
- P29a https://ui.shadcn.com/docs/cli
- P30 https://raw.githubusercontent.com/stripe/projects-template-registry/main/guided.yaml
- P30a https://api.github.com/repos/stripe/projects-template-registry/git/trees/main?recursive=1
- P30b https://raw.githubusercontent.com/stripe/projects-template-registry/main/example/example.yaml and README.md
- P31 https://raw.githubusercontent.com/stripe/projects-template-registry/main/simple_landing_page/clear-sky.yaml
- P31a …/astro_portfolio_and_blog/cobalt-hedgehog.yaml
- P31b …/nextjs_auth/quickstart.yaml
- P31c …/nextjs_saas/crystal-peak.yaml
- P31d …/simple_storefront/purple-wave.yaml
- P31e …/gym_cancellation_saas/cloudflare-postalform.yaml
- P32 https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_saas_amber-fox/README.md
- P32a https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_saas_amber-fox/package.json
- P32b https://raw.githubusercontent.com/stripe/projects-templates/main/api_backend_iron-harbor/README.md
- P33 https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_saas_amber-fox/AGENTS.md and prompts/starter-to-product.md
- P34 https://raw.githubusercontent.com/stripe/projects-templates/main/static_site_clear-sky/prompts/starter-to-product.md (tree via api.github.com/repos/stripe/projects-templates/contents/static_site_clear-sky)
- P35 https://docs.opensaas.sh/
- P35a https://docs.opensaas.sh/start/guided-tour/
- P36 https://api.github.com/repos/dyad-sh/dyad/contents/scaffold and /scaffold/src
- P36a https://raw.githubusercontent.com/dyad-sh/dyad/main/scaffold/package.json
- P37 https://raw.githubusercontent.com/dyad-sh/dyad/main/scaffold/AI_RULES.md
- P38 https://docs.base44.com/Getting-Started/App-templates.md
- P38a https://docs.base44.com/Getting-Started/starting-from-your-first-prompt.md
- P38b https://docs.base44.com/Building-your-app/Designing-with-AI.md
- P39 https://docs.lovable.dev/integrations/airtable.md
- P39a https://docs.base44.com/Building-your-app/Managing-your-app-data.md
- P40 https://docs.lovable.dev/features/projects/import-website.md; https://docs.base44.com/Getting-Started/building-a-website.md; https://docs.base44.com/index.md
- P40a https://docs.lovable.dev/integrations/build-with-url.md
- P41 https://docs.base44.com/Building-your-app/Design-system.md
- P41a https://docs.base44.com/Performance-and-SEO/SEO-and-search-visibility.md
- P42 https://docs.lovable.dev/features/knowledge (Instructions)
- P42a https://docs.base44.com/Building-your-app/Mobile-experience.md
- P43 https://docs.rork.com/introduction/build-your-first-app.md
- P43a https://docs.rork.com/swift-vs-react-native/whats-the-difference.md
- P43b https://docs.a0.dev/development/testing/mobile-app-testing.md
- P43c https://docs.a0.dev/llms.txt
- S43d Lovable mobile output: web search 2026-10-29 (rapidevelopers.com, rork.com/guides, choicely.com); no Lovable primary page found
- P43a https://docs.lovable.dev/tips-tricks/from-idea-to-app.md (cited as P43a in §4–5 and the catalogue)
- P44 https://www.framer.com/ai/
- P44a https://www.framer.com/solutions/ai-website-builder/
- P45 https://www.framer.com/agents/?md
- P45a https://www.framer.com/marketplace/templates/?md
- P46 https://www.wix.com/ai-website-builder
- P46a https://durable.com/
- P47 https://lovable.dev/guides/framer-vs-lovable-app-builder-comparison
- S47 Webflow AI site builder: web search 2026-10-29 (webflow.com/blog/ai-site-builder, help.webflow.com article 38840145286035); direct fetches returned 403 / header overflow
- P48 https://framer.com/compare/framer-vs-lovable (vendor comparison)
- S48a https://till-freitag.com/en/blog/ai-website-builder-comparison
- P49 https://docs.softr.io/start-here/ai-co-builder.md
- P50 https://www.softr.io/templates; https://docs.softr.io/start-here ("90+ templates")
- P51 https://www.glideapps.com/docs/os/what-is-glide-os.md
- P51a https://www.glideapps.com/docs/os/data.md
- P52 https://www.glideapps.com/docs/getting-started/agent.md; https://www.glideapps.com/templates (540 listed)
- P53 https://www.glideapps.com/docs/essentials/data-sources
- P54 https://docs.retool.com/build/apps
- P54a https://docs.retool.com/build/apps/quickstart
- S55 https://dev.to/appsmith/automatically-generate-a-crud-app-from-any-database-in-just-a-few-clicks-4m1a (Appsmith, 23 Nov 2023; no current docs page found)
- P56 https://docs.retool.com/build/apps/concepts/data
- P57 https://community.airtable.com/announcements-6/new-a-major-upgrade-to-omni-48793 (Airtable, 6 Oct 2026)
- Earlier notes referenced: `2026-10-29-app-builder-workflows.md`, `2026-10-29-how-lovable-builds.md`, `2026-10-28-small-software-stripe-projects-read.md`, `2026-10-28-small-software-dyad-read.md`.
