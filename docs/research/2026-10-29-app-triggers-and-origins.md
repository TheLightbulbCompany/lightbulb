# Why and when a company ends up wanting a hosted app, and where the app starts from (29 Oct 2026)

**Why this exists.** The founder, reviewing §3 of `docs/superpowers/specs/2026-10-28-apps-design.md`:
"There are different kinds of triggers and things that need to happen for someone to want a hosted
app… sometimes it might be external, sometimes people might want to copy other apps", and on
personalization: "It could ask to personalize or look into the market and build something custom
for them." §3 today has one entry point ("Ask: one sentence in a room"). This note maps the other
entry points, how the builders handle each, what Lightbulb already has that can fire them, and
rewrites §3 as origins × what happens.

**Tags.** [P] primary (the vendor's own docs, changelog or site; Lightbulb code on `origin/main`
with file:line). [S] secondary. [U] unverified. Every web source fetched 2026-10-29. Lovable docs
are cited as `docs/<path>` for `https://docs.lovable.dev/<path>`, all read from
`https://docs.lovable.dev/llms-full.txt`. No browser, no Reddit or X fetched directly; Reddit/HN
quotes are carried from `2026-10-27-small-software-voice-of-customer.md`, which fetched them.

Builds on, without repeating: `2026-10-29-lovable-ux-deep.md` (Lovable screens),
`2026-10-29-templates-kinds-of-apps.md` §3 (what builders read vs ask),
`2026-10-27-small-software-*.md` (demand evidence).

---

## Short answer

- **There are nine ways an app comes about, not one.** A person asks. Data outgrows a sheet. A
  chore or question repeats. Something outside asks for it (a customer, a partner, an event, a
  vendor failing). Someone wants one "like X". The company wants to drop a SaaS it pays for. It wants
  to move its own site or app. It starts from a template. It copies a teammate's app. Only the first
  goes through §3's "Ask" row today. (§1)
- **Builders handle the copy and migrate cases with imports, and draw one legal line.** You may copy
  in full only what you own. Lovable asks you to confirm in chat that you own the site, once per
  site. Base44 says you "must be the rightful owner". For someone else's site, the closest any
  builder gets to a safe mode is Base44's **Design only** ("same look and feel", new content).
  Replit, Bolt and v0 say nothing about ownership. The reason the line matters: Proofpoint found
  "tens of thousands" of malicious sites built on Lovable, and Lovable now scans every new site
  for phishing. (§2)
- **No builder looks at a company's work and proposes a new app.** Their suggestions are next-step
  chips inside an app that already exists. Lightbulb already has the parts to do this: `suggest_idea`
  plus the Ideas list, Automations, and workspace memory. An app is just one more kind of idea. (§3)
- **No builder does a market scan before building as a normal step.** Stripe's starter interviews
  you, but only about the product. Lovable's and Replit's Plan modes research the project's code,
  not the market. Lovable researches competitors only for ads and SEO, after the app is built. An
  optional "look at what others do" step that runs alongside the build and never blocks the
  preview would be new. (§4)
- **Recommendation:** rewrite §3 as a table of origins (who starts it, what the builder does first,
  what it asks, what the person sees), plus a cloning rule and the optional research card. (§5)

---

## 1. Why and when someone ends up wanting a hosted app

Each origin is listed with the evidence that people really start from it, and the moment it
happens.

| # | Origin | The moment | Evidence |
|---|---|---|---|
| A | **A person asks, from nothing** | An idea, a request in chat | The default in every builder. Lovable puts the prompt before the account: "keeping your prompt through sign-up" [P docs/introduction/getting-started]. |
| B | **Data has outgrown a sheet** | The sheet gets shared, gets too big, or a second person has to edit it | "We track this process in a spreadsheet and it sucks" (Jese__Ki, r/founder, 28 Sep 2026) [S via VoC file]. "3 hours per week… in a crappy spreadsheet" → a dashboard in 4 hours (meetingthrower, HN, 19 Jan 2026) [S via VoC file, https://news.ycombinator.com/item?id=46677867]. Every builder has a start-from-a-sheet door: Replit imports `.xlsx`, `.csv` or a Google Sheets URL [P https://docs.replit.com/build/import-from-providers]. Base44 reads a sheet "for their columns and sample data" [P https://docs.base44.com/Getting-Started/starting-from-your-first-prompt]. Softr sells a Google Sheets client portal [P https://www.softr.io/use-cases/google-sheets-client-portal]. AppSheet runs from Sheets → Extensions → Create an App [S https://advancedweb.hu/intro-to-appsheet/]. |
| C | **A chore or question keeps coming back** | The third time someone merges the same CSVs, answers the same question, or rebuilds the same report | "I manually merge these three CSVs every Friday" (Jese__Ki) [S]. "Any time someone is like man this sucks, I'll build a tool for it" (a sign shop owner, r/signshop, 23 Sep 2026) [S]. "A coworker comes to me with a question about our DB content…" → wanting non-engineers to "do that whole loop by themselves" [S VoC file line 77]. |
| D | **Something outside asks for it** | A customer wants a portal or status page. A partner sends a form to fill in. An event or launch has a date. A vendor fails or a product is shut down. | eXp Realty: "a failed vendor before a Peru launch was the turning point". The Hub was built to replace Facebook Workplace after it was shut down (~$1M/yr avoided) [P https://lovable.dev/blog/exprealty]. Softr sells its portal against "messy email threads and spreadsheet chaos" [P https://www.softr.io/client-portal]. Lovable's template categories include Events and Landing Page [P https://lovable.dev/templates]. |
| E | **"Like X": copy a product or a look** | Someone has seen Calendly, a competitor's page or a screenshot and wants "that, but ours" | Every builder accepts a screenshot. v0 "infers likely functionality based on visible UI elements" [P https://v0.app/docs/screenshots]. Base44: "Design only… same look and feel" [P Base44 first-prompt]. Website cloning is a paid feature in Dyad Pro [P https://www.dyad.sh/pro]. Firecrawl's open-lovable says "Clone and recreate any website as a modern React app" [P via `2026-10-28-small-software-open-lovable-read.md`]. A whole category of products (Same.new and others) exists only to clone sites [S https://knowara.com/ai-tools/coding/same-new-review/]. |
| F | **Replace a SaaS they pay for** | A renewal comes up, a per-seat price rises, a vendor won't fit, a needed feature never ships | Retool, Feb 2026, n=817 (its own customers): 35% replaced at least one SaaS tool [P via thesis file]. Lovable's stories include "replaced a $40,000 Salesforce contract" and eXp's "cancelled millions in SaaS contracts" ("per-seat SaaS pricing grew with headcount") [P https://lovable.dev/blog/customer-stories, /blog/exprealty]. Lightbulb already has an idea for the audit half of this: `apps/backend/ideas/unused-software-seats.yaml` [P origin/main]. |
| G | **Move their own site or app** | Leaving Wix, Squarespace or Webflow. An agency built the site and the company wants to edit it. A prototype was built in another builder. | Lovable's import page names it: "move a site you own off a website builder" [P docs/features/projects/import-website]. Importing from Replit, Bolt, v0, Base44 and Manus is a first-class flow [P docs/features/projects/import-project]. Replit imports from Lovable, Bolt, Base44, Vercel and GitHub [P Replit import]. Lightbulb has a `website-rebuild` idea priced against agencies at "$3000–8000, 2–6 weeks" [P `apps/backend/ideas/website-rebuild.yaml`]. |
| H | **A template someone shares** | A link from a peer, a community gallery, a template the workspace saved | Lovable templates each have **Preview** and **Remix** and can be browsed logged out. Workspace templates are on Business+ [P via `lovable-ux-deep.md` §8]. Bolt has Marketplace templates and Teams "team template" [P https://support.bolt.new/building/start-project]. |
| I | **A teammate's app, copied or extended** | "Can I have Ana's tracker for my team?" or "add a page to it" | Lovable Remix makes "your own independent copy… changes you make to it never affect the original". Public remixing is off by default [P docs/features/projects/remix]. |

**What the evidence says about these.**

1. **B, C, D and F happen in the company's work before anyone types a request.** That is where an
   agent that already lives in the company's chat, mail and connected tools sees something an
   editor-first builder never can (§3).
2. **D has deadlines.** A launch date or a customer waiting turns "nice to have" into "needed by
   Friday". The builder should ask for the date when the origin is external, and lead with the
   smallest working slice.
3. **E and G look alike but are opposites legally.** G copies your own work. E copies someone
   else's. Builders split them by ownership, not by technique (§2).
4. **Most one-off tools die from neglect, not because they fail to build.** The voice-of-customer
   file lists three common fates: thrown away on purpose, "quietly promoted to recurring, then rots",
   or "never adopted by anyone but the author". A Rokt lead guessed "only a handful get continued
   consistent usage" out of ~100 Replit tools [S via `small-software-one-off-evidence.md`]. The
   origins that come from real, repeated work (B, C, D, F) are the ones most likely to be used, so
   the builder should record where each app came from and why.

## 2. How the builders handle each origin, and where they draw the line on copying

| Origin | Lovable | Bolt | v0 | Replit | Base44 | Dyad / cloners |
|---|---|---|---|---|---|---|
| B sheet | Attach a file. Connectors for Airtable, Notion, Google Workspace [P via templates file] | Upload files [P Bolt start] | — | **Spreadsheet** import (`.xlsx`, `.csv`, Sheets URL) [P Replit import] | Reads columns and sample rows [P Base44] | — |
| E screenshot | Attach it, and draw on it to circle what you mean [P docs/features/projects/chat] | Upload [P Bolt start] | Analyses "layout, colors, and components" [P v0 screenshots] | Frames attached as snapshots [P via templates file] | "match a design, a screenshot" [P Base44] | — |
| E Figma | Plugin, MCP, `.fig` upload, "not to rebuild every screen" [P docs/integrations/figma] | Frame import through Anima. Login required [P https://support.bolt.new/integrations/figma] | Link to a file or frame, no plugin [S https://www.v0.build/docs/figma, search snippet only] | Frame URL → React [P Replit import] | **Import from Figma** [P Base44] | — |
| E someone else's URL | **Not allowed**: owner or permission only (below) | No URL start documented [P Bolt start] | Not documented. A 2024 blog says a URL in the prompt was turned into a screenshot [S https://old.baoyu.io/blog/clone-page-by-url-v0dev] | No URL start [P Replit import] | **Design only**, with an ownership warning that still applies [P Base44] | Dyad Pro "Website cloning" with no stated rule [P dyad.sh/pro; rule U]. Same.new: public URLs [S] |
| G own site | **Import website**: confirm ownership in chat, up to 10 pages per request, side-by-side check at desktop and phone size [P import-website] | — | — | Vercel import (repo, not domain) [P Replit import] | **Content & Design** [P Base44] | — |
| G another builder | ZIP or guided export from Replit, Manus, Bolt, Base44, v0. Reports **Ready / Still needed** [P import-project] | Import from GitHub or Lovable [P Bolt start] | — | Lovable, Bolt, Base44, Vercel, GitHub, ZIP. Records and secrets do not come over [P Replit import] | — | — |
| H template | 228 templates. Picking one skips the design questions [P via templates file] | Marketplace, team template [P Bolt start] | 13 categories [P via templates file] | — | — | — |
| I teammate | Remix. A remix of a project on an outside Supabase needs edit access [P remix] | — | — | — | — | — |

**What comes over and what doesn't, everywhere.** Every builder copies what a visitor can see:
text, images, fonts, colours, layout. None copies the server: "logins, databases, or anything else
that ran on the original site's server", and a form "keeps its look, but not what the original
site did with the submissions" [P Lovable import-website]. App imports bring the code and the
database schema but not the records or the secrets [P Replit import; Lovable import-project]. The
builder then rebuilds the parts that actually work (the form handler, sign-in, the data).

**The copying lines, as each builder states them.**

- **Lovable**: "Import only a website you own or have the owner's permission to copy… Importing a
  client's site you are rebuilding for them is the intended use." The chat asks once per website
  before it reads the page [P docs/features/projects/import-website]. Trust and Safety takes DMCA
  notices and trademark complaints through lovable.dev/abuse [P docs/changelog].
- **Base44**: "You must be the rightful owner of the website you start from, or have full rights to
  its content, design, and images." This covers **Design only** too [P Base44 first-prompt].
- **Replit, Bolt, v0**: their import, start and screenshot pages contain no ownership language
  [P, the pages cited above].
- **Dyad Pro, Same.new and the open cloners** sell cloning as the feature, and show no rule [U].

**Why the line exists: abuse.** In Guardio Labs' April 2025 "VibeScamming" test, Lovable scored
1.8/10 for resisting phishing prompts. It built a Microsoft-style login page and published it on
`lovable.app` [S https://thehackernews.com/2025/04/lovable-ai-found-most-vulnerable-to.html].
Proofpoint reported "tens of thousands" of malicious Lovable-built sites since Feb 2025, including
Microsoft logins built from phishing kits and a UPS-themed card harvester [S
https://www.darkreading.com/cyberattacks-data-breaches/cybercriminals-abuse-vibe-coding-service-malicious-sites].
Lovable then ran every new site through Guardio's Safe Browsing engine at creation [S
https://tech.yahoo.com/cybersecurity/articles/lovable-adds-safe-browsing-engine-200049840.html].
Lightbulb apps are hosted on the customer's own Vercel at `<name>.apps.thelightbulb.company`, so a
look-alike login page would be served from our domain.

**The law the line rests on (not legal advice).** US copyright does not cover "any idea, procedure,
process, system, method of operation, concept" [P 17 U.S.C. §102(b),
https://www.law.cornell.edu/uscode/text/17/102]. A scheduling tool that works like Calendly is
fine. Calendly's text, images, logo, name and code are its own expression, and its brand is a
trademark. Blogs that cover cloning agree: the risk sits with whoever publishes someone else's
copy, branding or logic [S https://www.codecademy.com/resources/blog/can-you-clone-a-website-legally].

**What this means for Lightbulb (used in §5):**

- **"Like X" (named product) is always allowed.** Lightbulb rebuilds the function in the company's
  own brand. It never uses X's name, logo, text or images.
- **Your own URL is fully imported** (G), after one confirmation per site in the thread, as Lovable
  does. If the workspace's verified domain matches, skip the question: the workspace already proves
  ownership.
- **Someone else's URL or screenshot is a reference** (Design only). Lightbulb takes the layout,
  the patterns and the set of features, never the words, images, logos or brand. The builder says
  that in one line.
- **Never** a page that imitates another organisation's sign-in, checkout or brand. That one is a
  refusal, not a switch. It is the single exception to "nothing is declined" (my call; Lovable
  now scans every new site for exactly this).

## 3. Proactive triggers: what Lightbulb already has, and what others do

**What exists on `origin/main` (code, not docs):**

| Part | What it does | Where |
|---|---|---|
| `suggest_idea` tool | The agent adds an idea for *this* company to the workspace's Ideas list, never as a chat card. Fields: title, why, up to three deliverables, size (project/task/quick), emoji. The tool description: "only when you notice a concrete, company-specific job during real work… never a generic or routine one" | `apps/infra/hermes/plugins/lightbulb/__init__.py:141-163`; service `apps/backend/core/services/ideas.py:410` |
| Limits | At most 10 open suggestions per workspace (`SUGGESTION_CAP`). Duplicate titles are rejected, against both the catalog and earlier suggestions | `ideas.py:54`, `ideas.py:418-424` |
| Let's do it | Starting an idea creates the Work item and posts the person's own message in their DM with the agent: the idea, "Let's do it!", and "Tracked in Work" | `ideas.py:1-10` (module doc); skill `references/ideas.md` |
| Catalog | 35 YAML ideas. Several are already apps: `website-rebuild`, `landing-page-checkout`, `crm-setup`, `shopify-store-setup`, `stripe-subscriptions`; `unused-software-seats` is the SaaS audit | `apps/backend/ideas/*.yaml` |
| Automations | Signed webhook and schedule triggers seed a routine run. Connected apps: Stripe, Sentry, Linear, Attio, GitHub, Vercel, Google, Gmail | `apps/backend/core/services/inbound_triggers.py:461, 477, 888` |
| Workspace memory | Recall plus `workspace_memory_search` by topic, private and shared | `apps/infra/hermes/plugins/memory/agentcore/__init__.py:23` |

So the parts for origins B, C, D and F are already in place. An agent working in mail, Sheets or a
room can see the third Friday CSV merge, the fifth "where is my order?" email, or a renewal invoice.
What's missing is narrow:

1. **An idea can't yet say "this is an app".** "Let's do it" makes a Work item. For an app it should
   start the builder with the evidence attached.
2. **The rule for when an app is worth suggesting.** The existing rule ("concrete, company-specific")
   holds. For an app the "why" should carry the count and the source ("merged the same 3 CSVs on 4
   Fridays", "11 customers emailed for order status in October"). Both are visible in the agent's
   own work (Gmail search, memory) and line up with the "rots in silence" evidence in §1: count
   first, suggest second.
3. **Origin D arrives through Automations.** A customer email asking for a portal, a partner form,
   or a Stripe event is already a routine run. That run should be able to `suggest_idea` (it can)
   with the external party named.

**What others do (evidence that nobody does this):**

- **Lovable**: suggestion chips by the input, which now "stay visible when you refresh" [P
  docs/changelog]. Project monitoring checks *existing* projects for problems [P
  docs/features/project-monitoring]. Growth tools research "what would help your business" after
  the app exists [P docs/features/growth-tools]. Nothing proposes a new project from the company's
  work.
- **Airtable Omni**: offers "additional request ideas" inside an app you are building [S
  https://support.airtable.com/docs/using-omni-ai-in-airtable]. No unprompted suggestion found.
- **Notion Custom Agents** (Notion 3.3, Feb 2026): run on triggers someone defines (schedule, Slack
  message, database change, email). No suggestion feature found [S
  https://www.notion.com/product/custom-agents].
- **Microsoft Power Apps Plan designer**: closest in spirit. You describe a business problem and it
  proposes user stories, a data schema, and the apps, flows and agents to build [P
  https://learn.microsoft.com/en-us/power-apps/maker/plan-designer/plan-designer]. It still waits
  for the problem to be described.
- **Replit**: Plan mode turns your requirements into task lists. No proactive suggestions [P
  https://docs.replit.com/replitai/plan-mode].

**What this means:** every builder waits for an app request. Lightbulb's agents are already inside
the work that produces those requests. This is the one origin where Lightbulb starts ahead, and it
uses machinery already in production.

## 4. Personalization and market research before building

**Who has a discovery or research step:**

| Builder | Step | What it covers | Market research? |
|---|---|---|---|
| Stripe `starter-to-product` | "Discovery-first conversation": "ask for the name first", "one question at a time", palette and direction offered as 2–4 choices, then a summary of the brief and "Are you ready for me to start implementing this?" | Name, what and for whom, main workflow, look, "any references, constraints, must-haves, or must-avoids" | **No.** References come from the user [P https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_saas_amber-fox/prompts/starter-to-product.md] |
| Lovable Plan mode | Asks clarifying questions, can start subagents "to research your project", writes a plan you approve. Costs one credit plus research | The project | **No.** It researches the code [P docs/features/plan-mode] |
| Lovable design card | Up to 4 questions (palette, type, layout, content), first option preselected, **Skip all** | Look | No [P via `lovable-ux-deep.md`] |
| Lovable Ads / Growth | "Lovable researches what would help: competitors and their websites, public ad libraries, relevant searches" | Campaigns, SEO | **Yes, but after the app exists** [P docs/features/ads, docs/features/growth-tools] |
| Base44 | Asks only when the prompt leaves out "what the app does or who it's for". One question at a time, Skip, **Use a placeholder**. Plan mode tracks intent, audience, flows, design | The app | No [P Base44 first-prompt] |
| Replit Plan mode | Brainstorm, "pros and cons analysis", task list | The build | No web research mentioned [P Replit plan-mode] |
| Dyad Pro | Web search in the agent | Whatever the agent decides | Possible, not a step [P dyad.sh/pro] |
| Power Apps Plan designer | Business problem → user stories, schema, components | The solution | No [P MS Learn] |

**How they personalize from the company.** This is covered in `templates-kinds-of-apps.md` §3 and
not repeated here. In short: they read the URL, screenshot, sheet, Figma file, design system,
standing instructions and connected tools, and ask only for design and missing basics. Base44's
**Use a placeholder** is the one piece worth adding to the §3 design. It covers details "only you
know, such as your business name" without stopping the build [P Base44].

**What this means for "look into the market and build something custom for them".** No builder
surveyed does this before the build. Stripe's prompt is the most careful pre-build step, and it
still treats references as something the person brings. Lightbulb can do more because its agent
already has web search, the browser and the company's memory. The evidence also shows the cost:
every question before the first preview is friction. Lovable "does not make people plan. It makes
the agent ask", and only when the prompt is open-ended (`lovable-ux-deep.md` §9). The way to get
both:

1. **The default never waits.** The builder starts the first version from what it can read, as now.
2. **The offer is one chip on the builder's first reply**, shown only when it helps:
   *"Want me to look at what others in your market do first?"* It shows when the app faces
   customers (a site, a portal, a product with payments), when the request is "like X", or when
   the company is entering something new. It doesn't show for internal tools, forms or dashboards,
   where the company's own data is the specification.
3. **Research runs alongside the build, not before it.** A background task (web search plus the
   browser, the same tools the agent has today) looks at 3–5 comparable products or local
   competitors and returns **one card**: what each does, their prices if public, the 3 features
   they share, the one gap. Each row has **Add to my app** or **Skip**. A tapped row becomes the
   next change request on the preview that already exists. Nothing from a competitor's text or
   images is copied (§2 rule).
4. **Personalization questions follow Base44's rule.** Ask only what can't be read (who uses it, the
   one main action, a launch date for origin D). Each question has **Use a placeholder** or Skip.
   A product with payments follows Stripe's name-first rule: never invent a product name.
5. **The person can ask for research anytime** ("check what others charge"). It is the same
   background task.

## 5. Recommendation: §3 rewritten as origins × what happens

Proposed replacement for the opening table of §3 in `2026-10-28-apps-design.md`. The rows from
Preview to Later stay as they are, because they don't change by origin. "My call" marks what the
founder has not ruled on.

### 3. What the person experiences

An app starts in one of nine ways. Every one ends in the same place: a preview in Canvas, built on
the company's own accounts, changed by replying in the thread. What differs is who starts it, what
the builder reads first, and what it asks.

| Origin | Who starts it | Builder does first | It asks (each skippable, with **Use a placeholder**) | The person sees |
|---|---|---|---|---|
| **Ask** ("build us a crew schedule") | A person in a room | Reads the workspace brand, the thread, memory and any attachment, then builds | Nothing, unless the request leaves out who uses it or the one main action. A website with no brand to read gets the design card | "On it", then the preview, with one line on what it reads and does |
| **Outgrown sheet** | A person attaches a sheet, or an agent suggests it | Reads the columns and sample rows. Builds list, detail and edit on that data | "Who edits, who only views?" | Preview with their own rows in it, and the source named |
| **Repeated chore or question** | An agent, through `suggest_idea`, with the count in "why" | Nothing until **Let's do it**. Then builds from the evidence it cited | Nothing new: the idea already carries the why | An idea in the Ideas list ("Merged the same 3 CSVs on 4 Fridays → a one-click merge page"). After Let's do it, the preview in their DM with the agent |
| **External request** (customer, partner, event, launch, vendor gone) | Mail or an Automation run (agent suggests), or a person forwards it | Names the outside party and the date. Builds the smallest slice that meets it first | "When is it needed?" and "Who outside opens it?" Sharing defaults to **anyone with the link** for the external party only. *My call* | Preview, plus one line: "Ready for <customer> by Fri; sharing: link only" |
| **"Like X"** (named product) | A person | Builds X's function in the company's brand | Nothing. The research chip is offered | Preview, plus one line: "Works like X; your brand, none of X's words or images" |
| **Someone else's URL or screenshot** | A person | Uses it as a reference for layout and features only | Nothing. The research chip is offered | The same one-line rule. The reference shows beside the preview in the recording |
| **Our own site or app** (Wix, Webflow, another builder, a repo) | A person, or the `website-rebuild` idea | Checks ownership: a verified workspace domain passes, otherwise one confirmation in the thread. Then imports pages (≤10 per request) or code, and compares side by side at desktop and phone size | "Bring the records and users over now, or later?" | **Ready / Still needed** lines (Lovable's pattern), then the preview. Webflow and Framer sites are edited in place (§4) |
| **Replace a SaaS we pay for** | A person, or the `unused-software-seats` agent finding the renewal | Reads what the company actually uses (exports, connected data), and builds that part only | "Which parts do you use daily?" if it can't be read | Preview, plus the SaaS's monthly cost beside the app's on its Canvas entry. The old tool is never cancelled without approval |
| **Template or teammate's app** | A person ("give my team Ana's tracker") | Copies the app into a new App with its own accounts and empty data. The original is never touched | Nothing. A template skips the design card | A new Canvas entry, "copied from …" |

**The optional research step.** On customer-facing apps and "like X" requests, the builder's first
reply carries one chip: "Look at what others in your market do first?" Tapping it starts research
alongside the build. It comes back as one card of 3–5 comparable products, each with **Add to my
app** or **Skip**. A chosen row becomes the next change. The preview never waits for it. Anyone
can ask for it at any time. *My call: never shown for internal tools, forms or dashboards.*

**Copying.** The builder copies in full only what the company owns. For anything else it copies
function, layout and features, never another company's name, logo, words or images, and says so
in one line. It never builds a page that imitates another organisation's sign-in, payment or
brand. That is the one thing it declines.

**Where each app came from** is recorded on the App (origin, the evidence or request, the outside
party, the date). The 30-day unused notice in the Later row uses it: "Built for <customer>'s
launch on 3 Nov; unused since." This is the honest check against the "rots in silence" fate.

### What this needs that doesn't exist yet (for §8 or §11)

1. A `kind: app` flag on suggested ideas, so that Let's do it starts the builder instead of a plain
   Work item. The rest of the `suggest_idea` path is unchanged (`ideas.py:410`).
2. Website import with ownership checked against the workspace's verified domain.
3. The research card (a background task plus a card type). It can ship after the first build:
   the default path does not depend on it.
4. Origin metadata on the App record.

Skipped on purpose: a "choose how to start" picker. No builder makes people pick a type
(`lovable-ux-deep.md` §8), and in a room the request or the idea already says which origin it is.

---

## Sources (all fetched 2026-10-29)

**Lightbulb (origin/main):** `apps/infra/hermes/plugins/lightbulb/__init__.py:141-167`;
`apps/backend/core/services/ideas.py:1-60, 146-175, 410-440`; `apps/backend/ideas/*.yaml` (35);
`apps/backend/core/services/inbound_triggers.py:461, 477, 888`;
`apps/infra/hermes/plugins/memory/agentcore/__init__.py:23`;
`apps/infra/hermes/skills-src/lightbulb/references/ideas.md`, `platform-primitives.md`.

**Primary:**
- Lovable docs (from https://docs.lovable.dev/llms-full.txt): features/projects/import-website,
  features/projects/import-project, features/projects/remix, features/plan-mode, features/ads,
  features/growth-tools, features/project-monitoring, changelog.
- https://lovable.dev/blog/customer-stories ; https://lovable.dev/blog/exprealty ; https://lovable.dev/templates
- https://support.bolt.new/building/start-project ; https://support.bolt.new/integrations/figma
- https://v0.app/docs/screenshots
- https://docs.replit.com/build/import-from-providers ; https://docs.replit.com/replitai/plan-mode
- https://docs.base44.com/Getting-Started/starting-from-your-first-prompt
- https://www.dyad.sh/pro
- https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_saas_amber-fox/prompts/starter-to-product.md
- https://learn.microsoft.com/en-us/power-apps/maker/plan-designer/plan-designer
- https://www.softr.io/use-cases/google-sheets-client-portal ; https://www.softr.io/client-portal
- https://www.law.cornell.edu/uscode/text/17/102

**Secondary:**
- https://thehackernews.com/2025/04/lovable-ai-found-most-vulnerable-to.html
- https://www.darkreading.com/cyberattacks-data-breaches/cybercriminals-abuse-vibe-coding-service-malicious-sites
- https://tech.yahoo.com/cybersecurity/articles/lovable-adds-safe-browsing-engine-200049840.html
- https://knowara.com/ai-tools/coding/same-new-review/ (same.new itself returned 429)
- https://old.baoyu.io/blog/clone-page-by-url-v0dev
- https://www.codecademy.com/resources/blog/can-you-clone-a-website-legally
- https://advancedweb.hu/intro-to-appsheet/
- https://support.airtable.com/docs/using-omni-ai-in-airtable ; https://www.notion.com/product/custom-agents
- Reddit/HN quotes as fetched in `2026-10-27-small-software-voice-of-customer.md` and
  `2026-10-27-small-software-one-off-evidence.md`.

**Unverified:** Dyad Pro's cloning rules (none found); whether v0 still turns URLs into screenshots;
Same.new's terms (site returned 429).
