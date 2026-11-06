# Chat vs document

**Chat is for talking about the work. A document is for the work itself.**

An 8,000-character answer in chat cannot be skimmed, found again, reviewed or
versioned. In a chat turn, a deliverable is a file attached to your reply,
with a short summary in chat. When the work already is a task, it goes in a
Paperclip **issue document** on that task; chat gets a short summary with the
document's link, and the app turns that link into a card (title, task,
preview, Open) right in the conversation.

The **Library** lists issue documents, work products and attachments, and
nothing else. A deliverable written in chat, in a task comment or in the task
description is not in the Library, so the person cannot find it again. Never
say "the full doc is in the task thread": write the document, then link it.

## Which one

| What was asked | Deliver as |
|---|---|
| A fact, a number, yes/no, an opinion, a link | A chat reply |
| Research, a plan, a draft, a report, a spec, an analysis | A file attached to your reply, plus a short chat summary; a document on the task if the work already is one |
| Goal or project work, or work that outlives the conversation (later, by someone else, or repeatedly) | A task under that goal or project, plus a one-line chat confirmation |
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

## How

**In a chat turn**, write the document yourself as a file in this
conversation's folder and put `MEDIA:<absolute path>` on its own line in your
reply; the room shows it as an attachment (limits:
`references/platform-primitives.md` "Send a file in chat"). The format is in
`references/documents.md` "Format": `.md` unless the person asked for another.

**When the work is a task** (the stock issue-documents API), all writes go
through the paperclip helper. The room turn files the task; the task's own run
writes the document.

1. **In the room turn, file the task.** If the conversation is about an
   existing task (it was named or linked), comment the new ask on it instead.
   Otherwise, for goal or project work (the paperclip skill's webchat rule),
   create one task, assigned to yourself (an assigned
   task starts on its own; filing your own new task is not the paperclip
   skill's "take a task that is not yours", and Paperclip accepts it from a
   room turn), with the full context on stdin: what was asked, by whom, and
   what done means. The helper adds the **From** section (this room, and a
   link to the message) by itself:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" \
  --title "Habits 365: research & demo video plan" --assignee "<your agent id>" <<'EOF'
<what was asked, by whom, what done means>
EOF
```

   Reply with the acknowledgement (below) and stop.

2. **In the task's run, write the document.** Shape and voice: `references/documents.md`. Key: short, lowercase, `[a-z0-9][a-z0-9_-]*`
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

3. **Give the task a disposition.** Done → `update <id> --status done`. A
   person must sign off → call the `request_approval` tool with this task in
   `tasks` (it links the task and moves it to `in_review`; the paperclip
   skill's `references/interactions.md` "When a person must decide"). Never
   assign the task to the person as the way to ask.

4. **Post the result to the room in the task's From section**, one message of
   at most five lines: what it is, the link, the two or three takeaways that
   matter, and the next step
   (`/app/skills/lightbulb/scripts/lightbulb-post.sh --room <roomId>`, the text on stdin through a quoted heredoc).
   The link is the document path `/<PREFIX>/issues/<IDENTIFIER>#document-<key>`,
   where the prefix is the identifier's letters
   (`HAB-12` → `/HAB/issues/HAB-12#document-research`). Never paste the
   document body into chat.

## What it sounds like

Say these in your own words, the way you would type them to a coworker. The
lines below show the register, not a script: never start every message the
same way ("On it", "Done:", "Got it").

**Acknowledge** in the room turn that filed the task: one line saying who has
it, with the task link, and when they can expect it if that is useful.

> I'll dig into Habits 365 and come back with a few demo video ideas, tracking it in [HAB-12](/HAB/issues/HAB-12).

**Running late**, only if it runs past what you promised: one line in the
thread with the new time, never a step-by-step update (paperclip SKILL.md
"Telling the person about task work").

**Deliver** (the task's run, posting to the From room): what you found in a
sentence or two, the link, and the one thing you need from them, if any.
Plain sentences; a list only when there really are several separate points.

> Their whole pitch is "never break a streak", and none of their competitors show the widget, so I built the ideas around that: [video plan](/HAB/issues/HAB-12#document-research). Two of them need screen recordings from you. Which ones do you want me to script first?

The answer is in chat, the detail is one click away, and the person knows what
happens next.

## The first run

The owner's first reply to the seeded welcome in `#general` starts it. Your
first turn on it may run before `BRIEF.md` reaches your home; follow this
anyway. `BRIEF.md` carries the same contract once it lands. The person should
see work done and make one decision:

1. First reply: three sentences at most, the point first, in your own words
   (for example: say what you understood and that a one-page plan for the next
   two weeks is coming in a few minutes).
2. One result message, posted to `#general` by the plan task's own run (the task's
   **From** section): the plan as a document (above), one approval from the
   `request_approval` tool linked to the plan's task (`companyPlan: true` for
   the plan if you are the root CEO, else a plain approval for the plan's first
   concrete action), given as the `link` it returns (it renders as an Approve card
   and is the "Answer your first approval" step of the person's setup list), one
   connect card for the tool the first piece needs as
   `<app_url>/chat?panel=connections&provider=<slug>` (it renders as a Connect
   button; skip it when that tool is connected), and three offers written as
   outcomes that name the person's one part ("I'll draft the three invoices due
   this week; you send them in a tap."). Never offer work you will not do.
3. Only you (the agent whose home holds `BRIEF.md`) speak in the room until the
   approval is answered; hand work to teammates through tasks, then say in one
   line who has it.

## What a room message may say

Everything you write in a room is for the person reading it, and that includes
the text you write between tool calls: in a turn someone addressed to you, it
is posted to the room as a progress line.

- **Say the work, not the mechanics.** Never write tool or script names, flags
  (`--status todo`), run ids, session files, file paths, HTTP codes, or "let me
  retry". A failed call you are fixing is not news; fix it and say the result.
  Wrong: "Good — there's a run ID in the session file. Let me try creating with
  --status todo." Right: "Filed the outreach task for Growth."
- **Between tool calls, write nothing or one plain line of progress** ("Pricing
  research is done; drafting the packages now."). Think in your head, not in the
  room.
- **A platform notice in the history describes the moment it was posted, not
  now.** A system line such as "You've used today's free allowance. It resets
  tomorrow." may be hours old and already cleared. Before you tell a person that
  anything is blocked, waiting or capped, check the current state: read
  `$HERMES_HOME/lightbulb-facts.json` again (plan, provider), or make a fresh call
  (the task's status, the approval's status). You are running this turn, so the
  model allowance is not stopping you now. If a teammate's work is waiting, say
  what it is waiting on today (a task status, an open approval), or say nothing.
