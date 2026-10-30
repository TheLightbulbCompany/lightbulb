# How AI app builders make generated software effortless: workflows, templates, hidden stages

Research note, 29 Oct 2026. Tags: **[P]** primary (vendor docs, vendor GitHub, vendor CLI), **[S]** secondary, **[U]** unverified. Each [P] cites a numbered source at the end; every source was fetched 2026-10-29. Web search, web fetch, `gh`/raw GitHub and the local Stripe CLI only. No browser.

This note does not repeat the stack, pricing and hosting facts already in `2026-10-27-small-software-stack-teardown.md`, the Floot MCP tool list in `2026-10-28-small-software-specific-floot-valtown.md`, or the Dyad and Naïve reads. Lovable appears only for comparison.

## Short answer

The builders that hide infrastructure best keep two stages (building and live), push schema but never data on publish, fence the agent off live data, and never roll live data back with code [P1][P3][P6][P9][P10]. Template choice is collapsing to a default (bolt.diy dropped its LLM picker on 11 Oct 2026; Stripe ends on a `default: true` variant) [P7][P11]. The agent's workflow ships as text inside the starter [P7a][P12].

## 1. Replit Agent

- **Loop.** Agent builds against a private dev preview; Publish creates "a production deployment... at its own URL" (package, provision, security checks, build, promote). [P4]
- **Stages.** Two, named plainly: the development database is "where you and Agent experiment while building"; production "stores the live data that powers your published app". "Agent can modify the development database but can't touch production." On Helium, publish creates production automatically and applies Agent's structural changes; dev data is not copied; publishing may cause "a brief downtime". A "deployment preview" is "a temporary, isolated copy of your production environment". [P1][P2]
- **Workflow.** Plan mode plans "without modifying your project's code or data" and ends in Revise / Cancel / **Build here** / **Build in background** (background changes wait for review) [P5]. App Testing: "Agent navigates through your application like a real user would", chooses when to test, then "automatically fixes any issues"; off in Free Mode [P13][P14]. Checkpoints are automatic and hold files, the dev database and "the complete conversation history"; rollback never touches production [P3]. No template mechanism documented [U].
- **Ownership.** Replit runs all of it on its own cloud and bills usage (teardown).

## 2. Bolt.new and bolt.diy

- **Loop (bolt.new).** Prompt → WebContainer preview → Publish to bolt.host or Netlify; only owners publish; security audit from the Publish menu. [P15]
- **Stages.** There is one. The Bolt Database docs describe no dev/prod split. Published projects' databases are no longer auto-paused. You can "claim your database into your own Supabase account". [P16] Version history restores files only: "Restoring to an earlier project version will not change your current Bolt or Supabase databases." [P6]
- **Plan mode** (Aug 2026) "replaces Discussion Mode"; a template marketplace and team templates exist. [P15]
- **bolt.diy (open source) starters.** The list in `app/utils/constants.ts` has 14: Expo, Astro, Next.js+shadcn, Vite+shadcn, Qwik, Remix, Slidev, SvelteKit, Vanilla Vite, **Vite React (default)**, Vite TS, Vue, Angular, SolidJS. All are GitHub repos under `blueberry-llm/*`. [P8]
- **How it picks (current).** `selectStarterTemplate.ts` uses no LLM. Information questions with no build verb get `blank`. A named framework is matched by regex (e.g. `/\bnext\.?js\b/` → Next.js shadcn only if "shadcn" is also present, else blank). "script / snippet / one-off" gets `blank`. Everything else gets `Vite React`. The commit that removed the LLM picker (d8ccd5a6, 11 Oct 2026) is titled "Speed up template responses". [P7]
- **How it picked (before).** An LLM prompt read "IMPORTANT: Vite is preferred. Only choose shadcn templates if the user explicitly asks" and returned `<templateName>` plus a title. [P17]
- **Template contract.** `.bolt/prompt` is injected as "TEMPLATE INSTRUCTIONS"; `.bolt/ignore` files become READ-ONLY. After import: "Record a brief plan with observable completion criteria, then implement it now. Do not stop after a plan, ask whether to proceed... Repair failures and continue until the requested app is implemented, verified, and running." [P7]
- **System-prompt rules.** "Use Supabase by default", "Always enable RLS", every DB change as a migration plus a query, every screen with loading/error/empty/success states [P12]. "ALWAYS READ FILES BEFORE EDITING" (patches carry `expectedRevision`); poll a process until it exits; confirm the dev server from its output; "Treat tool output as the source of truth. Report failed commands as failed." [P18]
- **Ownership.** Bolt Cloud is Bolt's. Supabase, if chosen, belongs to the user. [P16]

## 3. v0 (Vercel)

- **Loop.** Chat → sandbox preview → Publish "promotes the current work through the project's configured Git and Vercel workflow": a PR into the base branch with GitHub, else a direct deploy. [P19]
- **Stages.** There are two, and they are Vercel's own: previews come from working branches with "unique URLs" that "do not change the project's production URL". Env vars are scoped Production / Preview / Development. v0 has no rollback button. Rollback is in the Vercel deployment inspector, or you "restore an earlier chat version, then publish again". [P19]
- **Databases.** You add one from Settings → Integrations → Create (the Vercel Marketplace), or by asking v0. This "provisions a new user account on that service". v0 "can generate and run SQL" and documents no migration workflow. [P20] The Neon Vercel-managed integration bills "through your Vercel invoice". **Preview branching is opt-in**: when enabled it creates "an isolated Neon branch (copy-on-write) for every Vercel Preview Deployment", injected at deploy time. Production uses the main branch. [P21] Community reports say v0 chats use production env values and migrations land on the production branch [S].
- **Templates.** There is a gallery in 13 categories (Apps & Games, Dashboards, AI, Agents…), each with "Duplicate a template". Design Systems 2.0 is the brand-consistency mechanism. [P22][P23]
- **Workflow.** Errors fixed "automatically as part of the generation loop"; **Fix with v0** "sends the error logs to v0" on a failed deploy; browser use opens the app, uses it and posts screenshots; terminal autonomy Ask / Auto / Full. [P24]
- **Ownership.** The Vercel project and the Marketplace database sit in the user's Vercel team and are billed there. [P20][P21]

## 4. Base44

- **Stages.** There is one by default and an opt-in second. Turning on **Test Data** creates "a new, empty database". "Production and test data are completely separate", with "no automatic sync... in either direction". The published app "always uses production data". A testing link shows unpublished changes against test data. **The AI chat "works with production data by default."** [P9] The docs don't say whether schema changes reach both [U].
- **Versions.** A checkpoint is "a saved version of an app" (code, entity schemas, backend functions). Any checkpoint can be deployed via `checkpoint_id`. [P25] Data version history and restore are a separate feature for higher plans [S]. A user request asks for the history to mark which version is live, which suggests it doesn't today [S].
- **Ownership.** Base44 runs a managed NoSQL backend (teardown).

## 5. Google AI Studio Build (and Firebase Studio)

- **Firebase Studio is closing.** New sign-ups stopped 22 Jun 2026 and it shuts down 22 Mar 2027. Google points browser prompt-to-app users to AI Studio. [P26]
- **Loop.** Prompt → live preview → Publish → Cloud Run URL. The Antigravity harness "now powers Build mode" with "verified execution"; build failures are fixed by prompting. App Gallery remix and "I'm feeling lucky". The agent can "automatically provision and set up Firebase, including Firestore". [P27]
- **Stages.** One. The docs describe no preview/production or dev/prod split, and no checkpoints (GitHub sync is the history). [P27][P28]
- **Ownership.** The Starter Tier deploys up to two services with no Cloud project or billing. Beyond that you need "a Google Cloud project linked to your AI Studio account and billing enabled": **the user's own GCP**. [P28]

## 6. Floot (delta only)

- The tools are covered in the earlier note: `typecheck`, `run_tests` (Jasmine), `screenshot_preview`, `get_logs`, `create_checkpoint`, `publish_app`. Preview logs cover the last hour. "Published app's production logs" are kept for 90 days. [P29]
- No primary source says whether preview and live share a database. One aggregator says they do [S, U].

## 7. Stripe Projects templates

- **Registry.** `stripe/projects-template-registry` holds **32 YAML manifests**, no code. Guided categories: **SaaS / One-time payment / Static site** (else "Something else"). Most are Stripe's Next.js SaaS and one-time-payment variants; the rest come from Customer.io, Clerk, Daytona, E2B, Kernel, HeyGen, PostalForm, Shopify, PostHog and Netlify. [P11]
- **Manifest.** `template`, `variant`, `default`, pinned `ref`, `install_command`, `services` (e.g. `vercel/project`, `clerk/auth`, `neon/postgres`), `next_steps`, and `tier_plans` mapping three traffic tiers to provider plans (Neon/free → Neon/launch). [P11]
- **How `stripe projects build` picks.** Search, or the guided flow: app type → framework → expected traffic; it "filters the registry to matching templates and presents a recommendation" priced per service. Then: copy at the pinned commit, install, provision, print next steps; the template reads the provisioned env vars "so that everything works immediately after setup". [P30]
- **Starter-to-product prompt.** `next_steps` prints `claude "Help me turn this into a real product. Follow prompts/starter-to-product.md."` Steps: audit named files; discovery, "ask one question at a time", never invent the product name, offer 2–4 palette/design directions; synthesis and "Are you ready for me to start implementing this?"; phases landing → dashboard/auth/checkout → business logic, keeping the wiring intact (links Anthropic's frontend-design skill); verify `/api/health` and the main flow. `AGENTS.md` adds non-negotiables (durable Stripe objects only via setup scripts, keep routes and `/api/health`). [P7a][P7b]
- **Ownership.** Every service is a real account owned and billed to the developer through Stripe. [P30]

## Comparison

| | Prompt → live URL | Stages the user sees | Data on publish | Template choice | Agent loop | Rollback | Infra owner / payer |
|---|---|---|---|---|---|---|---|
| Replit | Agent builds → Publish → `.replit.app` [P4] | Dev + prod DB, named plainly; deployment preview [P1][P2] | Schema pushed, data stays; Agent can't touch prod [P1] | Not documented [U] | Plan mode (approve) → build → browser App Testing → auto-fix [P5][P13] | Checkpoint = code + dev DB + chat; never prod DB [P3] | Replit [teardown] |
| Bolt.new | Prompt → WebContainer → Publish (bolt.host/Netlify) [P15] | One [P16] | Same DB [P16] | Marketplace + team templates [P15] | Plan mode; autofix; security audit [P15] | Files only, DB untouched [P6] | Bolt Cloud, or user's Supabase [P16] |
| bolt.diy | Prompt → template import → build → run [P7] | One (local) | n/a | Regex + default Vite React (LLM picker removed 11 Oct) [P7][P17] | "Plan with completion criteria, don't stop, verify" [P7][P18] | Chat history | User |
| v0 | Chat → branch preview → Publish (PR → merge) [P19] | Preview/production (Vercel's) [P19] | Shared DB unless Neon preview branching opted in [P21] | Gallery, 13 categories; design systems [P22] | Auto error-fix loop, Fix with v0 from deploy logs, browser use [P24] | Vercel inspector or old chat version [P19] | User's Vercel team [P20][P21] |
| Base44 | Chat → preview → publish [P9] | One; Test Data opt-in [P9] | Fully separate; chat writes to prod by default [P9] | n/a | n/a | Deploy any checkpoint [P25] | Base44 |
| AI Studio | Prompt → preview → Publish → Cloud Run [P27][P28] | One [P28] | Firestore, single [P27] | App Gallery remix, "feeling lucky" [P27] | Verified execution; prompt to fix [P27] | GitHub only [P27] | User's GCP after 2 free services [P28] |
| Floot | MCP chat → preview → `publish_app` [P29] | Preview/published; DB split unknown [U] | Unknown [U] | None documented | typecheck, tests, screenshot, logs [P29] | Checkpoints [P29] | Floot |
| Stripe Projects | `build` → template → provision → agent prompt → `npm run deploy` [P30] | None by itself | n/a | Guided: type → framework → traffic → default variant [P30] | Discovery → approve → phased → verify `/api/health` [P7a] | n/a | Developer, via Stripe [P30] |

## What this means for Lightbulb

Read against `docs/superpowers/specs/2026-10-28-apps-design.md`.

1. **Two stages, never called "dev/prod".** Use **preview** and **live**, as the spec does. Replit needs a docs page to explain "development database"; Anything and Lovable never say it. [P1][P10]
2. **Publish moves schema forward, never copies data, and the builder never writes to live.** This is Replit's and Anything's rule [P1][P10]. Enforce it in `lightbulb app` (the builder session never receives the live `DATABASE_URL`), not in `AGENTS.md`. Base44's "AI chat works with production data by default" is the failure to avoid [P9].
3. **Rethink "code and data go back together" on rollback.** No surveyed builder restores live data with code [P3][P6]. A dump restore erases every row written since that publish, so the foremen lose a day of edits. Default to rolling code back with additive migrations. Make data restore a separate owner card that states the rows it would lose.
4. **Warn before a destructive migration.** Anything warns before dropping a field with live data [P10], and Replit flags renames and drops [P2]. `publish` should diff migrations and post a card when a drop or rename touches non-empty columns.
5. **One starter; the agent never picks.** bolt.diy replaced its LLM picker with a regex and a default within the month [P7]. Stripe ends on a `default: true` variant [P11]. Grow capability through the starter's `services` list. If a second starter ever exists, use Stripe's order: type → framework → traffic.
6. **The workflow lives in the starter.** Write `AGENTS.md` + `prompts/starter-to-product.md` as Stripe does, plus bolt.diy's rules: read before edit, tool output decides, failures reported as failures [P7a][P18]. Then the same workflow runs for Forge and for a laptop Claude Code user.
7. **In a room: one question at most, then a preview.** Stripe's interview-and-approve loop suits a terminal [P7a]. In a room, bolt.diy's rule fits: the request is the authorization; plan with observable completion criteria and build until a preview runs [P7]. The preview link is the question. Keep Stripe's phase order and "never invent the product name".
8. **Verify before posting a link.** Build/typecheck, hit `/api/health`, and run one scripted browser pass with screenshots in the thread (Replit App Testing, v0 browser use [P13][P24]). On a failed `up`, feed Vercel logs straight back to the builder (Fix with v0 [P24]).
9. **Versions carry the conversation.** Replit checkpoints include "the complete conversation history" [P3]. Lightbulb has the room thread: post each `up`/`publish` as a thread line that links the commit and deploy, so "go back to Tuesday's" resolves from the room.
10. **Own the loop; leave the accounts to Stripe.** Stripe Projects owns accounts, credentials, billing, caps, tier→plan mapping and template distribution [P11][P30]. Lightbulb owns the starter and its rules, the preview/live split and the live-DB fence, migration safety, versions, verification, the sign-in door, Canvas/room sharing and approval cards. Register the starter in the registry for laptop users. Ask Stripe to reveal a Neon API key so previews can use Neon branches instead of dump/restore (spec §7).

## Sources (all fetched 2026-10-29)

- P1 https://docs.replit.com/cloud-services/storage-and-databases/production-databases
- P2 https://docs.replit.com/features/data-and-storage/development-and-production
- P3 https://docs.replit.com/replitai/checkpoints-and-rollbacks
- P4 https://docs.replit.com/category/replit-deployments
- P5 https://docs.replit.com/replitai/plan-mode
- P6 https://support.bolt.new/building/using-bolt/rollback-backup
- P7 https://github.com/stackblitz-labs/bolt.diy/blob/main/app/utils/selectStarterTemplate.ts (and commit history of that path)
- P7a https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_one_time_payment_amber-wave/prompts/starter-to-product.md
- P7b https://raw.githubusercontent.com/stripe/projects-templates/main/nextjs_one_time_payment_amber-wave/AGENTS.md
- P8 https://github.com/stackblitz-labs/bolt.diy/blob/main/app/utils/constants.ts
- P9 https://docs.base44.com/documentation/managing-app-data/testing-your-data.md
- P10 https://www.anything.com/docs/apps/databases
- P11 https://github.com/stripe/projects-template-registry (README, guided.yaml, all 32 manifests, example/example.yaml)
- P12 https://github.com/stackblitz-labs/bolt.diy/blob/main/app/lib/common/prompts/prompts.ts
- P13 https://docs.replit.com/replitai/app-testing
- P14 https://docs.replit.com/chat/agent-modes
- P15 https://support.bolt.new/release-notes.md
- P16 https://support.bolt.new/cloud/database
- P17 https://github.com/stackblitz-labs/bolt.diy/blob/5397ce4de1c7c252eaa74c92d321d7af2c7f5166/app/utils/selectStarterTemplate.ts
- P18 https://github.com/stackblitz-labs/bolt.diy/blob/main/app/lib/.server/llm/prompt-workstyle.ts
- P19 https://v0.app/docs/deployments
- P20 https://v0.app/docs/databases
- P21 https://neon.com/docs/guides/vercel-managed-integration
- P22 https://v0.app/templates
- P23 https://v0.app/docs/llms.txt
- P24 https://v0.app/docs/agentic-features
- P25 https://docs.base44.com/developers/references/apps-api/sections/checkpoints.md
- P26 https://firebase.google.com/docs/studio/migrating-project
- P27 https://ai.google.dev/gemini-api/docs/aistudio-build-mode
- P28 https://ai.google.dev/gemini-api/docs/aistudio-deploying.md
- P29 https://floot.com/docs/integrations/what-your-ai-can-do
- P30 https://docs.stripe.com/projects/templates (read via `stripe docs /projects/templates`)
- S: community.vercel.com threads on v0 env vars and Neon preview strings; feedback.base44.com; releasebot.io (Base44); aggregator claim on Floot shared DB (tooljunction.io).
