# App-builder judge rubric (screens only)

You are scoring a web app an AI coding agent built from the task prompt below.
You see only the prompt and up to six screenshots of the running app. Where the
app has accounts, they are taken signed in as a fresh user who has created one
sample record through the app's API (its name may read like "Probe private"):
typically the sign-up page, the list with that record, the record's own page and
the create form. You do not see code, tests or the database. Automated checks
already cover behaviour and security (sign-in, validation messages, completing,
editing, deleting, privacy), so judge only what the screens show and do not mark
the app down for flows that sit behind a click you were not shown.

Score each dimension from 0 to 1 (0 = absent or broken, 0.5 = works with real
gaps, 1 = a sound answer to this prompt):

- **fit**: do the screens build what the prompt asked for, with its names,
  fields and wording, rather than a generic template?
- **completeness**: do the screens you were shown carry what the prompt says
  belongs on them (the list with its filters and its record, the record's
  details and actions, forms with their fields), not stubs or placeholders?
- **clarity**: can a first-time user tell what each screen is for and what to
  do next? Headings, labelled fields and buttons, readable data.
- **design**: is it usable and tidy: legible, consistent, nothing overlapping,
  cut off or unstyled browser defaults? The prompt asks for no visual style,
  so plain and simple is fine; decoration earns nothing.

`score` is your overall 0 to 1 judgement, weighted toward what the prompt asked
for: roughly 0.35 fit + 0.35 completeness + 0.2 clarity + 0.1 design. A plain
app that shows everything the prompt asked for scores 0.9 or more; screens that
are an error page, a sign-in wall, blank or a generic template score low.

## Output

Reply with STRICT JSON only, no markdown fences and no prose outside the JSON:

{"fit": <0-1>, "design": <0-1>, "clarity": <0-1>, "completeness": <0-1>, "score": <0-1>, "reason": "<3-5 sentences citing what the screenshots show>"}
