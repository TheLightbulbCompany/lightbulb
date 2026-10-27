# 14 · Directories and listings

## Purpose

Moves the stage-1 goal (ten founding teams set up, inbound launched) by putting Lightbulb where Claude Code and Codex users already look for tools. Serves the business plan's "Where they come from" (the plugin listings, Prasiddha) and stage 2 "Rented" (plugin marketplaces, the App Store). Method: the `directory-submissions` skill. Its tier list is cut hard: the product is a B2B team tool with one developer surface, not an AI toy, so most of its 250 directories are skipped.

## What we know

| Fact | Source | Confidence |
|---|---|---|
| The public repo `TheLightbulbCompany/lightbulb-plugin` has a Claude marketplace file, a Claude and a Codex `plugin.json`, a remote MCP server (`https://api.thelightbulb.company/mcp`), two skills and a Claude-only hooks/mod folder. **No topics, no LICENSE file.** The repo is a CI copy of the private monorepo | `gh api` of the repo, 13 Oct | High |
| Anthropic runs a plugin directory. Submit at `claude.ai/directory/manage` ("Submit new", "Plugin bundle"): repo URL, `claude plugin validate`, data-handling answers, contact email, a security scan, then a reviewer publishes. Needs a public repo, GitHub connected, push access. Limit 10 submissions per org per day. No review time is published | [Anthropic submit docs](https://www.claude.com/docs/plugins/submit) | High (first party; I could not open the form itself) |
| OpenAI: "To publish a plugin for public use, submit it through the plugin submission portal"; the listing is shared by ChatGPT and Codex. **Plugins with lifecycle hooks "aren't eligible"**. A remote HTTPS MCP is submitted through "With MCP". The portal URL is not in the page I read | [Codex build plugins](https://developers.openai.com/codex/plugins/build) | High for the rules, unverified for the portal URL |
| `codex plugin marketplace add owner/repo` works from GitHub shorthand today, so our own repo is already a Codex marketplace | same page | High |
| awesome-claude-code: 55.5k stars, 2,107 commits. A recommendation goes through an issue template ("Recommend a new resource"), not a pull request; an automated review checks relevance, docs, maintenance and licence | [repo](https://github.com/hesreallyhim/awesome-claude-code), [contributing guide mirror](https://www.mintlify.com/hesreallyhim/awesome-claude-code/contributing) | Medium (the repo page did not show the guide; the mirror did) |
| Show HN: must be something people can run; you must be around to discuss; no landing pages or sign-up pages; "avoid requiring signups or emails"; no asking friends to upvote | [HN Show HN rules](https://news.ycombinator.com/showhn.html) | High |
| G2: B2B software only, **no alpha or beta**, extensions of an existing product are usually rejected. Free profile, claim review 1–3 business days. The free plan carries no link to your site. Capterra's "get listed" button opens G2's form | [G2 docs](https://documentation.g2.com/help/docs/finding-or-listing-a-product-on-g2), [SaaSCity, 7 Oct](https://saascity.io/directories/g2) | Medium |
| AlternativeTo: free queue with no date, $5 priority review (not used). New links likely nofollow. SaaSHub: followed link only after free verification | [SaaSCity AlternativeTo](https://saascity.io/directories/alternativeto), [SaaSCity SaaSHub](https://saascity.io/directories/saashub) | Low–medium (third-party, sources conflict) |
| Most AI-tool directories now charge: There's An AI For That from $49; Toolify about $99 for a fast listing; Futurepedia $247–$497, its submit page reported 404 | [Orangebot](https://orangebot.ai/blog/where-to-submit-your-ai-tool-2026), [SaaSCity](https://saascity.io/directories/futurepedia) | Low (conflicting, third-party) |
| thelightbulb.company, 13 Oct: `/`, `/privacy`, `/terms`, `sitemap.xml`, `llms.txt` return 200. `/pricing`, `/request-setup`, `/alternatives` return 404. Homepage H1 is still "Your team and your agents, in one place." | `curl` | High |
| The Mac app ships as `desktop-v0.4.847` on the **private** repo's releases. A directory needs a public download URL on our site; I did not verify one exists | `gh release list` | Medium |

## Findings (ranked by impact)

1. **The plugin surfaces are the only listings that reach the wedge.** The plan names Claude Code and Codex users who install plugins. Anthropic's directory and the awesome list are free, read by exactly those people, and the plugin already exists.
2. **The Codex public directory may reject us.** The plugin has a hooks folder and the Codex rule bars hooks. Our own marketplace route already works, so the fix is a hooks-free Codex build for the portal. Unverified whether the Codex manifest references the hooks; Forge should check.
3. **The company site is not ready for review sites.** No pricing page, no setup door, no alternatives page. G2 and the skill's Rule 2 both say wait. G2 waits until the site gate passes. Submitting before `/pricing` exists would waste a first impression.
4. **Free AI-tool directories are mostly gone.** Paying $49–$497 per listing for generic traffic is not in a $0 stage-1 budget. Skipped, with one exception: submit free tiers only where the form is free.
5. **Show HN and Product Hunt are the launch workstream (07), not this one.** Listing a Product Hunt page is cheap; launching is gated to early December/January by the plan. Show HN's no-sign-up rule is a real risk, because the plugin and the apps all need a Lightbulb account.
6. **The name collides.** TheLightbulb.ai and a UK lighting retailer will outrank us on "lightbulb". Every listing carries the company name and the full descriptor.

## Recommendation

List in this order: the Anthropic directory, our own plugin repo cleaned up (topics, licence, README top lines), the Codex portal after a hooks check, awesome-claude-code, then G2 and SaaSHub/AlternativeTo once the site has `/pricing` and a comparison page. Product Hunt: claim the product page now, launch later under workstream 07. Skip every paid AI directory, the 40-site "AI directory" sweep, press-release sites, social bookmarking and local-business sites.

Rejected: paid directories (budget, and poor fit); BetaList and the 15 minor launch sites (a pre-launch audience we do not need, and their one-day spike would burn the Show HN story); a G2 review drive now (36 of 46 workspaces have one member, and the beta bar applies).

## Deliverable

### 1. Ranked list

Reach/backlink/AI signal: H/M/L. All free.

| # | Place | Submit at | Needs | Review time | Gives | Owner |
|---|---|---|---|---|---|---|
| 1 | **Anthropic plugin directory** (Claude Code and Cowork) | `claude.ai/directory/manage` | Public repo, `claude plugin validate` clean, data-handling answers, contact email | Not published; unverified | Reach H (the wedge, inside Claude Code), backlink L, AI signal M | Prasiddha |
| 2 | **Our plugin repo as a marketplace** (already live) | GitHub repo settings | Topics `claude-code`, `claude-code-plugin`, `codex`, `mcp`, `ai-agents`, `team-workspace`; a LICENSE; the disambiguating first line in the README | Same day | Reach M (repo search, and the install commands in every post), backlink M, AI signal H (READMEs are what AI tools quote) | Prasiddha |
| 3 | **Codex / ChatGPT plugin directory** | OpenAI plugin submission portal (URL linked from the [build page](https://developers.openai.com/codex/plugins/build); unverified) | Hooks-free package, remote HTTPS MCP via "With MCP", auth per their docs | Unverified | Reach M–H, backlink L, AI signal M | Prasiddha |
| 4 | **awesome-claude-code** | Issue template "Recommend a new resource" in the repo | Public repo, clear docs, a licence, actively maintained | Automated; unverified | Reach H (55k stars), backlink H (GitHub), AI signal H | Prasiddha |
| 5 | Other Claude plugin lists (awesome lists for Claude plugins/MCP; community marketplaces) | Each repo's contributing page | Same as #4 | Varies | Reach M | Pulse finds and drafts the list of live ones, Prasiddha submits. I did not verify individual repos |
| 6 | **MCP registries** (the remote MCP at `api.thelightbulb.company/mcp`) | The official MCP registry and Glama; check each submit page | Public server URL, description | Unverified | Reach M, AI signal M–H. Skill rates Glama grades as an LLM source | Prasiddha |
| 7 | **G2** (Capterra opens from the same form) | G2 product submission form | Beta label gone, B2B, `/pricing` live, a company email | 3–5 business days (estimate) + 1–3 days claim | Reach L now (zero reviews), backlink L on the free plan, AI signal M–H | A founder (Aden) |
| 8 | **AlternativeTo** (as an alternative to Slack, Microsoft Teams, Grok Bot) | `alternativeto.net/manage-item/` (needs an account) | Description, logo, screenshots | Free queue, no date | Reach M (people search "Slack alternative"), backlink L (nofollow), AI signal H | A founder (Leo) |
| 9 | **SaaSHub** | `saashub.com` add software | Free; verify the domain for a followed link | Unpublished | Reach L–M, backlink M, AI signal M | Pulse drafts, Leo submits |
| 10 | **Product Hunt: product page only** | `producthunt.com` | Maker account (Prasiddha owns it), 5 screenshots, 60-second demo | The launch itself waits for workstream 07 | Reach L until launch, AI signal M | Prasiddha |
| 11 | **Hacker News Show HN** | `news.ycombinator.com/submit` | See rules below | None | Reach H, backlink nofollow, AI signal H | Prasiddha |
| 12 | **Mac app on Mac directories** | MacUpdate and similar: submit pages unverified | A public, notarised DMG link on our site (not the private release URL), icon, screenshots | Unverified | Reach L, backlink M | Pulse finds the live ones first; Prasiddha |
| 13 | **App Store** | App Store Connect | Ships with item 6 (record 6788944100) | Apple review | Reach M, AI signal M; `aso` skill when it ships | Prasiddha |
| 14 | Crunchbase, LinkedIn company page, Wikidata | Each site | Company facts, the descriptor | Same day to days | AI signal H (the skill ties these to AI corpora), backlink M | Aden |

**Show HN rules we must meet:** a title starting "Show HN"; the thing can be run; no landing page; no asking friends to upvote; we stay in the thread. The sign-up barrier is the risk. The public session page is the only no-account look (D4); nothing more is built for launch.

**Skipped on purpose:** TAAFT, Futurepedia, Toolify and similar paid AI directories ($49–$497); BetaList and the minor launch sites; press-release, article, local-business and social-bookmark sites; any directory under DR 30 with no editorial gate.

### 2. The description and tagline (use verbatim)

**Name field:** Lightbulb, by The Lightbulb Company

**Tagline (9 words):** Your team and your AI employees, in one place.

**Descriptor (use after the name wherever a short line fits):** Lightbulb, the team workspace with AI employees, by The Lightbulb Company.

**Description (49 words):**
> Lightbulb is a team workspace where AI employees work alongside your people. Mention a CEO, Engineering, Finance or Growth agent in a channel; the result comes back where everyone can see it, approve it or pick it up. Shared Claude Code and Codex sessions included. By The Lightbulb Company.

Per-surface openers (the rest stays the same; do not paste identical text everywhere):
- Plugin surfaces: "The Lightbulb plugin lets Claude Code and Codex share a session with your team, and read and send messages in your Lightbulb workspace. Lightbulb is the team workspace with AI employees, by The Lightbulb Company."
- Alternative sites: "Lightbulb is a team workspace like Slack, with AI employees as members. Not related to TheLightbulb.ai."
- Hermes and Paperclip may be named as "built on", never as the product's name. Listings quote our own aggregate numbers only, vetted by a founder first.
- Always add the URL `thelightbulb.company` with UTM. Never use "multiplayer", "AI-native", "bot", "orchestration".

### 3. Asset checklist

| Asset | Spec | Status |
|---|---|---|
| Logo | SVG, PNG 1024x1024 square, 512, 256, 128; favicon; one on light, one on dark | Produce from the brand file (unverified where it lives) |
| Screenshots | 1920x1080, five: a channel with a mention and the result; an approval card; a shared Claude Code session; Library; the phone app. Founder workspace only, nothing customer-related in frame; a founder vets each screenshot and each number before it is posted (B4) | To produce |
| Gallery for Product Hunt | 1270x760 versions of the same five | To produce |
| Demo video | 60 seconds, captions, one real handoff, no music. Hosted on our site and YouTube | Workstream 13 or Demo Lab; to produce |
| Plugin repo | topics, LICENSE, README top lines carrying the descriptor, a short GIF of `/lightbulb:share` | Prasiddha |
| Social card | 1200x630 | To produce |
| Site pages | `/pricing`, `/request-setup`, one comparison page, FAQ markup | Blocked on workstreams 10 and 11 |
| Public Mac download link | A stable URL on our site, not a private-repo release | Forge to confirm (task 3) |

### 4. Four-week calendar (14 Oct to 10 Nov)

| Week | Task | Owner |
|---|---|---|
| 1 (14–20 Oct) | Add topics, LICENSE and README top lines to the plugin repo. Run `claude plugin validate`. Confirm whether the Codex manifest uses hooks. Create Crunchbase, LinkedIn company page. Pulse drafts all copy from section 2 | Prasiddha (repo, validate); Aden (company pages); Pulse (drafts) |
| 2 (21–27 Oct) | Submit to the Anthropic directory. File the awesome-claude-code issue. Submit to MCP registries. Record the 60-second demo | Prasiddha; Pulse drafts, Prasiddha submits |
| 3 (28 Oct–3 Nov) | Submit to the Codex portal (hooks-free build if needed). Claim the Product Hunt page. Site gate check: `/pricing` and a comparison page live? | Prasiddha; Aden checks |
| 4 (4–10 Nov) | If the site gate passes: G2, AlternativeTo, SaaSHub, with a founder submitting each. Log every URL, status and date in the tracker (CSV from the skill's template, kept in the founders' workspace). Verify each backlink with `curl -sIL` | Aden (G2), Leo (AlternativeTo, SaaSHub); Pulse logs |

Nothing is published without a named founder's sign-off. Pulse researches and drafts only. Show HN and the Product Hunt launch follow workstream 07's dates, not this calendar.

### 5. UTM convention

`https://thelightbulb.company/?utm_source=<listing>&utm_medium=directory&utm_campaign=listing-2026q4&utm_content=<surface>`

- `<listing>`: `anthropic-directory`, `plugin-repo`, `codex-directory`, `awesome-claude-code`, `mcp-registry`, `g2`, `alternativeto`, `saashub`, `producthunt`, `hn`, `macdirs`, `appstore`, `crunchbase`.
- `<surface>`: `listing`, `readme`, `comment`.
- Lowercase, hyphens, one source per listing. The README link uses `plugin-repo`. App Store uses its campaign-token field, not a query string.
- The setup form's "How did you first hear about us?" gets the same list as options, so a team that skips the UTM is still counted.

### 6. Measures

Both tied to the plan's Monday numbers (setup requests, trial starts, by source):

| Measure | Source | Target by 10 Nov |
|---|---|---|
| Listings live, with backlink verified | tracker | 6 of the first 9 |
| Plugin installs | GitHub traffic/clones on the plugin repo (proxy only; Claude Code gives no install count that I verified) | Baseline |
| Visits by `utm_source` | PostHog | Baseline |
| **Setup requests by listing source** | `setup_requested` event (item 7) + the setup form | 1 or more |
| **Trial starts by listing source** | sign-up with the UTM or "first heard" answer | 3 or more |
| AI-engine mentions | Monthly check: ask ChatGPT, Claude and Perplexity "team workspace with AI employees" and "Slack alternative with AI agents"; log whether and how we appear, and whether the company name is right | Baseline |

Honest expectation: directories will not produce ten teams. The two plugin listings are the only ones that might produce a setup request in 90 days. The rest buy name recognition and AI citations. Judge them at the week-6 check and drop any with zero sessions.

## Build tasks (Forge-ready)

1. **Plugin repo hygiene.** Today: no topics, no LICENSE; README opens with "Lightbulb for Claude Code and Codex". Change: add topics, an MIT LICENSE file, and a first line with the descriptor. The change goes in the monorepo's `packages/claude-plugin/` because CI overwrites the copy. Why: awesome-claude-code and the Anthropic review both check licence and docs. Measure: `claude plugin validate` clean; repo shows topics.
2. **Hooks-free Codex package.** Today: the plugin folder has a hooks directory. Change: check `.codex-plugin/plugin.json`; if it references hooks, ship a second Codex manifest without them. Why: the OpenAI portal bars hooks. If the portal is closed to us or the hooks rule disqualifies the plugin, launch without the Codex directory; the `codex plugin marketplace add` route stays. Measure: portal accepts the upload, or the listing is dropped.
3. **Public Mac download URL.** Today: DMGs are on a private repo's releases. Change: confirm a stable public download route on thelightbulb.company and show it on a `/download` page. Why: Mac directories need a link that works for anyone. Measure: `curl -sI` returns the DMG.
4. **UTM capture.** Part of item 7: store `utm_*` on sign-up and the setup request. Measure: setup requests carry a source.
5. **Pages the review sites need**: `/pricing`, `/request-setup` and one comparison page. Owned by workstreams 10 and 11.

## Founder decisions (13 Oct 2026)

- E11: MIT licence on the plugin repo.
- E12: Prasiddha owns the Product Hunt maker account.
- E13: Launch without the Codex directory listing if it is closed or its hooks rule disqualifies the plugin.
- E14: G2 waits until the site gate passes.
- B10: Hermes and Paperclip may be named as "built on", never as the product's name.
- B4: Our own numbers and founder-workspace screenshots, each vetted by a founder before it is posted.
- D4: The public session page is the no-account look for Show HN; nothing more for launch.

## Sources

- [Anthropic: submit a plugin](https://www.claude.com/docs/plugins/submit)
- [OpenAI: build plugins for Codex](https://developers.openai.com/codex/plugins/build)
- [awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) and [its contributing guide (mirror)](https://www.mintlify.com/hesreallyhim/awesome-claude-code/contributing)
- [HN Show HN rules](https://news.ycombinator.com/showhn.html)
- [G2: finding or listing a product](https://documentation.g2.com/help/docs/finding-or-listing-a-product-on-g2); [SaaSCity: G2](https://saascity.io/directories/g2), [AlternativeTo](https://saascity.io/directories/alternativeto), [SaaSHub](https://saascity.io/directories/saashub), [Futurepedia](https://saascity.io/directories/futurepedia)
- [Orangebot: AI directory costs](https://orangebot.ai/blog/where-to-submit-your-ai-tool-2026)
- Product Hunt timing advice: third-party guides only, not the platform's own rules (unverified)
- First party: `gh api` of `TheLightbulbCompany/lightbulb-plugin`, `gh release list`, `curl` of thelightbulb.company (13 Oct); `docs/business-plan.md` §8 and Go To Market; `.agents/product-marketing.md` v7; `docs/marketing/01-marketing-plan.md`
