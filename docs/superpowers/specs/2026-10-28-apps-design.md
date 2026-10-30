# Apps: any software a company asks for, built for them in a room

Date: 2026-10-29 (third draft; replaces the 28 Oct drafts)
Status: DRAFT for owner approval. Nothing is built until approved.
Author: Claude (with Prasiddha)

The research behind every line is in `docs/research/2026-10-29-*.md`: the synthesis
(`apps-how-to-build-synthesis.md`), Lovable's experience and agent (`lovable-ux-deep.md`,
`lovable-agent-deep.md`), Replit/Bolt/v0/Base44/Anything (`builder-agents-deep.md`), templates and
kinds of app (`templates-kinds-of-apps.md`), content sites (`content-sites-like-webflow.md`), the harness (`builder-harness-engineering.md`), Dyad's source
(`dyad-source-deep.md`), and the production spike (`apps-spike-revealed-keys.md`). "My call" marks
what you have not ruled on.

## 1. The promise

Anyone in a workspace can ask for any software (a site, a tool, a dashboard, a portal, a form, a
product with payments) in one sentence, and get a working, personal, shareable version back in
minutes, without knowing how software is built. It runs on the company's own accounts, the room is
the sign-in, and the agent that built it keeps it working. Because the company owns everything, any app can also be made available outside the workspace (your review, 29 Oct).

## 2. One CLI, two kinds of driver, one set of words each

**`lightbulb app` is used the same way by every builder** (your ruling, 29 Oct). A Lightbulb workspace
agent building an app for a room and a developer in their own Claude Code or Codex who wants our
app builder, hosting and infrastructure run the same commands, get the same output, and go through
the same backend: Stripe Projects on the company's services account, hosting, the database, the door
and Canvas. The developer signs in once with `lightbulb login`; the workspace agent is already signed
in. There is no separate "laptop mode".

The CLI speaks plainly in real terms (environments, migrations, deployments, providers), as Naive's
CLI does. The difference is only who reads it:

- **A developer** reads the CLI directly.
- **A person in a room** never sees the CLI. The workspace agent reads it and talks to them in five
  words: **preview, live, publish, undo, Canvas**. Never environment, staging, production, branch,
  migration, deploy, database, Vercel or Neon (Lovable's glossary has none of them).

Inside Lightbulb there are three names: the **App** (code, its accounts, its address), the **builder**
(whoever drives the CLI: a workspace agent or a developer's agent), and **`lightbulb app`**.

## 3. How apps start, and what the person experiences

### Nine ways an app gets started

From `app-triggers-and-origins.md`. Each one starts the same builder; what differs is who starts it and
what the builder does first.

| Origin | Started by | The builder first | Asks |
|---|---|---|---|
| A person asks ("build us a crew schedule") | A person | Reads the thread and the company's brand and data | Nothing, or one card |
| Data outgrew a spreadsheet | A person or the agent noticing | Imports the sheet and keeps its columns | Who edits vs who reads |
| A chore or question keeps coming back | The agent, as a suggested idea; "Let's do it" starts the build | Shows what it saw repeat and what the app would do | Nothing |
| Something outside asks (a customer wants a portal, a partner needs a form, a launch, a vendor fails) | A person or an automation | Builds for the outside audience; link-only sharing for them | Who outside should see it |
| "Like Calendly" / a URL / a screenshot / a Figma file | A person | Copies it, look and feel included | Nothing |
| Replacing a SaaS the company pays for | A person or the agent noticing unused seats | Lists what they actually use; builds that, with their data imported | Confirms the list |
| Moving their own site or app | A person | Imports it in full (ownership checked against the workspace's verified domain) | Nothing |
| A shared template | A person | Starts from it, personalized to this company | Nothing |
| Copying a teammate's app | A person | Remixes it within the workspace | What to change |

**Lightbulb is ahead here:** no builder looks at a company's work and proposes an app. Our agents
already suggest ideas, run automations and remember the workspace, so "the agent noticed" is a
first-class origin, not an afterthought. Each app records where it came from, so later notices can
say what it was built for.

**Personalizing and researching:** the builder reads what it can first (the company's name, logo,
colours, data, the thread, any URL or file) and asks only what it can't; every question can be
skipped or answered "use a placeholder". For customer-facing apps and "like X" requests, its first
reply offers one optional chip: "Look at what others in your market do first?" The research runs
alongside the build and returns one card of three to five comparable products, each with "Add to my
app" or "Skip". The preview never waits for it.

### The experience, from ask to everyday use

| Moment | What they do | What they see |
|---|---|---|
| Ask | One sentence, or "Let's do it" on a suggested idea | The builder says it's on it. At most one card, only for what it can't read. |
| First time only | One short form | If the workspace has no Stripe services account: Stripe's own form (name, birth date, email, terms), once. |
| Accounts | One tap | One approval card for this app's accounts (free tier). The first preview never waits for the accounts. |
| Preview | Opens it in Canvas | A working app with their context in it, one line on what it does and what it reads, a recording of it working, and what it cost to build. |
| Change | Replies in the thread, or points at part of the preview | The same preview updates; one line says what changed. |
| Publish | "ship it", or Publish in Canvas | Live for whoever it's shared with. No settings. |
| Use | Opens it from Canvas | Already signed in, on a phone or a laptop. |
| Edit content (sites) | Clicks Edit on the live page, or asks the builder | Words and images change in place; a reviewer publishes. |
| Trouble | "this is broken" / "undo that" | The builder fixes it, or puts the previous version live. Undo is code only; the data stays as it is. |
| Later | Nothing | Cost per app on its Canvas entry; one line in the room if an app goes unused for 30 days, saying what it was built for. |

**Canvas** is a screen on the workspace rail beside Chat, Activity, Work and Library. It lists the
workspace's apps (greyed with "ask in #room" when you can't open one), opens an app in the main pane,
and lets the owner change who can open it and its budget.

**Sharing** is one line per app, and the owner can change it any time from Canvas:

- **The room** (default) or **the whole workspace**: opened from Canvas through a short-lived Lightbulb
  pass, so nobody creates an account.
- **Invited outside people** (a client, a partner, a contractor): the owner adds their emails; each
  gets a one-time link to that app only, no Lightbulb account (my call).
- **Public**: anyone with the address, for sites and products; when outside users need their own
  accounts (a customer portal, a product people sign up for), the app adds sign-in from the Stripe
  catalog (Clerk or Neon Auth) on the company's own account.
- **The company's own domain**: because the accounts are theirs, a public app can move from
  `<name>.apps.thelightbulb.company` to their own domain.

## 4. Kinds of software: one starter, switches, and the company's own tools

There is no "generative software in general"; every builder collapses to one default starter and the
kind of request flips switches (database, sign-in, design card, payments, content). The builder never
picks a template.

The starter is Next.js (App Router), Tailwind, shadcn, Drizzle with migrations, the sign-in door,
an error reporter, and `AGENTS.md` with the workflow in §5, adapted from Dyad's Next.js template
(Apache-2.0). Its `services` list is the switchboard.

| Kind | Switches | Builds first | Done when |
|---|---|---|---|
| Landing page / small site | content | The page from the brand or an existing URL; SEO defaults | Builds, links work, phone width checked |
| Content site (edited weekly) | content | Collections, page templates, the editor | An editor can change a headline and a reviewer can publish it |
| Form / intake | database | Form, table, gated submissions list | A test submission appears in the list |
| Internal tool on data | database, sign-in with roles | Import the attached sheet, else sample data; list, detail, edit | Two people can't see each other's private rows |
| Dashboard | database if file data | Summarise the data first; then cards, charts, filters | One figure checked against the source |
| Customer portal | database, sign-in with a customer role | The customer's view of their own rows | A customer sees only their rows |
| Product with payments | database, sign-in, payments | Landing, sign-in, checkout, then logic | A test checkout completes |
| Automation | shares the app's | A scheduled job, off by default, with a dry run | The dry run posts what it would do |

**Content sites (the Webflow case):** the `content` switch adds Payload 3 (MIT, Next.js-native, runs on
the app's own Neon): collections, drafts, scheduled publishing, translations, forms, redirects, SEO
fields, roles and live preview, signed in through our door (no second password). We add a small Edit
overlay that jumps from a section on the live page to its field. A content request from the room
becomes a draft with a preview and a diff, published on "ship it"; code changes are only for new
section types or layout. If a company already runs Webflow or Framer, the builder edits it there
through their own agent interfaces instead of migrating it.

**Payments** are a Stripe Projects service like the database: the catalog's `stripe/sandbox` (test
payments) and `stripe/livemode` (live payments) hand the app a Stripe API key, as Stripe's own SaaS
template does. The key belongs to the account the app's Project lives on: the company's real Stripe
account when Lightbulb created it; for a company that connected an existing account, the Project lives
on the Projects-only services account, which isn't set up to take payments. How Stripe handles that
case is undocumented; one free test on Demo Lab (`stripe/sandbox` and a test payment) settles it
before payments are built.

**The company's other tools** matter only when an app works on data the company already keeps
elsewhere (an inventory board over Shopify orders, a portal over a CRM). The rule: an app never holds a
company credential; it reaches those tools through a Lightbulb gateway that attaches the workspace's
connection and allows only what that app was granted, the way Lovable's connectors work. Which tools,
and how each is connected, is decided when a real request needs one (`app-integrations.md`).

## 5. The builder

Reliability comes from the loop around the model, not the model (on the one benchmark that judges
deployed apps in a browser, the best model passes 62%, and testing its own work while building is the
strongest predictor of success). So the builder's workflow is fixed, and the parts that matter are
enforced in code.

**Its workflow, in `AGENTS.md` and the Hermes skill (rules adapted from Dyad's Apache prompts):**
1. Read before asking (§4). Ask at most one card, only what can't be read.
2. Build frontend first with sample data; add data, then sign-in, when the feature needs them.
3. Check cheapest-first: edit validation, type check and lint, migration check, the build, health
   check and logs, the saved flow tests, then one pass in the shared browser.
4. Post the preview only when `lightbulb app up` says it is done, with the recording.
5. After a second failed fix of the same error, stop patching: diagnose, with a stronger model
   proposing a fresh plan. After a third, undo and say so plainly.
6. Write a lesson after every resolved failure; read the app's lessons before touching that area.
7. Safe by default: the schema only grows; a destructive migration becomes an owner card showing the
   SQL; secrets only through a form, never in the thread; generated apps start read-only or undoable,
   and delete, pay, send or publish only on explicit request.

**What is enforced in code, in the open-source `lightbulb app` CLI:**
- **The done gate.** `up` refuses to report a preview ready unless the app built, its health check
  passed and a browser pass ran after the last edit (bolt.diy's server rejects "complete" without
  fresh evidence; we do the same).
- **State in files:** plan, progress and lessons live in the repo, because the workspace agent
  compacts its memory and works in 30-minute turns.
- **Caps:** fix attempts per error, steps per turn, and an honest stop.
- **`lightbulb app check`:** type check and build with errors parsed into file, line and fix
  (ported from Dyad's parser), used by `up` and by the builder directly.
- **The destructive-migration gate**, using Dyad's MIT schema classifier.

- **Design and safety checks in `check`:** a lint that flags design breaking the company's own
  tokens (Lovable scans every generation against its design system); on publish, a dependency audit,
  a check that every route sits behind the door, and a probe as two different users.
- **Stuck detection:** a "report a broken tool" command and a shared file of known fixes, so a stuck
  builder stops retrying and we learn what broke.
- **Tests that stay:** a small test for every business rule (a price, a limit, who can do what), and
  the done gate's browser pass saved as a repeatable test, recorded from the preview (Dyad's
  recorder, Apache).

**Speed:** the first preview never waits for provisioning (the database took six minutes in the
spike): it runs on sample data while the accounts are created, then switches over.

**Pointing at things:** in a preview, a person can point at a part of the page and say "change this"
(Dyad's element tagger, Apache).

**A separate reviewer** (a skeptical agent with fresh context) checks the app in the browser on the
first build and on every "ship it". The builder never grades its own work at those two moments.

**Weekly, by us:** the builders' lessons are clustered into fixes to the starter, lint rules or
harness reminders. Every harness part is removable once evals show newer models don't need it.

## 6. How we know it works

An eval suite in the monorepo (`evals/apps/`), adapted from Dyad's app-builder benchmark (Apache):
20 tasks (10 new builds across the kinds in §4, 7 changes to apps the builder made, 3 adversarial),
each run three times from scratch. Graders: code for the safety rules, browser user journeys against
the preview URL, and a judged rubric calibrated against human labels. **Reliable means all three runs
pass.** We also track minutes from "build me" to a second person opening it, fix loops per build,
and cost (estimated $7–10 per first build, $1–2 per change; the first run measures it).

**Stage 0 is measuring today's workspace agent on the 20 tasks, before building anything**, so every
later stage has a baseline to beat.

## 7. What we take from Dyad, and what Lovable shows us

**Dyad (Apache-2.0 parts only, with attribution; nothing from `src/pro`).** Its agent loop moved
entirely into `src/pro` (FSL, a two-year competing-use ban) in August, so its engine is not reusable,
and our builders bring their own loop. We take: its Next.js template as the starter skeleton; the
rules in its prompts (simplicity, completeness, understand-plan-implement-verify, fix-loop rules,
product questions before the first build, and the summary prompt that keeps a person's standing
preferences in long chats); its type-check error parser, ported to Rust; its MIT schema classifier as
the destructive-migration gate; its element tagger and flow recorder; its install policy (a package
firewall and a one-day minimum package age); its versioning behaviours (one commit per turn, undo as
a new commit); its error-capture pattern; and its app-builder
benchmark with its own sanity checks (a reference app must score 100% and a broken twin must fail
every security probe), as our eval suite. Sources: `dyad-source-deep.md`, `dyad-vs-lovable-gaps.md`.

**Lovable is a reference, never a source.** Its terms (28 Aug 2026) ban reverse engineering, working out
its system logic, and scrapers, so we learn only from its public docs, changelog, engineering blog and
open-source repos. What it does better that we match or beat, each with our mechanism, is in
`dyad-vs-lovable-gaps.md` §4.2; the main ones are folded into §3–§5 above (first-preview speed, design
checks, pointing at elements, tests per rule, publish security, stuck detection, cost on the
done line, import and remix). Where we are ahead: rooms as the sign-in, agents proposing apps, and apps on
the company's own accounts that never go dark.

## 8. Not in the first build

drag-and-drop layout editing for marketers (Puck, if a room asks); A/B tests;
Webflow-level animation tooling; a second starter.

## 10. My calls (check these)

1. One starter with switches; the builder never picks a template.
2. At most one question card, only when the answer can't be read.
3. The done gate, state files and caps live in the open-source CLI; the reviewer runs on first build
   and on "ship it".
4. Undo is code only. Data backups and restores are the database provider's job (Neon keeps its own history on the company's account); Lightbulb takes none (your ruling, 29 Oct).
5. The preview database starts from the app's sample data, so nothing done in preview touches live.
6. Payload for content; our own Edit overlay; Puck only on demand.
7. Stage 0 (baseline eval) comes before any build.

## 11. Your calls

Settled 29 Oct from your review: copying any app or site, look included, is allowed (no refusal for
imitation); payments come from Stripe Projects' own Stripe service (test pending for connected accounts); integrations wait for a real
request. My call to check: keep a publish-time scan for pages that collect passwords or card details
under another company's brand (Lovable added one after phishing sites were built on it), as a warning,
not a refusal.

Also settled: Vercel's free (Hobby) plan stays the default; a company upgrades through Stripe Projects
when it needs to.

## Appendix A: the plumbing (proven in production, 29 Oct)

- **Stripe Projects is the one rail.** A Projects-only services account per workspace, created on the
  first app request; Stripe's form once; one approval card per app. Demo Lab provisioned Neon in ~6
  minutes and Vercel in ~30 seconds after approval, with no sign-in at either provider.
- **What Stripe reveals:** Neon gives a connection string (no admin key, so no Neon branching);
  Vercel gives a token scoped to that one project. Both stay in Secrets Manager under the app and never
  reach the builder or the thread.
- **Two accounts per app, Stripe's way (your ruling):** a preview database and a live database as
  separate resources. Publish applies only add-only schema changes to live; the builder never writes
  live data. Backups are Neon's, on the company's own account; Lightbulb takes none (your ruling).
- **Hosting:** the backend sets env vars, deploys from files (31 s in the spike), turns off Vercel's
  own login wall (on by default for new projects), attaches `<name>.apps.thelightbulb.company`, and
  rolls back (6 s in the spike), all through Vercel's API. Vercel refuses known-vulnerable Next.js
  versions, so the starter pins current ones.
- **The rail changes still needed for apps** (agreed with the rail owner): one Stripe project per app,
  the environment field on every resource, app credentials kept out of every agent's environment, the
  catalog re-read just before each write, per-provider limits from the app's budget, and a room post
  on approval so the builder wakes. Two production bugs in the rail were found and fixed by the spike
  (#4486 empty catalog, #4488 Vercel request crash).
- **Sign-in:** a Lightbulb-signed pass (15 minutes) opens the app; the app sets its own day-long
  cookie; anyone else sees "Open this from Canvas in Lightbulb".

- **Where an app's code lives:** every app is a git repository, with one commit per change (that is
  what makes undo work). Lightbulb keeps a copy of each version, so any builder can pick an app up
  later even if the one that built it is gone; if the company has connected GitHub, the same commits
  are pushed there too, the way Lovable syncs to GitHub.

## Appendix B: where the code lives

| Piece | Lives in |
|---|---|
| `lightbulb app` (new, up, publish, rollback, status, destroy, check), the done gate, state files, caps, migration gate | Open-source Rust CLI (`TheLightbulbCompany/cli`) |
| The starter, `AGENTS.md`, per-kind playbooks, error reporter, door, Payload switch | A starter embedded in the CLI |
| The skill, the reviewer, room posts | The workspace (Hermes skill and bridge) |
| Stripe rail changes, Vercel and database plumbing, app record, pass, idle check | The backend |
| Canvas | The web workspace |
| The eval suite | `evals/apps/` in the monorepo |

The implementation plans (`docs/superpowers/plans/2026-10-29-apps-*.md`) predate this draft and get
rewritten from it once you approve.
