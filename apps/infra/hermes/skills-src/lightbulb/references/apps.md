# Apps: software built for the room

An app is software you build and host for the workspace with `lightbulb app`. It is listed in
Canvas, the screen on the workspace rail where people open it.

## When this applies

Anyone asks for software of any kind: a site, a tool, a form, a dashboard, a portal, a product, an
automation. Someone says "Let's do it" on a suggested app idea. A request from outside the
company needs one. Nothing is declined. Copying an app or a site, its look included, is fine.

This file replaces the "First move" of `references/software-projects.md` for an app: no plan
document, no issues. Its git, workspace-size and stack rules still apply. `up` asks for the app's
accounts itself; the one-time Stripe form and what an account is are in `references/money.md`.

## Where the app is built

Every app has its own conversation, named "App · {name}", shown beside the app in Canvas. The app
is built there, never in a team room, so several apps can be built at once.

- **Asked in a room or a DM:** hand it over. Run
  `lightbulb app start "<the request, word for word>" --name <name> --room <roomId> --requested-by <sender id> --json`
  (`sender` id: the user id on their message, never a name), post the line its `next` gives you
  once, and end the turn. If `next` says the app was already started, post nothing. Don't build here.
- **A message in a room about an app that already exists** (a change, a question, "ship it"):
  `lightbulb app ask <name> "<the message, word for word>" --room <roomId> --requested-by <sender id> --message-id <message id>`,
  then one line: "Passed to {app}'s conversation in Canvas." The builder answers there. If they
  asked to ship it, add: "To make it live, say “ship it” in the app's conversation in Canvas."
- **In an app's own conversation** you are its builder: the first message is the request,
  relayed by Lightbulb as "{person} (in #room): …" or "(via CLI): …". Follow Start below, with
  `lightbulb app new <name> --kind <kind>` and no `--room` (it takes over the app that was
  started). Every later message here is from someone shaping the app: act on it.
- **Changing an app you didn't start, or from another folder:** `lightbulb app pull <name>` first.
  If `up` says `stale_base`, pull again, merge, and run `up` again.

## Start

Before writing any code, check the workspace can host it:
`/app/skills/lightbulb/scripts/lightbulb-api.sh GET "/api/v1/company/provisioning/u/$ISOL8_OWNER_ID/catalog?q=vercel"`.
If it answers `services_account_needed`, `no_company_stripe_account` or `eligible: false`, post the
hosting line (§ What to say) and end the turn. On a `402`, say hosting needs a paid plan and end the
turn. Don't build something it can't host.

1. Read before asking: the thread, the workspace's name, logo and brand, and any URL, screenshot
   or file someone gave you.
2. Pick the kind: `site` (a landing page), `content-site` (edited weekly), `form`, `internal-tool`
   (data with roles), `dashboard`, `portal` (customers see their own rows), `payments` (a product
   that takes money), `automation` (a scheduled job, off by default).
3. Make it in an empty folder of its own:
   `mkdir -p "$HERMES_HOME/.run/<name>" && cd "$HERMES_HOME/.run/<name>"`, then
   `lightbulb app new <name> --kind <kind>` (in an app's own conversation, as above). The name is
   lowercase letters, digits and dashes. Then `cd <name> && pnpm install`: that inner folder is
   the app's folder. Run `lightbulb app up --json` straight away, before writing any code: it
   files the hosting card now, so the owner can approve it while you build. If it says
   `awaiting_approval`, post the approval line and keep going; don't end the turn yet.
4. Follow the app's `AGENTS.md` and `.lightbulb/plan.md`. They hold the stack, the kind's
   playbook and what "done" means for it. Read `.lightbulb/lessons.md` before touching an area.

Then ask once, on one card (`references/ui.md`), before you build: a `Form` of at most four `Select`
fields, each with your default first and a fair alternative. Ask what changes what you build, such as
who can use it, whether things can be deleted, extras beyond what they listed, and the look. Skip
what they already said, and skip the card if nothing is left. Say "I'll go with the first option
in each if you just say go". Never block on it: any reply, including "go", starts the build. For a
customer-facing app or a "like X" request, offer
one optional chip: "Look at what others in your market do first?" If it is taken, research
alongside the build and return one card of three to five comparable products. The preview never
waits for the research.

## The loop

Build the first version yourself; never hand the first build to a sub-agent. Make the main flow
first, the one thing the person asked for, and run `up` as soon as `app check` passes: the person
should see something in minutes, not after everything is finished.

`lightbulb app check` → fix → `lightbulb app up --json`. An app built in its own conversation
already knows who asked (`app start` recorded it). Only for an app you build in a room you were
asked in (no conversation), add `--requested-by "<the person's user id>"` on the first `up` (the
`sender` id on their message; a name is ignored). `up` commits, checks, sends the
code and waits for the build (up to ten minutes). Read its `next`. If it starts with fix, diagnose or undo,
do that; otherwise do the step it names:

- `fix`: fix the listed problems and run the loop again.
- `diagnose`: stop patching. List what you tried, find the root cause, then fix that.
- `undo`: run `lightbulb app rollback` (before the first preview, `git revert` your last change)
  and tell the room plainly what failed.
- A `hint` is a known fix or one of your own lessons: try it first.

A failed `up` or `publish` prints `{code, message, next}` instead of a `state`: read that `next`.
A result with a `state` and an `error` has it in `error.next`. `lightbulb app status --errors
--json` shows the app's recent errors.

## Waiting for approval

If `state` is `awaiting_approval`, the app's accounts need an owner's tap. The card is in
Activity (the bell), under "Needs you", not in this room. Post the approval line once (§ What to
say). Keep building while you wait. Only when the app is ready and `up` still says
`awaiting_approval` do you **end the turn**. The approval posts a line in this room that mentions
you; run `lightbulb app up --json` again when it does. Never poll, never sleep in a loop.

## First version, then done

The first time `up` returns state `ready` and its `next` says the preview is up, post the
first-version line once (§ What to say). A `preview_url` alone is not that: while `next` says the app
is still the starter placeholder, post nothing. Then keep going: add tests for the business rules, fill in the rest, and run `up` again after each change.
Canvas tells people the agent is still working while your run is open.

Then prove it, yourself: never ask the person to test, sign in or click for you. Try the main flow
once: get a `url` from `lightbulb app test-link --json` (it signs the browser in as the builder) and
open it with the
browser tool's navigate call, not by typing into the address bar. If that doesn't work first time,
stop trying and add "I couldn't try it in a browser" to the ready line. Don't spend more than two
minutes here. Never install a browser, and never build a local server or redirect to get past a
browser problem. If you did try it, record it:
`lightbulb app evidence --kind browser --file <recording or screenshot> --note "<what you checked>"`.
Keep the file under `.lightbulb/evidence/` or outside the app; anywhere else is refused.
Run `lightbulb app verify --json` last, after your own checks: it opens every page, sends each form,
then puts the preview data back to its samples, so the person never sees your test entries. Fix
what it lists, then run `up` and `verify` again.
Then run `lightbulb app status --json`, not `up`: a second `up` moves the preview past your record.
Read what's on screen as the person will: wrong words like "Add a account", cut-off text and empty
states are yours to fix first. Post the ready line (§ What to say) only when it says `"done": true`.
The `preview_url` is for your own check. Never post it: for anyone else it is a locked door
page. People open the app from Canvas, so both lines carry `{canvas_url}`. A preview is
not "live"; say "live" only after `publish` returns.
Attach the recording to that reply (`references/chat-vs-document.md`). If `sample_data` is true,
add that it runs on sample data and the real data arrives in a few minutes.

Rows you put in `drizzle/seed.sql` are examples, so label them: end each visible name with
"(sample)". They must never pass for the person's real customers. Offer to clear them.

Outside a room turn (a Paperclip run), post the same line in the app's conversation with
`lightbulb-post.sh --room <roomId>`, where `<roomId>` is the app's `conversation_id` (from
`lightbulb app status --json`, run in the app's folder).

## Changes and publishing

- An edit: the same loop, then one line saying what changed.
- "ship it" or "publish": run `lightbulb app verify --json` for the current version, fix anything
  it lists, then run `lightbulb app publish --json` and post the live line. No second agent and no
  checking line: verify is the check. Ask for a review (`references/apps-reviewer.md`) only when the
  person asks for one.
- "Who can open it" and the budget: the person who asked for the app sets them in Canvas → {app}
  → Settings (a workspace admin can too). Point them there.
- Removing: point the same people to the app's Settings in Canvas, "Remove app…".
- "undo": `lightbulb app rollback --json` (`rollback <n>` for an older version), then post the
  undo line. Undo changes only the app, never its data.

## What to say

Post these lines as written; fill in the `{braces}`. `{app}` is the app's name as people read it
("Workshop Sign-up"), never the lowercase name you gave `app new`. `{who}` is who can open it:
"people in #{room}" (the start), "everyone in the workspace", "people you invited" or "anyone with
the link".

Post as little as the person needs: the approval line if a card was filed, the first-version line,
and the ready line. No progress notes in between unless the person asks how it's going; Canvas
already shows that the agent is still working. The lines below stay exactly as specified.

- Hosting: "Before I build {app}: this workspace needs a services account to host it. An admin can
  add one from the Stripe card in Settings → Connections. Tell me when it's done and I'll start."
- First version (once, at the first `ready` preview; never "ready" or "live"): "A first version of {app}
  is in Canvas: {canvas_url}. It {what works so far}. I'm still working on {what's left} and will
  post here when it's ready." If `sample_data` is true, add that it runs on sample data for now.
- Preview ready (when `"done": true`): "Your preview of {app} is ready in Canvas: {canvas_url}. It {what it does}; it
  keeps {what data it saves or reads}. I tested it by {what you actually did, step by step}.
  {n} example rows are in it, marked (sample); I can clear them. Only people in #{room} can open
  it for now. Say "publish" (or press Publish in Canvas) to make it live." Name anything you could
  not test. Only if `cost_cents` is present, add "Building it cost ${dollars}." End that reply with
  a `FollowUps` card: "Publish it" first, then two or three changes this app would really gain from.
- Meant for people outside the room (a sign-up page, a site, a client form), after the preview
  line and again before publishing: "Right now only people in #{room} can open it. For {your
  attendees}, set "Who can open it" to "Anyone with the link" in Canvas → {app} → Settings. You
  can do that yourself, or a workspace admin can."
- Approval: "To keep its data I need an owner's OK to set up hosting and storage for {app}. The
  card is in Activity, under Needs you; tap Approve. The preview can't start until then." Name the
  owner if you know who.
- Review failed: "The check found {n} problems ({short plain list}). Fixing them now, then it goes
  live."
- Live, public: "{app} is live — anyone with the link can open it: {address publish returned}. Say
  "undo" to put the previous version back."
- Live, otherwise: "{app} is live — {who} can open it. They open it from Canvas. Say "undo" to
  put the previous version back."
- Undo done: "I put back the previous version of {app}. Undo changes only the app, not its data.
  To restore data from a backup, the person who asked for it or a workspace admin uses Data and
  backups in the app's Settings in Canvas."

## Words

The CLI speaks in real terms; the room never hears them. In the room, the only special words are
**preview, live, publish, undo, Canvas**. Never say deploy, environment, staging, production,
branch, migration, database, provider, console, Vercel or Neon. Say "data" for the database, "make
it live" for a deploy, "the change to how data is stored" for a migration. No version numbers,
retries, flags, ids, file names or the app's lowercase name in the room.

## Rules

- Never a secret in code, in a file you commit, or in the thread. A key or password the app
  needs comes through the `secrets` tool.
- Never remove or bypass the door (the app's sign-in check). Data access stays server-side.
- The schema only grows: add a column and copy data, never rename or drop. A destructive change
  needs the owner's sign-off: `request_approval` showing the SQL, then run `up` or `publish` with
  `--approved <approval id>`.
- Delete, pay, send or publish only when someone asked for it.
- Write a lesson in `.lightbulb/lessons.md` after every failure you resolve.
- A tool that is broken, not your code: report it once with
  `lightbulb app report-broken-tool <tool> "<what happened>"`, then stop retrying it.
- If GitHub is connected (`gh auth status`), push the app's repo to the company's GitHub after
  each publish (`references/software-projects.md` § Repository).
- Keep looking after the app in the room it was built in.
