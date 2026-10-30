# Reviewing an app

The builder never grades its own work. A separate reviewer checks the app before the first preview
is posted and again before it goes live. `lightbulb app publish` refuses without a pass for the
exact commit.

## Starting a review (the builder)

Start a fresh run that knows nothing of how the app was built:

- a Paperclip task assigned to a second agent on the roster (the paperclip skill), or
- a `delegate_task` subagent with fresh context.

Give it only these:

- the preview URL (`preview_url` from `up`);
- the person's exact words: every message where they said what they want, copied from the room
  word for word, with no summary and no tidying;
- the absolute path of the app's folder (it reads `AGENTS.md` there and records its result there);
- whether `sample_data` is true;
- "short", only when `references/apps.md` § Changes and publishing says a short review is enough.

Not your plan, your lessons or what you think works. Post the matching checking line
(`references/apps.md` § What to say), then end the turn or wait for the subagent; never review it
yourself. On failures, post the matching review-failed line before you fix them.

## Doing the review (the reviewer)

You are skeptical. Your job is to find what is broken before a person does. Use test names and
`@example.com` addresses so nobody real gets mail, and never a real card. Remove what you made
where the app lets you.

1. Read the app's `AGENTS.md` and its `.lightbulb/plan.md` "done when" line. Where they differ from
   the person's words, the person's words win.
2. Open the preview URL in the shared browser (`references/surfaces.md` § Browser). If you only
   reach the page that says "Open this from Canvas", you are not in the app: report "could not get
   in" and stop.
3. List each specific thing the person named: a word, a format, a number, an order, a limit. Check
   each on screen exactly as they said it. Close is a failure.
4. If the app has accounts, sign up as someone new and carry on until a signed-in page shows that
   person. A "success" message that leaves you signed out is a failure. Sign out and back in.
5. Try a duplicate of anything that must be unique: an email, a booking, a name, a seat. The second
   must be refused with a plain message, and no second row may appear.
6. Walk every flow: each page, each form, each button, empty and bad input, and back again.
7. Probe as two different users where the app has sign-in or roles: one must not see or change
   the other's private rows. A signed-out window must reach nothing but the closed page.
8. Check phone width (390 px wide): nothing cut off, nothing overlapping, every action reachable.
9. Record the result from the app's folder with a recording or screenshot of what you saw, kept
   under `.lightbulb/evidence/` or outside the app (anywhere else is refused):
   `lightbulb app evidence --kind review --verdict pass --file <file> --note "<what you walked>"`.
   On a failure use `--verdict fail`. `publish` counts only a pass for the exact commit in HEAD.
10. Report the pass, or one line per failure, to the builder (the task comment or your final reply).

On a short review, do steps 1 to 5, walk the main flow, then 9 and 10.

If `sample_data` is true, nothing entered is kept, so steps 4 and 5 cannot be proved. Report them
as "not tried: sample data"; that alone does not fail the review. Anything else you could not try
is a failure: say "could not try" and why.

Never edit the app's code, never run `up` or `publish`, and never post in the app's room: the
builder tells the room. A failure is the builder's to fix, then it asks for a new review.
