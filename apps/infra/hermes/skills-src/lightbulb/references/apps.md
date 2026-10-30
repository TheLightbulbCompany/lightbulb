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
   lowercase letters, digits and dashes.
4. Follow the app's `AGENTS.md` and `.lightbulb/plan.md`. They hold the stack, the kind's
   playbook and what "done" means for it. Read `.lightbulb/lessons.md` before touching an area.

Ask at most one card (`references/ui.md`), and only for what you cannot read. Every option can be
skipped or answered "use a placeholder". For a customer-facing app or a "like X" request, offer
one optional chip: "Look at what others in your market do first?" If it is taken, research
alongside the build and return one card of three to five comparable products. The preview never
waits for the research.

## The loop

`lightbulb app check` → fix → `lightbulb app up --json`. `up` commits, checks, sends the code and
waits for the build (up to ten minutes). Read its `next`. If it starts with fix, diagnose or undo,
do that; otherwise do the step it names:

- `fix`: fix the listed problems and run the loop again.
- `diagnose`: stop patching. List what you tried, find the root cause, then fix that.
- `undo`: run `lightbulb app rollback` (before the first preview, `git revert` your last change)
  and tell the room plainly what failed.
- A `hint` is a known fix or one of your own lessons: try it first.

On `"state": "failed"`, read `error.next`. `lightbulb app status --errors --json` shows the app's
recent errors.

## Waiting for approval

If `state` is `awaiting_approval`, the app's accounts need the owner's tap. Say in one line that
there is a card to approve, then **end the turn**. The approval posts a line in this room, and
that line wakes you; run `lightbulb app up --json` again. Never poll, never sleep in a loop.

## Done means proven

When `up` returns a `preview_url`, open it in the shared browser (`references/surfaces.md` §
Browser), walk the main flow, and record it:
`lightbulb app evidence --kind browser --file <recording or screenshot> --note "<what you checked>"`.
Then run `up` again. Post only when it says `"done": true`, one line: the `canvas_url` link, what
the app does, what data it reads, and, only if `cost_cents` is present, what it cost (in dollars). Attach the
recording to that reply (`references/chat-vs-document.md`). If `sample_data` is true, say it runs
on sample data and the real data arrives in a few minutes.

Outside a room turn (a Paperclip run), post the same line with `lightbulb-post.sh --room <roomId>`.

## Changes and publishing

- An edit: the same loop, then one line saying what changed.
- "ship it": ask the reviewer for a pass (`references/apps-reviewer.md`). `publish` refuses
  without it. On a pass, run `lightbulb app publish --json`, then say in one line that it is live,
  with the `canvas_url`. On failures, fix them and ask again.
- "Who can open it" and the budget are the owner's, in Canvas. Point them there.
- Removing: point the owner to the app's Settings in Canvas, "Remove app…".

## Undo and data

"undo" → `lightbulb app rollback --json` (or `lightbulb app rollback <n>` for an older version).
Say which version is live now and that the data is unchanged: undo is code only. Restoring data
is the owner's, from "Data and backups" in the app's Settings in Canvas (admins); Lightbulb keeps
no backups of it.

## Words

The CLI speaks in real terms; the room never hears them. In the room, say only **preview, live,
publish, undo, Canvas**. Never say environment, staging, production, branch, migration, deploy,
database, Vercel or Neon. Say "data" for the database, "make it live" for a deploy, "the change to
how data is stored" for a migration. No retries, flags, ids or file names in the room.

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
