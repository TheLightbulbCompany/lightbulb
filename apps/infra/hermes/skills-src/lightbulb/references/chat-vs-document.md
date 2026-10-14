# Chat vs document

**Chat is for talking about the work. A document is for the work itself.**

An 8,000-character answer in chat cannot be skimmed, found again, reviewed or
versioned. A deliverable goes in a Paperclip **issue document** on a task; chat
gets a short summary with the document's link, and the app turns that link
into a card (title, task, preview, Open) right in the conversation.

## Which one

| What was asked | Deliver as |
|---|---|
| A fact, a number, yes/no, an opinion, a link | A chat reply |
| Research, a plan, a draft, a report, a spec, an analysis | A document on a task, plus a short chat summary with its link |
| Something to do later, by someone, or repeatedly | A task, plus a one-line chat confirmation |
| A spreadsheet, PDF or image | An attachment (`MEDIA:` line) or an uploaded work product |

Write a document when **any two** of these hold:

- **Length:** more than about 1,500 characters (one phone screen) or ~20 lines.
- **Structure:** it needs headings, a table, or a numbered plan.
- **Reuse:** someone will come back to it, forward it, or build on it.
- **Review:** it needs approval before anyone acts.
- **Living:** it will be updated later (a roadmap, a tracker, a spec).
- **The ask used deliverable words:** research, plan, draft, write up, report,
  spec, proposal, brief, analysis.

This holds in a DM too. Where you are only changes the chat part: in a channel,
keep the summary short and take the back-and-forth into the thread.

## How (the stock issue-documents API)

All writes go through the paperclip helper. Chat turns have no run id; the
helper omits the header, and every call below works without one.

1. **Pick the task.** If the conversation is about an existing task (it was
   named or linked), put the document on it. Otherwise create one task per
   deliverable, assigned to yourself, **with an explicit `--status backlog`**
   so no new run starts (you do the work in this turn):

   ```bash
   /app/skills/paperclip/scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" \
     --title "Habits 365: research & demo video plan" \
     --assignee "<your agent id>" --status backlog
   ```

   Keep the returned `id` and `identifier` (`HAB-12`). A task another agent
   owns, or one `in_progress` under a live run, refuses a document write from
   chat: file a subtask under it instead (`subtask … --parent <id>`).

2. **Write the document.** Key: short, lowercase, `[a-z0-9][a-z0-9_-]*`
   (`research`, `video-plan`; `plan` for a plan). Build the body with `jq`:

   ```bash
   jq -n --rawfile body doc.md \
     '{title: "Habits 365: research & demo video plan", format: "markdown",
       body: $body, changeSummary: "First version", baseRevisionId: null}' |
   /app/skills/paperclip/scripts/paperclip-api.sh raw PUT "/api/issues/<id>/documents/research"
   ```

   Updating an existing document: `GET /api/issues/<id>/documents/<key>` first
   and pass its `latestRevisionId` as `baseRevisionId`. Link to the **`key` in
   the response**, not the one you asked for: Paperclip may redirect a write
   on a locked document to a new key.

3. **Give the task a disposition.** Done → `update <id> --status done`. Needs
   sign-off → `confirm <id> --prompt "Approve this plan?"`, then
   `update <id> --status in_review` (see the paperclip skill's
   `references/artifacts.md`).

4. **Reply in chat** with at most five lines: what it is, the link, the two or
   three takeaways that matter, and the next step. The link is the document
   path `/<PREFIX>/issues/<IDENTIFIER>#document-<key>`, where the prefix is the
   identifier's letters (`HAB-12` → `/HAB/issues/HAB-12#document-research`).
   Never paste the document body into chat.

## What it sounds like

**Acknowledge** only when the work continues after this turn (you filed a task
a later run will finish). Your reply is then the acknowledgement, and the
finished document is announced with `scripts/lightbulb-post.sh`:

> On it. I'll research Habits 365 and put together 5–7 demo video ideas as a doc on a task; I'll post it here in about 10 minutes.

**Progress**, only if it runs past what you promised, in the thread:

> Quick update: the competitor scan is done, and I'm now drafting the video concepts. About 5 more minutes.

**Deliver:**

> Done: [Habits 365: research & demo video plan](/HAB/issues/HAB-12#document-research)
> 1. Their hook is "never break a streak", so lead with that.
> 2. Competitors don't show the widget; that's our video #2.
> 3. Videos 1 and 4 need screen recordings from you.
> Pick 2 concepts and I'll script them.

The answer is in chat (the takeaways), the detail is one click away, and it
ends with a clear next step.

## The first run (when `BRIEF.md` is in your home)

`BRIEF.md` carries the result contract; this is its mirror. The person should
see work done and make one decision:

1. First reply: three sentences at most, the point first ("Got it: … I'm
   writing a one-page plan for the next two weeks. It'll be here in a few
   minutes.").
2. One result message: the plan as a document (above), one
   `request_board_approval` for the plan's first concrete action linked as
   `<app_url>/<PREFIX>/approvals/<id>` (it renders as an Approve card), one
   connect card for the tool the first piece needs as
   `<app_url>/chat?panel=connections&provider=<slug>` (it renders as a Connect
   button; skip it when that tool is connected), and three offers written as
   outcomes that name the person's one part ("I'll draft the three invoices due
   this week; you send them in a tap."). Never offer work you will not do.
3. Only the CEO speaks in the room until the approval is answered; hand work to
   teammates through tasks, then say in one line who has it.
