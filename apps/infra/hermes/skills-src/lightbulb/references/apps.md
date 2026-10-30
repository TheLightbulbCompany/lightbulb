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

## Start

1. Read before asking: the thread, the workspace's name, logo and brand, and any URL, screenshot
   or file someone gave you.
2. Pick the kind: `site` (a landing page), `content-site` (edited weekly), `form`, `internal-tool`
   (data with roles), `dashboard`, `portal` (customers see their own rows), `payments` (a product
   that takes money), `automation` (a scheduled job, off by default).
3. Make it in an empty folder of its own:
   `mkdir -p "$HERMES_HOME/.run/<name>" && cd "$HERMES_HOME/.run/<name>"`, then
   `lightbulb app new <name> --kind <kind> --room <roomId>` (this room's id). The name is
   lowercase letters, digits and dashes. Then `cd <name> && pnpm install`: that inner folder is
   the app's folder.
4. Follow the app's `AGENTS.md` and `.lightbulb/plan.md`. They hold the stack, the kind's
   playbook and what "done" means for it. Read `.lightbulb/lessons.md` before touching an area.

Ask at most one card (`references/ui.md`), and only for what you cannot read. Every option can be
skipped or answered "use a placeholder". For a customer-facing app or a "like X" request, offer
one optional chip: "Look at what others in your market do first?" If it is taken, research
alongside the build and return one card of three to five comparable products. The preview never
waits for the research.

## The loop

`lightbulb app check` → fix → `lightbulb app up --json`. On the first `up` add
`--requested-by "<the person's user id>"` (the `sender` id on their message; a name is ignored)
so Canvas shows who asked and they can change its Settings. `up` commits, checks, sends the
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

If `state` is `awaiting_approval`, the app's accounts need an owner's tap. The cards are in
Activity (the bell), under "Needs you", not in this room. Post the approval line (§ What to say),
then **end the turn**. Each approval posts a line in this room that mentions you; run
`lightbulb app up --json` again when it does. Never poll, never sleep in a loop.

## Done means proven

When `up` returns a `preview_url`, open it in the shared browser (`references/surfaces.md` §
Browser), walk the main flow, and record it:
`lightbulb app evidence --kind browser --file <recording or screenshot> --note "<what you checked>"`.
Keep the file under `.lightbulb/evidence/` or outside the app; anywhere else is refused.
Then run `lightbulb app status --json`, not `up`: a second `up` moves the preview past your record.

`"done": true` means only that your own check is in. Its `next` and the app's `AGENTS.md` say to
tell the room; this file wins. Nothing goes to the room until a fresh tester has passed it:

1. Ask the reviewer for a pass on the preview (`references/apps-reviewer.md`). Hand it the person's
   exact words, copied from the room, and the `preview_url`. Post the preview checking line, then
   end the turn or wait.
2. On failures, post the preview review-failed line, fix them with the same loop and your own check,
   and ask again. A fix changes the commit, so the new version needs its own pass.
3. The same failure twice: stop patching. List what you tried and find the root cause.
4. Three failed reviews, or "could not get in": stop. Post the still-not-right line and end the
   turn. Ask for another review only after the person answers.
5. On a pass, post the preview line (§ What to say).

The `preview_url` is for these checks. Never post it: for anyone else it is a locked door
page. People open the app from Canvas, so the preview line carries `{canvas_url}`. A preview is
not "live"; say "live" only after `publish` returns.
Attach the recording to that reply (`references/chat-vs-document.md`). If `sample_data` is true,
add that it runs on sample data and the real data arrives in a few minutes.

Outside a room turn (a Paperclip run), post the same line with `lightbulb-post.sh --room <roomId>`.

## Changes and publishing

- An edit: the same loop, then one line saying what changed.
- "ship it" or "publish": ask the reviewer for a pass (`references/apps-reviewer.md`); `publish`
  refuses without one for the exact commit in HEAD. Post the checking line, then end the turn. The
  reviewer never posts in the room; you do. On failures, post the review-failed line, fix them and
  ask again, with the same limit as the preview. On a pass, run `lightbulb app publish --json`,
  then post the live line.
- Ask for a short review only when all of these hold: `git status --porcelain` prints nothing,
  `git rev-parse HEAD` is the `commit` of the preview's review pass in `.lightbulb/progress.json`,
  and that pass was not on sample data. Otherwise ask for the full one.
- The short review is this file's rule. With HEAD unchanged, the preview's pass already satisfies
  `publish`, and a later `--verdict fail` on the same commit does not cancel it. Never publish over
  a failure the reviewer reported.
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

Progress notes while building are optional: one plain sentence about what the person will get
("Setting up the sign-up form and where the answers go"), never a library, table, type or file
name, and at most one every few minutes. The lines below stay exactly as specified.

- Preview ready: "Your preview of {app} is ready in Canvas: {canvas_url}. It {what it does}; it
  keeps {what data it saves or reads}. Only people in #{room} can open it for now. Say "publish"
  (or press Publish in Canvas) to make it live." Only if `cost_cents` is present, add "Building it
  cost ${dollars}."
- Meant for people outside the room (a sign-up page, a site, a client form), after the preview
  line and again before publishing: "Right now only people in #{room} can open it. For {your
  attendees}, set "Who can open it" to "Anyone with the link" in Canvas → {app} → Settings. You
  can do that yourself, or a workspace admin can."
- Approval: "To keep its data I need an owner's OK to set up hosting and storage for {app}. The
  three cards are in Activity, under Needs you; tap Approve on each. The preview keeps going
  meanwhile." Name the owner if you know who.
- Checking, before the preview: "Checking {app} before I hand it over: a second agent is trying it
  the way you asked. I'll post your preview here, usually within a few minutes."
- Review failed, before the preview: "The check found {n} problems ({short plain list}). Fixing them
  now, then you get the preview."
- Still not right: "{app} isn't right yet: {short plain list}. You can open the preview in Canvas:
  {canvas_url}. It is not live. Tell me what to change or drop." For something that could not be
  tried, the list says "not checked: {what}".
- Checking: "Checking {app} before it goes live: a second agent is trying every page. I'll post
  here when it's live, usually within a few minutes."
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
