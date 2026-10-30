# Content sites like Webflow and Framer: what makes them good, and how an agent-built Next.js app matches them

Date: 2026-10-29. Question: what exactly makes Webflow, Framer, Wix Studio and Squarespace good for content sites, and how can a room agent build a Next.js app on the customer's own Vercel + Neon (via Stripe Projects) so the company's non-technical people keep editing content without the agent?

Context: the founder has ruled that nothing is declined, including content-heavy sites. That overturns the "decline content sites" line in `docs/research/2026-10-29-templates-kinds-of-apps.md` §6. This note does not repeat §6 (app builders' SEO, the Lovable/Base44 website gap, Framer's agent from the June release). It covers the mechanics underneath.

**Labels.** [P] means a primary source (the vendor's own docs, code or pricing page) that I fetched. [S] means a secondary source, or a primary page I could only read through a search-engine extract. `help.webflow.com` returns 403 and `webflow.com` fails with "header overflow" for WebFetch, so most Webflow product claims are [S] even when the page is Webflow's own. [U] means my inference or something I could not verify. Every source was fetched or searched on 2026-10-29. URLs are in the Sources list at the end.

## Short answer

1. **The visual canvas is not what makes these tools good for content sites. The binding is.** Collections are bound to a template page, fields are bound to elements, and SEO fields are bound to items. Editors change words and images on the live page while layout and styles stay locked. A reviewer then publishes to a CDN with one button. Webflow and Framer both work this way [P5][P25][S26][P21].
2. **Most of the content part already exists in open source, inside a Next.js app on Postgres.** Payload 3 runs "inside your `/app` folder" on Neon (MIT licence) [P36][P39][P41]. It covers collections, drafts, autosave, scheduled publish, field-level localization, access control, form builder, redirects, search, SEO fields and side-by-side live preview [P37][P38][P40][P42][P44].
3. **The hard part is on-page, click-to-edit editing with the design locked, done by a non-designer on the live site.** Framer's On-Page Editing and Webflow's Edit mode do this [P5][S26]. In the Vercel world it costs money: Vercel Content Link needs Pro or Enterprise, and Payload's Content Link plugin is "enterprise-only" with a sales-issued key [P47][P45]. A small "Edit" overlay in our starter covers 80% of it [U].
4. **What still beats us:** freeform layout editing by humans (moving sections, restyling breakpoints), authoring interactions, and analytics plus A/B testing in one product. The agent covers the first two on request. PostHog in the Stripe catalog covers the third [P2].
5. **The reverse route is real.** An external agent can drive Webflow (MCP with Designer and Data tools) and Framer (MCP/External Agents, which work on a branch) [P19][P7][P8]. Neither Webflow nor Framer is in the Stripe Projects catalog. **Wix Headless** (`wix/headless`, which includes CMS and a client-run dashboard) and **WordPress.com** (`wordpress.com/site`) are [P1][P2][P3].
6. **Recommendation:** build content sites as the same `app` starter with a `content` service flag. The flag adds Payload with the website-template collections, sign-in through `lib/door.ts`, live preview, an Edit overlay and SEO defaults. Content edits by the agent go through the CMS as drafts, never as code. If the company already runs Webflow or Framer, the agent edits it there through their MCP instead of migrating it.

---

## 1. Webflow and Framer mechanics (and the parts of Wix Studio and Squarespace that matter)

### 1.1 The CMS model

**Webflow.**
- *Collections and fields.* A Collection is a typed table, and its fields include reference and multi-reference fields. Each Collection gets one **Collection template page**: a single design that renders every item at its own URL. Collection Lists place filtered items on any page [S29].
- *Limits.* Third-party summaries give 2,000 items on the CMS plan and 10,000 on Business (some say up to 20,000), 20 or 40 collections, and 30 fields per collection. Webflow's own help says nested collection lists show "up to 10 items" on non-Enterprise plans [S29]. The pricing page did not load for me, so these numbers are [S].
- *Data API.* Items are "staged" or "live". `isDraft` defaults to `true`, and status comes from `lastPublished` + `isDraft`. Webflow describes scheduled content as "scheduled for future publication. This can not be controlled by the CMS API" [P21][P22]. You publish one item with `publishItem`, update and publish in one call with `updateItemLive`, or publish everything with `sites.publish`. Site publish is rate-limited to "one successful publish per minute" [P21][P23].
- *Localization* shares the CMS item cap across locales and is priced per locale [S32].

**Framer.**
- *Collections.* Framer has the same shape: collections, reference and multi-reference fields, a CMS **detail page** per collection ("one reusable layout for individual items"), collection lists with filters, and "up to 100K items" [P13][S14].
- *Plan limits* [P4]:

  | Plan | CMS collections | CMS items |
  |---|---|---|
  | Basic ($10/mo) | 2 | 1,000 |
  | Pro ($30/mo) | 10 | 2,500 |
  | Pro add-ons | up to 40 | up to 40,000 |

- *Plugin-managed collections.* A plugin can own a collection outright: "Fields and items can only be added, edited and deleted by the plugin and not the user". This is how Notion, Airtable and other syncs work [S14].

**Wix Studio.**
- A **dynamic item page** "generates a unique page for each collection item", with up to 64 dynamic pages per collection. Page variants can give different layouts to subsets of items [S34].
- CMS roles have per-collection view/add/update/delete permissions. Some permissions (Edit Site, Edit Code) "automatically make the role an 'admin' role" [S34].

**Squarespace.** Its CMS is mostly the blog. Blog posts still use the classic editor ("It's not possible to use Fluid Engine on blog posts"). Posts can be scheduled from the Publish menu [S35].

**What to copy:** one template per collection, and fields bound to elements. That is a route `app/blog/[slug]/page.tsx` reading one collection. Next.js already has this pattern. The CMS only has to provide the typed collections.

### 1.2 Editing by non-designers

**Webflow Edit mode.**
- *Who it is for.* The Content Editor role is for "copywriters, product marketers, and content strategists". It can "edit site content (e.g., copy, assets, and videos) … using a simplified interface that ensures the site design stays untouched" [S26].
- *What editors can do.* Webflow University lists: "Edit site content, including text, images, and links"; "Add, edit, and publish CMS items" and "Publish pages" (each "if permissions are enabled"); "make updates directly on the canvas while layout and styling stay protected" [P25].
- *What editors cannot do.* "Build pages using page templates or components", "Make changes to the site's styles or structure", "Create new or modify existing CMS Collections", "Edit custom code or site-wide settings" [P25].
- *How editors get in.* Add `/?edit` to the live URL, or use the "Edit site" button [S26].
- *Publish permissions.* "Can publish" is a toggle, and restricting it requires Growth, Agency or Enterprise workspaces. Since 24 Feb 2026, editors can change unbound text, links and images inside components [S26].

**Framer On-Page Editing.**
- *Who and where.* "Site owners and collaborators with **Content** permissions can use on-page editing by opening the published site in their browser" [P5].
- *What is editable.* Text, rich text, images, component properties, static-page content and CMS content [P5].
- *What is not.* "Layout templates and overlay content are not supported" [P5].
- *Review before live.* "Finish Editing" emails the collaborators who can publish, and they review and publish [P5].
- *New items and hidden fields.* "Add Page" on a CMS detail page creates a new item in place. "Show All Fields" opens SEO descriptions and toggles that do not appear on the page [P5].
- *Later additions.* Version 2.0 added localized editing. Drag-to-reorder is reported but unconfirmed [S15].
- *Seats and branching.* Pro includes 10 seats, with extra editors at $20 each [P4]. Since Framer 3.0 (16 June 2026), every change by an agent or a teammate can go on a **branch** that is reviewed, compared, merged and published [P6].

**Wix Studio and Squarespace.** Both use contributor roles. Wix's blog app has "Blog Editor, Blog Writer, or Guest Writer" roles [S34]. A Squarespace contributor "can only create, edit or publish content", without settings or billing [S35].

**The pattern across all four:** the editor works on the real page or a mirror of it; the design is locked by role; drafts are reviewed and then published; adding a CMS item happens from the page. Nobody gets a blank form detached from the page as their only tool.

### 1.3 SEO controls

**Webflow.** The MCP server's "Manage" tools list "Create pages, bulk-update page settings, manage schema markup, control sitemap inclusion, and update SEO meta tags" [P19]. That implies per-page meta and OG fields, schema markup per page, and a generated sitemap with per-page inclusion. 301 redirects live in site settings. 302s are a known gap, raised on the wishlist [S33].

**Framer.** Framer auto-generates `sitemap.xml` and a `robots.txt` that links to it. One mid-2026 guide says there is "no in-editor UI for inspecting or editing either file". Structured data is pasted JSON-LD in each page's or CMS item's SEO custom-code box [S16]. Framer's agents generate "SEO metadata" and audit for broken links [P6]. External agents handle redirects ("make sure every URL in a file has a redirect") [P7].

**Read:** SEO is a set of fields per page and per item, with defaults bound to collection fields (the title from `name`, the OG image from `cover`), plus a sitemap and a redirect table. None of it needs a canvas.

### 1.4 Forms, site search, interactions, hosting, analytics, A/B

- **Forms.**
  - Framer forms send to email, Slack, Zapier or a webhook, with "built-in spam protection by default" [S15].
  - Webflow includes spam filtering with hosting [S33].
  - In Webflow's code export, forms and site search are disabled [S28]. They are hosted services, not code.
- **Site search.** Webflow has a native site search. I found no primary doc on it [U]. Framer search is a component [U].
- **Interactions.**
  - Webflow bought GSAP and made it free in 2025. Its new interactions engine "Interactions with GSAP (IX3)" supports timelines, ScrollTrigger and SplitText [S30].
  - The MCP server "cannot yet create or apply" Classic Interactions [P19].
  - Framer animations are part of its React runtime [S17].
- **Hosting and speed.**
  - Webflow says "AWS infrastructure with Cloudflare's global CDN"; a forum post says Fastly, so the CDN is unconfirmed [S33].
  - Framer pre-renders HTML and then calls `hydrateRoot()` with a React bundle [S17]. Framer reports hydration work "up to 150% faster" for CMS sites [S17]. Vendors selling static rebuilds claim Framer mobile PageSpeed around 58 versus 96 for static, with no published method [S17].
  - Framer bandwidth: 1 GB (Free), 50 GB (Basic), 100 GB (Pro) [P4].
- **Analytics and A/B.**
  - Framer: analytics history of 30 days (Basic) or 90 days (Pro). A/B testing is a Pro add-on at "$50 per 500,000 events, up to 5 A/B tests" [P4].
  - Webflow: Optimize "combines traditional A/B testing, rules-based personalization, and AI". Third-party pricing gives $299–$1,199/month [S31]. Analyze is a paid add-on [S31].

### 1.5 AI features

- **Webflow AI site builder** (5 Feb 2026). Describe the site, it proposes pages and sections, you generate, then refine in the Designer. Up to five pages per generation. New sites only, not existing projects. Output is built on Webflow's Flowkit CSS framework [S27]. Reviewers say the copy and brand fit still need work [S27].
- **Framer AI** (May 2025). **Wireframer** generates "a responsive page with structure and starter content". **Workshop** "creates React components matching your site's style" [S18].
- **Framer 3.0 Agents** (16 June 2026) "see and edit the same canvas, components, CMS content, styles, SEO settings" and the publishing workflow. They generate pages, handle breakpoints, organize styles, write code components, manage CMS and audit sites [P6].
- **Brand extraction.** I found no primary doc for "extract brand from URL" in either product. §6 of the templates note covers Lovable's import, which reads fonts, colours and spacing [U].

### 1.6 Import and migration

- **Webflow export** downloads a ZIP of HTML/CSS/JS. It excludes CMS content (collection lists export as empty states), localized content beyond the primary locale, code components, logic, forms and search. It needs a paid workspace [S28].
- **Framer** imports CSV, JSON, Notion and WordPress with hourly sync (see §6 of the templates note). Plugin-managed collections are how syncs stay authoritative [S14].
- **Migration into our stack.** Migrating out of either tool means scraping the rendered pages plus pulling CMS items through the Data API (Webflow) or the Server API (Framer) [P20][P9]. Any agent can script that [U].

### 1.7 APIs and whether an external agent can drive them

**Webflow** has three surfaces:
- *Data API (REST).* Sites, pages, CMS, assets, forms, publishing, at 60–120 requests per minute by plan [P20][P23].
- *Designer API.* Used through the MCP server. Since September 2025 the MCP server covers elements, styles, variables, components, "page branches (Enterprise plan)", responsive layouts and GSAP-based interactions [P19].
- *MCP constraints* [P19]:
  - Some actions "require the Webflow Designer open with the Bridge App connected": snapshots, image uploads, and the current selection, page or breakpoint.
  - "You can't create new website projects through MCP".
  - The server "can't create new localized CMS items, but it can update existing ones".
  - Classic interactions are not supported.
  - Each authorization covers one workspace.
- *Webflow Cloud* hosts Next.js, Astro or Vite apps "alongside a Webflow site" at a path like `my-site.webflow.io/my-app`, with SQLite, KV and object storage [P24]. This is Webflow's own answer to "the site is in Webflow and the app is code".

**Framer** has four surfaces:
- *Plugin API.* Runs inside the editor [P12].
- *Server API (`framer-api`).* "Update and publish your Framer projects with a simple script from any server at any time". It uses one API key per project ("bound to a specific project") and "shares most methods" with the Plugin API. It adds `publish()` (a preview), `deploy(id)` (production), `getChangedPaths()` and `getChangeContributors()` [P9][P10][P11].
- *Framer MCP.* Now recommended. Agents get "full access to your canvas, components, CMS, and more" [P8].
- *External Agents* (`npx @framer/agent setup`). They "can read and write CMS collections", do localization (which "Framer MCP does not yet support") and redirects [P7]. They cannot change project names or domains, "assign overrides to nodes", or "access analytics data" [P7]. "Every change an external agent makes goes on a branch" [P8].

**Verdict:** yes, an external agent (Forge) can build and edit inside a customer's Webflow or Framer. Both need a human to create the project or site and authorize once. Webflow's canvas tools need the Designer tab open.

---

## 2. What is hard to replicate and what open source already gives

### 2.1 Feature by feature

| Feature | Webflow / Framer | Next.js + embedded CMS on Vercel + Neon | Effort for us |
|---|---|---|---|
| Typed collections, references, template per collection | Yes [S29][P13] | Payload collections + a Next route per collection [P36][P40] | Done in template |
| Drafts, autosave, versions | Yes [P21][P5] | Payload `versions.drafts`, autosave [P38] | Done |
| Scheduled publish | Yes; Webflow's not via API [P21] | Payload `schedulePublish` needs a job runner; **Vercel Hobby cron is once a day ±59 min**, Pro once a minute [P38][P48] | Needs Pro or an external tick |
| Roles (editor cannot touch design) | Content Editor / Content permission [P25][P5] | Payload access control per collection and field [P36]; design is code, so editors cannot touch it by construction [U] | Done |
| Review before publish | Publish toggle; Framer "Finish Editing" email [S26][P5] | Drafts + a "publish" permission; notify the room [U] | Small |
| Localization | Add-on per locale [S32][P4] | Payload field-level `localized: true`, fallback [P44]; the agent translates | Done |
| SEO meta/OG per page and item, sitemap, redirects, schema | Yes [P19][S16] | Payload SEO and redirects plugins [P40]; Next `generateMetadata`, `sitemap.ts`, `robots.ts` [U] | Small (defaults in starter) |
| Forms with submissions | Hosted [S15][S33] | Payload form builder: fields, submissions in DB, emails, even a payment field [P42] | Done |
| Site search | Native | Payload search plugin, "SSR search in Next.js" [P40] | Done |
| Media with focal point and sizes | Yes | Payload Media; Vercel Blob storage adapter [P40][P41] | Done |
| Side-by-side live preview | Canvas is the preview | Payload Live Preview iframe via `postMessage`, breakpoints [P37] | Done |
| **Click-to-edit on the live page** | Yes, the core UX [P5][S26] | Vercel Content Link is Pro/Enterprise, and Payload's plugin is "enterprise-only", key from sales [P47][P45]. Otherwise build an overlay | **Real gap** |
| **Freeform layout editing by humans** | Yes, the whole point of the canvas | Payload blocks let editors add, reorder and remove sections with typed fields [P40]; nothing visual beyond that. Puck gives drag-and-drop (MIT) [P50] | **Gap** (agent fills it) |
| Interactions authoring | GSAP IX3 [S30] | Code (Motion, GSAP) by the agent | Agent only |
| Analytics + A/B in one | Framer add-on; Webflow Analyze/Optimize [P4][S31] | Vercel Web Analytics; PostHog (analytics, feature flags) in the Stripe catalog [P49][P2] | Wire-up |
| Hosting/CDN, one-button publish | Yes | Vercel; `lightbulb app publish` | Done |

### 2.2 The embeddable CMS options

| Option | Runs | Data | On-page visual editing | Roles | Drafts / scheduling | Licence | Maturity |
|---|---|---|---|---|---|---|---|
| **Payload 3** | **In the Next.js app** (`/app`, admin at `/admin`) [P36] | **Postgres via Drizzle** (`@payloadcms/db-postgres` or `db-vercel-postgres`); Neon in the Vercel template [P39][P41] | Side-by-side live preview [P37]; click-to-edit only via Vercel Content Link (enterprise) [P45] | Granular access control [P36] | Drafts, autosave, scheduled publish (jobs) [P38] | MIT [P56] | 45.6k stars; acquired by Figma June 2025, said to stay open source [P56][S46]; Payload Cloud reportedly paused sign-ups [S46] |
| **Keystatic** | In-app admin | Files in the repo (local or GitHub), or Keystatic Cloud [P54] | No | Not documented [P54] | Draft-mode preview recipe [P54] | MIT [P56] | 2.5k stars; last push 1 Oct 2026 [P56] |
| **TinaCMS** | In-app editor; backend TinaCloud or self-hosted | Git (Markdown/MDX) + an index DB (MongoDB, Vercel KV) when self-hosted [S53] | **Yes**, side-by-side contextual editing with `useTina` [P52]; supports Vercel Content Link [P47] | TinaCloud | Editorial workflow (branches) on paid plans [S53] | Apache-2.0 [P56] | 13.9k stars |
| **Decap** | In-app SPA at `/admin` | Git commits | No | Git provider | Editorial workflow via PRs [U] | MIT [P56] | 19.5k stars |
| **Sanity** | Studio can be embedded; content is hosted in Sanity's Content Lake | Hosted | **Yes**: Presentation tool, click-to-edit overlays, stega [P55] | Yes | Content Releases [P55] | Studio MIT; service paid [P56] | Mature |
| **Contentful** | Hosted | Hosted | Via Vercel Content Link [P47] | Yes | Yes | Proprietary | Mature |
| **Builder.io** | Hosted visual editor + SDKs | Hosted | Yes (drag and drop on your components) | Yes | Yes | SDKs MIT; service proprietary [P56][S57] | Mature |
| **Makeswift** | Hosted editor, runtime in Next.js | Hosted | Yes | Plans | Yes | Runtime MIT [P56]; service from about $79/mo [S57] | Small (248 stars) |
| **Plasmic** | Hosted studio (self-host discussed) | Hosted | Yes | Yes | Yes | MIT [P56][S57] | 7.1k stars |
| **Puck** | **In the React/Next app** | **Your own DB** (you write the save function) [P50] | **Yes, drag-and-drop page builder** on your components [P50] | You build | You build | MIT [P50] | 13.5k stars; Puck AI generates pages from your components (paid cloud) [S51] |

**What this means:**
- *Payload is the only option that is in-app, on Postgres/Neon, open source, and complete.* Complete means collections, drafts, scheduling, i18n, forms, redirects, search, SEO and roles. Its website template already ships Pages and Posts with layout blocks (Hero, Content, Media, Call To Action, Archive), Categories, Media, the SEO, Redirects, Search and Nested Docs plugins, on-demand revalidation, and a Vercel + Neon + Blob variant with a deploy button [P40][P41].
- *Puck is the only in-app, own-database option with true on-page drag-and-drop.*
- *Everything with polished click-to-edit is hosted (Sanity, Builder, Makeswift, Plasmic) or paid (Vercel Content Link).*

### 2.3 What is truly hard

1. **On-page click-to-edit with the design locked.** Webflow and Framer own the renderer, so every element knows its source field. In Next.js we must annotate rendered elements with their source. Vercel does it with stega-encoded content source maps [P45]. We can do it more cheaply with explicit `data-edit` attributes emitted by our block renderers, because the agent writes the renderers [U].
2. **Visual layout editing for humans.** Rebuilding a canvas is out of scope. Payload blocks (add, reorder, remove sections, each with typed fields) give marketers what they actually change weekly: copy, images, the order of sections, new posts. Restyling and new layouts go to the agent [U].
3. **Interactions, analytics and A/B, and hosting as one bill.** These can be assembled (code, PostHog, Vercel) but are not one product [P2].
4. **Ten years of polish and templates.** Not closable by code. Closable partly by the agent doing the design work [U].

---

## 3. The reverse-engineering angle

### 3.1 What Webflow and Framer actually publish

- **Webflow** publishes static HTML/CSS, a runtime script, and GSAP for interactions [S30]. CMS, forms, search and localization are hosted services. That is why export drops them [S28]. The design is a class-based CSS system (Flowkit for AI-generated sites) [S27].
- **Framer** publishes server-rendered HTML that a React bundle hydrates (`hydrateRoot`). Data is embedded in the HTML so CMS pages need no fetch on hydration [S17].

Both therefore produce an output we can match or beat with Next.js App Router + React Server Components: static-rendered pages, revalidated when content changes (Payload's template does this with hooks [P40]), with almost no client JavaScript for content pages [U]. The value is not in the output format. It is the editing and publishing loop around it.

### 3.2 Can the agent build on the customer's own Webflow or Framer?

- **Webflow:** yes, through the MCP server (OAuth, one workspace per authorization), and through the Data API for CMS, pages and SEO. Limits [P19]:
  - A human creates the site first.
  - Canvas operations need the Designer tab open with the Bridge App.
  - No classic interactions.
  - No new localized items.
- **Framer:** yes, through Framer MCP or External Agents (`npx @framer/agent setup`, browser approval, a per-project API key). Every agent change goes on a branch, and publish happens through `publish()`/`deploy()` [P7][P8][P10]. Gaps: no overrides on nodes, no analytics, no project or domain settings [P7].

### 3.3 Are they in the Stripe Projects catalog?

**No.** The providers table in `docs.stripe.com/projects` and the 68 providers at `projects.dev/providers` list neither Webflow nor Framer, nor Sanity or Contentful [P1][P2]. Three website-platform providers are present [P2]:
- **Wix** (`wix/headless`): "the full Wix business backend: commerce, bookings, CMS, CRM, events and memberships", with "a dashboard their clients can run themselves". "Resources stay in the user's own Wix accounts". It is a "developer preview" [P3].
- **WordPress.com** (`wordpress.com/site`): "Managed WordPress hosting with built-in domains, CDN, built-in AI, automatic updates".
- **Squarespace** (`squarespace/domain`): domains only.

Also in the catalog and relevant: Vercel, Neon, Resend (email), Algolia (search), Cloudinary (media), and PostHog, Mixpanel and Amplitude (analytics, feature flags) [P1][P2].

### 3.4 Trade-off: agent builds in Webflow/Framer vs agent builds Next.js + embedded CMS

| | Agent builds in the customer's Webflow / Framer | Agent builds Next.js + Payload on the customer's Vercel + Neon |
|---|---|---|
| Editor UX after launch | **Best in class**: on-page editing, roles, review, branches [P5][S26][P6] | Admin + live preview + our Edit overlay; good but not equal [P37][U] |
| Layout changes by humans | Yes (designers) | No; blocks only, or the agent |
| Provisioning | Not in Stripe Projects; a human signs up, picks a plan, creates the site, authorizes [P2][P19] | One `stripe projects add` path we already use [P1] |
| Cost to customer | Webflow site plan + editor seats + localization/Optimize add-ons; Framer Pro $30/mo + $20 per extra editor + add-ons [P4][S31][S32] | Vercel Pro (Hobby is "non-commercial, personal use only" [P49]) + Neon; no per-editor fee [U] |
| Agent reliability | Depends on vendor MCP coverage; canvas tools need an open Designer tab (Webflow) [P19] | The agent's home ground: code, tests, preview, rollback |
| Mixing with "apps" (portals, forms with logic, payments) | Separate: Webflow Cloud app under a path [P24]; Framer code components | Same codebase, same `lib/door.ts`, same database |
| Ownership and exit | Vendor-locked; export drops CMS [S28] | Customer owns repo, DB and hosting |
| Lightbulb's rail (versions, rollback, Canvas card) | Not ours; Framer branches, Webflow publish | Ours (`up`, `publish`, `rollback`) |

**Read:** the default is Next.js + Payload, because it rides our rail and the customer owns everything. The Webflow/Framer route is the right answer in one case only: **the company already has a site there.** Then "nothing is declined" means the agent edits it in place through the vendor's MCP rather than rebuilding it. Wix Headless is worth a spike as a third route for "we want Wix's dashboard and bookings" companies, because it is the one site builder that is in the catalog [P3][U].

---

## 4. Editing by people after launch

### 4.1 The best non-technical editor UX in each approach

**Framer (benchmark).** Visit the live site, press Edit, click a headline and type. For a new post, press "Add Page" on a post. For SEO text, use "Show All Fields". Press "Finish Editing", and a publisher gets an email and publishes [P5]. With 3.0, teammates and agents work on branches [P6].

**Webflow.** Go to `site.com/?edit` or press "Edit site", edit on the canvas with styles locked, then publish if allowed [S26][P25].

**Next.js + Payload, as we would ship it:**

| Step | What the person does | Mechanism |
|---|---|---|
| Sign in | Visits the live site while signed in to the workspace; an **Edit** bar appears | `lib/door.ts` cookie → Payload custom auth strategy (`authenticate(headers)` returns the user; `disableLocalStrategy: true`) [P43] |
| Change a headline | Hovers a section, clicks its pencil, and lands in `/admin` on that document and field, with live preview beside it | Block renderers emit `data-edit="pages/{id}#layout.{i}.heading"`; overlay links there [U]; Live Preview iframe [P37] |
| Reorder or add a section | Drags blocks in the admin layout field; picks from the site's block types | Payload blocks [P40] |
| Write a post | `/admin/collections/posts/create`, or "New post" on the blog index | Payload Posts + drafts + autosave [P38][P40] |
| Schedule | Sets "publish at" | `schedulePublish` + jobs (needs Pro cron or the room agent's tick) [P38][P48] |
| Review | "Submit" saves a draft and posts a preview link to the room; a publisher clicks Publish | Drafts [P38]; room message [U] |
| Translate | Switches locale in admin, or asks the agent | Field-level localization [P44] |
| See form leads | `/admin/collections/form-submissions`; the room gets a message per submission | Form builder [P42]; Resend in catalog [P2] |

This is two clicks more than Framer: hover the pencil, then edit in a side panel instead of inline. That is the honest gap. Vercel Content Link closes the same gap the same way, by linking from element to field rather than editing inline [P47], so even the paid Vercel route is not truly inline.

### 4.2 How the room agent edits content on request

Rule: **content requests change data, not code.** "Change the hero headline to X":
1. The agent resolves "hero" to `pages/home → layout[0] (hero) → heading`. It reads the collection through Payload's Local API in a script, or the REST API with an agent user [P36][U].
2. It writes a **draft** (`_status: draft`) and posts the preview link with a before/after diff in the room [P38][U].
3. On "ship it" it publishes the draft. Revalidation hooks refresh the page with no deploy [P40].
4. The edit is attributed to the agent user in Payload's version history, so a human can restore the previous version from the admin [P38].

Code changes happen only for a new block type, a layout or style change, interactions, or a new collection. They go through the normal `up` → preview → `publish` loop with a migration. This is the same split Webflow draws between Content Editor and Designer [P25]. Here the agent is the designer.

---

## 5. Recommendation for Lightbulb

### 5.1 The stack

Keep the **one starter** from the apps spec. Add a `content` flag to the starter's `services`; turning it on adds the content layer to the same app [U]. It is on by default when the request is a site, blog, docs or landing page with sections. The flag adds:

1. **Payload 3** in the same Next.js app (admin at `/admin`), using `@payloadcms/db-postgres` against the app's Neon database and Neon branch for previews [P39][P41]. Copy from Payload's website template [P40]:
   - collections: `pages` (layout blocks), `posts`, `media`, `categories`, `users`;
   - globals: `header`, `footer`;
   - plugins: SEO, Redirects, Search, Nested Docs, Form Builder [P40][P42];
   - drafts with autosave and `schedulePublish` [P38];
   - on-demand revalidation hooks [P40].
2. **Sign-in.** One Payload auth strategy that trusts `lib/door.ts` (`disableLocalStrategy: true`) [P43]. Workspace members get `editor` (create and edit drafts) or `publisher` (publish), mapped from room roles [U]. No second password.
3. **Media.** Vercel Blob (`@payloadcms/storage-vercel-blob`) as in the template [P41]. Use Cloudinary from the catalog only if the site is image-heavy [P2].
4. **Visual editing.**
   - Payload Live Preview with phone, tablet and desktop breakpoints [P37].
   - Our **Edit overlay**: a ~60-line client component, rendered only for signed-in editors. Block renderers emit `data-edit` paths, and the overlay shows a pencil per section linking to `/admin/...#field` [U].
   - Skip Vercel Content Link: Pro/Enterprise, and Payload's plugin needs an enterprise key [P47][P45].
   - Skip Puck until a room asks twice for drag-and-drop page layout. It is the add-on that closes the layout gap later: MIT, stores JSON in our DB [P50].
5. **SEO defaults** (written once in the starter, never asked about):
   - `generateMetadata` from SEO plugin fields, with fallbacks to item title, excerpt and cover;
   - canonical URLs; an OG image route;
   - `app/sitemap.ts` from published pages and posts; `app/robots.ts`;
   - a redirects table enforced in middleware;
   - JSON-LD: `Organization` site-wide, `Article` on posts, `BreadcrumbList` on nested pages;
   - `hreflang` when locales exist [P40][P44][U].
   - Run a check before publish: every page has a title, a description and one H1; there are no broken links. This mirrors Framer's `seo-check` and audits [P6].
6. **Forms.** Payload Form Builder: submissions stored in Neon and emailed through Resend (in the catalog). Each submission is also posted to the room [P42][P2].
7. **Search.** The Payload search plugin. Use Algolia only if the content is large [P40][P2].
8. **Analytics and A/B.** Vercel Web Analytics by default (50k events/month on Hobby [P49]). PostHog (catalog: analytics, feature flags) when the room asks for experiments [P2].
9. **Hosting plan.** A company site is commercial use, and Vercel Hobby is "non-commercial, personal use only". Hobby cron is also once a day [P49][P48]. **Content sites should go on Vercel Pro**, which also makes scheduled publish run to the minute. This conflicts with the spec's "Hobby by default" and needs a founder ruling [U].

### 5.2 The workflow and what the person sees

1. **Request** ("We need a marketing site with a blog, edited weekly"). The agent reads the company's existing site URL, brand and any document. It asks at most one question: the name, or "Is your current site on Webflow/Framer/WordPress?" If it is, see 5.4.
2. **Build.** The agent:
   - models the content (collections and block types);
   - imports existing pages and posts by scraping the current site or pulling through Webflow's Data API or Framer's Server API [P20][P9];
   - designs blocks from the brand and seeds real content;
   - runs the SEO check and posts a **preview link** plus a screenshot card.
3. **Iterate in chat.** "Make the hero darker" is a code change and a new preview. "Change the headline" is a content draft and a preview.
4. **Publish.** `lightbulb app publish`. The Canvas card shows the live URL, and a second line says "Editors: open the site and press Edit, or go to /admin".
5. **After launch:**
   - People edit on their own with Edit → field → Save draft → Publish (if they are publishers).
   - Reviews and publishes post to the room.
   - Scheduled posts go out on time on Pro.
   - The agent stays available for "add a careers page", "translate to Spanish" and "make a new section type".

### 5.3 The agent's instructions (additions to `AGENTS.md` when `content` is on)

- Every visible string, image and link on a content page comes from a Payload field. Never hardcode copy in JSX, except legal boilerplate and empty states.
- A new kind of section means a new block type: its fields, its renderer, its `data-edit` attribute, and a migration. Reuse an existing block if one fits.
- A request to change content gets a draft through the Payload API, never a code edit. Show a diff and a preview link. Publish only when asked.
- Never change collections, block schemas or styles because an editor asked to change words.
- Every page and post has SEO fields filled. Run the SEO check before `publish`.
- Images go through `media`, with alt text required.
- Redirect every changed slug, because the Redirects plugin exists.
- Production uses `push: false` and committed migrations ("Set `push: false` for production databases"). Seeding is destructive and is never run against live [P41].

### 5.4 When the company already has Webflow or Framer

Do not migrate unless asked. Connect the agent to their account:
- **Webflow:** MCP OAuth. The human creates any new site and keeps the Designer open for canvas work [P19].
- **Framer:** `npx @framer/agent setup` with a project key. Changes land on a branch for their review [P7][P8].

The agent then does the same jobs there: CMS items, SEO, redirects, translations and new sections. If they want to leave a platform, the migration is the §5.2 import step.

### 5.5 What still beats us, and how to close it

| Gap | Who beats us | Close it by |
|---|---|---|
| Inline editing on the live page | Framer On-Page Editing, Webflow Edit mode [P5][S26] | Edit overlay + live preview now [U]; inline text editing for `heading`/`body` fields as a v2 overlay, saved through Payload REST (same field paths) [U] |
| Human drag-and-drop of layout and styles | Webflow and Framer canvases | Blocks now; Puck on a second ask [P50]; the agent for everything else |
| Interactions authoring | Webflow IX3 (GSAP) [S30] | The agent writes Motion/GSAP on request; keep a small library of named effects in the starter [U] |
| A/B, personalization and analytics in one place | Webflow Optimize, Framer add-on [S31][P4] | PostHog flags and experiments when asked [P2] |
| Team branches and review for many editors | Framer 3.0 Branching [P6] | Payload drafts + room review now; Neon branches per change request later [U] |
| Speed to a beautiful first version | Webflow AI site builder, Framer Wireframer and Agents [S27][S18][P6] | Brand read + block library + design tokens, as §4 of the templates note describes |
| Template marketplace and designer community | Both | Not closed; the agent's designs plus the customer's brand |
| Per-seat polish (comments on page, activity log) | Both [P19] | Vercel Comments on previews [P49]; room threads |

### 5.6 Open questions for the founder

1. Vercel Pro for content sites (commercial use, minute-level cron) versus "Hobby by default" in the spec [P49][P48].
2. Is the `content` flag on by default for every site request, including one-page landing pages? (The proposal is yes, so marketing can edit from day one.)
3. Is a Wix Headless spike worth doing as a third route for "we want a dashboard and bookings" companies, given it is in the Stripe catalog [P3]?

---

## Sources (all fetched or searched 2026-10-29)

Internal: `docs/research/2026-10-29-templates-kinds-of-apps.md` §6; `docs/superpowers/specs/2026-10-28-apps-design.md`.

- [P1] Stripe Projects docs, providers table — https://docs.stripe.com/projects
- [P2] Stripe Projects provider directory (68 providers incl. `wix/headless`, `wordpress.com/site`, `squarespace/domain`) — https://projects.dev/providers
- [P3] Wix press release, "Wix Brings the Wix Headless Business Backend to Stripe Projects" — https://www.wix.com/press-room/home/post/wix-brings-the-wix-headless-business-backend-to-stripe-projects
- [P4] Framer pricing — https://www.framer.com/pricing
- [P5] Framer help, On-Page Editing — https://www.framer.com/help/articles/on-page-editing/
- [P6] Framer blog, Framer 3.0 — https://www.framer.com/blog/framer-3/
- [P7] Framer help, Use external agents with Framer — https://www.framer.com/help/articles/use-external-agents-with-framer/
- [P8] Framer, External agents / MCP — https://framer.com/agents/external
- [P9] Framer Server API introduction — https://www.framer.com/developers/server-api-introduction
- [P10] Framer Server API quick start — https://www.framer.com/developers/server-api-quick-start
- [P11] Framer Server API reference — https://www.framer.com/developers/server-api-reference
- [P12] Framer developers home — https://www.framer.com/developers/
- [P13] Framer dictionary, "Up to 100K items" — https://www.framer.com/dictionary/up-to-100k-items
- [S14] Framer developers, Working with CMS Collections / ManagedCollection (search extract) — https://framer.com/developers/cms ; https://www.framer.com/developers/reference/plugins-managed-collection
- [S15] Framer localization, forms, On-Page Editing 2.0 (search extracts) — https://www.framer.com/updates/localization ; https://www.framer.com/updates/localized-page-paths ; https://www.framer.com/updates/on-page-editing-2.0
- [S16] Third-party Framer SEO guide — https://www.stackmatix.com/blog/framer-site-seo-startups
- [S17] Framer performance (search extracts) and runtime teardown — https://www.framer.com/ja/blog/sites-interactive-faster/ ; https://dev.to/ankur_khandlwal/i-reverse-engineered-framers-react-runtime-to-export-sites-as-static-html-b75 ; https://sharppages-com.pages.dev/blog/framer-vs-custom-website-react-overkill/
- [S18] Framer AI launch (Wireframer, Workshop) — https://www.businesswire.com/news/home/20250521574932/en
- [P19] Webflow MCP server overview — https://developers.webflow.com/mcp/reference/overview.md
- [P20] Webflow Data API introduction — https://developers.webflow.com/data/reference/rest-introduction
- [P21] Webflow, Publishing with the CMS API — https://developers.webflow.com/data/docs/working-with-the-cms/publishing
- [P22] Webflow, Create collection item — https://developers.webflow.com/data/reference/cms/collection-items/staged-items/create-item
- [P23] Webflow rate limits — https://developers.webflow.com/data/reference/rate-limits
- [P24] Webflow Cloud introduction — https://developers.webflow.com/webflow-cloud/intro
- [P25] Webflow University, Content editor role quick guide — https://university.webflow.com/resources/guides/quick-guide-content-editor-role
- [S26] Webflow help, Edit mode / content editors; component content update (search extracts) — https://help.webflow.com/hc/en-us/articles/33961251014931 ; https://webflow.com/updates/edit-global-component-content
- [S27] Webflow AI site builder (search extracts) — https://help.webflow.com/hc/en-us/articles/38840145286035 ; https://webflow.com/updates/ai-site-builder-evolved ; https://www.appsrow.com/blog/webflow-ai-site-builder-in-2026-what-it-actually-builds-and-where-it-falls-short
- [S28] Webflow code export (search extract) — https://help.webflow.com/hc/en-us/articles/33961386739347
- [S29] Webflow CMS limits (search extract + third party) — https://help.webflow.com/hc/en-us/articles/33961370432275 ; https://www.rapidevelopers.com/webflow-tutorials/webflow-cms-limits
- [S30] Webflow GSAP / IX3 — https://webflow.com/updates/gsap-becomes-free ; https://webflow.com/blog/webflow-interactions-with-gsap
- [S31] Webflow Optimize — https://webflow.com/feature/optimize ; https://www.digidop.com/blog/optimize-by-webflow-the-ai-powered-cro-tool-for-marketers
- [S32] Webflow Localization (third party) — https://www.lowcode.agency/blog/webflow-localization-and-multi-language-what-to-know
- [S33] Webflow hosting, redirects (search extracts) — https://webflow.com/hosting ; https://help.webflow.com/hc/en-us/articles/33961315914515 ; https://wishlist.webflow.com/ideas/WEBFLOW-I-2635
- [S34] Wix CMS roles and dynamic pages — https://support.wix.com/en/article/cms-formerly-content-manager-creating-custom-roles-and-permissions ; https://support.wix.com/en/article/cms-about-dynamic-pages
- [S35] Squarespace blog and permissions — https://support.squarespace.com/hc/articles/206543727 ; https://webcf.waybackmachine.org/web/20210817155638/https://support.squarespace.com/hc/en-us/articles/206537297-Squarespace-permissions-explained
- [P36] Payload README — https://raw.githubusercontent.com/payloadcms/payload/main/README.md
- [P37] Payload Live Preview — https://payloadcms.com/docs/live-preview/overview
- [P38] Payload Drafts — https://payloadcms.com/docs/versions/drafts
- [P39] Payload Postgres adapter — https://payloadcms.com/docs/database/postgres
- [P40] Payload website template README — https://raw.githubusercontent.com/payloadcms/payload/main/templates/website/README.md
- [P41] Payload with-vercel-website template README — https://raw.githubusercontent.com/payloadcms/payload/main/templates/with-vercel-website/README.md
- [P42] Payload Form Builder plugin — https://payloadcms.com/docs/plugins/form-builder
- [P43] Payload custom auth strategies — https://payloadcms.com/docs/authentication/custom-strategies
- [P44] Payload localization — https://payloadcms.com/docs/configuration/localization
- [P45] Payload Vercel Visual Editing — https://payloadcms.com/docs/integrations/vercel-visual-editing
- [S46] Figma acquires Payload; Payload Cloud sign-ups — https://www.cmswire.com/digital-experience/when-cms-meets-ux-design-what-figmas-payload-deal-really-means ; https://techsy.io/blog/payload-cms-guide
- [P47] Vercel Edit Mode / Content Link — https://vercel.com/docs/edit-mode
- [P48] Vercel cron usage and limits — https://vercel.com/docs/cron-jobs/usage-and-pricing
- [P49] Vercel Hobby plan — https://vercel.com/docs/plans/hobby
- [P50] Puck README — https://raw.githubusercontent.com/puckeditor/puck/main/README.md
- [S51] Puck AI 0.8 — https://puckeditor.com/blog/puck-ai-08
- [P52] TinaCMS contextual editing — https://tina.io/docs/contextual-editing/overview
- [S53] TinaCMS open source / self-hosted / editorial workflow — https://tina.io/blog/Tinacms-is-now-fully-open-source/ ; https://tina.io/docs/drafts/editorial-workflow/
- [P54] Keystatic introduction — https://keystatic.com/docs/introduction
- [P55] Sanity Visual Editing introduction — https://www.sanity.io/docs/visual-editing/introduction-to-visual-editing
- [P56] GitHub repo metadata (licence, stars, last push) via api.github.com/repos/{payloadcms/payload, Thinkmill/keystatic, tinacms/tinacms, decaporg/decap-cms, sanity-io/sanity, BuilderIO/builder, makeswift/makeswift, plasmicapp/plasmic, puckeditor/puck}
- [S57] Builder.io vs Plasmic vs Makeswift (third party) — https://www.pkgpulse.com/guides/builder-io-vs-plasmic-vs-makeswift-visual-page-builders-2026
