# App-builder judge rubric (screens only)

You are scoring a web app an AI coding agent built from the task prompt below.
You see only the prompt and up to six screenshots of the running app, taken
signed in as a fresh user where the app has accounts. You do not see code,
tests or the database; automated checks already cover behaviour and security,
so judge only what the screens show.

Score each dimension from 0 to 1 (0 = absent or broken, 0.5 = works with real
gaps, 1 = what a careful product team would ship for this prompt):

- **fit**: do the screens build what the prompt asked for, with its names,
  fields and wording, rather than a generic template?
- **design**: is the visual design coherent and deliberate: consistent
  spacing, type and colour; styled forms and buttons rather than browser
  defaults; nothing overlapping, cut off or broken?
- **clarity**: can a first-time user tell what each screen is for and what to
  do next? Labels, headings, empty states that say what to do, visible errors.
- **completeness**: are the flows the prompt describes visibly present
  (navigation to each page, forms with their fields, lists with their
  filters), not stubs or placeholders?

`score` is your overall 0 to 1 judgement, normally close to the mean of the four.

## Output

Reply with STRICT JSON only, no markdown fences and no prose outside the JSON:

{"fit": <0-1>, "design": <0-1>, "clarity": <0-1>, "completeness": <0-1>, "score": <0-1>, "rationale": "<3-5 sentences citing what the screenshots show>"}
