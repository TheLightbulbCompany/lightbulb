# Reviewing an app

The builder never grades its own work on the first build or on "ship it". A separate reviewer
does, and `lightbulb app publish` refuses until it has.

## Starting a review (the builder)

Start a fresh run that knows nothing of how the app was built:

- a task for a second agent on the roster (ask Lightbulb to start it, naming them), or
- a `delegate_task` subagent with fresh context.

Give it only three things: the preview URL (`preview_url` from `up`), the app's purpose in one or
two sentences, and the absolute path of the app's folder (it reads `AGENTS.md` there and records
its result there). Not your plan, your lessons or what you think works. Post the checking line
(`references/apps.md` § What to say), then end the turn or wait for the subagent; never review it
yourself. On failures, post the review-failed line before you fix them.

## Doing the review (the reviewer)

You are skeptical. Your job is to find what is broken before a person does.

1. Read the app's `AGENTS.md` and its `.lightbulb/plan.md` "done when" line.
2. Open the preview URL in the shared browser (`references/surfaces.md` § Browser) and walk every
   flow: each page, each form, each button, empty and bad input, and back again.
3. Probe as two different users where the app has sign-in or roles: one must not see or change
   the other's private rows. A signed-out window must reach nothing but the closed page.
4. Check phone width (390 px wide): nothing cut off, nothing overlapping, every action reachable.
5. Only on a pass, record it in the app's folder with a recording or screenshot of what you saw:
   `lightbulb app evidence --kind review --verdict pass --file <file> --note "<what you walked>"`.
   Keep the file under `.lightbulb/evidence/` or outside the app; anywhere else is refused.
   `publish` needs a pass for the exact commit in HEAD, so any new commit needs a new review.
6. Report the pass, or one line per failure, to the builder (the task comment or your final reply).

Never edit the app's code, never run `up` or `publish`, and never post in the app's room: the
builder tells the room. A failure is the builder's to fix, then it asks for a new review.
