# Writing a document

A document is read by a person who did not watch you work. It is a memo from a colleague, not
a status report. The pane shows the title, who wrote it and when, and the version: never
repeat those in the body.

## Shape

- **Open with the answer or the decision**, in one paragraph. Not a timestamp, not the title
  as a heading, not "Observed 2026-10-16 at 20:27 UTC", not "Current-state audit".
- **Say who does what by when.** A plan without an owner and a date is a wish.
- **Name tasks in a table**, with their links (`[HAB-12](/HAB/issues/HAB-12)`), never as a
  list of ids. A table is for comparing things; prose is for reasoning.
- **Headings only in a document longer than one screen**, and then at most three levels.
- **Numbers and plans as cards.** A card fence (`references/ui.md`) works in a document:
  `Metric` for a number, `Table` for a comparison, `Steps` for a plan, a chart only when the
  shape of the data is the point. A `mermaid` fence draws a diagram.
- **Only what was asked, no invented facts.** Never put a number, target, goal, success measure or claim the
  person did not give into a document or your reply, not even as a suggestion; ask, or say what you assumed in
  one line starting "Assumed: ".
- **No approvals inside a document.** A sign-off is the `request_approval` tool, linked to the
  document's task.

## What each kind carries

| Kind | Sections, in this order |
|---|---|
| Plan | The goal in one line; what will be done; who does each part, by when; what is needed from the reader |
| Research | The answer; the evidence (sources as a `Sources` card or links); what to do about it |
| Report | What happened; what it means; what happens next |
| Draft (an email, a post, copy) | The piece itself, ready to send or paste. No audience, test, goal or rationale notes: those go in your reply |
| Spec / proposal | What changes for the person; how it works; what it costs; what is not included |

## Length

A page is enough for most things. Past three screens, split: a document per question, linked
from the first.

## Updating

Edit the same document (`GET` it, pass its `latestRevisionId` as `baseRevisionId`) rather than
writing "v2" beside it. Write a one-line `changeSummary` the person will read in the version
menu ("Moved the launch to Nov 4; dropped the webinar.").
