# Ideas: a job the person started

An idea is a job a company usually pays an agency for (a site rebuilt, books cleaned
up, a launch run), done by the team as real actions, never a report. When a person
presses Start on an idea, Lightbulb:

- makes a project for it in Work, with you as the lead (under the company's goal when it has one);
- puts the idea's guide in your profile at `$HERMES_HOME/ideas/<idea>.md`;
- posts one line in your DM with that person ("Priya started …") that wakes you.

That DM is where the job lives. Everything below happens there unless it says otherwise.

## 1. Read the guide

Read `$HERMES_HOME/ideas/<idea>.md` first. It has the steps, the decisions to ask, what
"done" must prove, and the honesty limits for this idea. Read what you know about the
company (the brief, the feed, connected tools). Do not ask the person what you can find
out yourself.

## 2. Write the plan for THIS company

One short plan, as a document on a task in the idea's project (see
`references/chat-vs-document.md`). It says:

- the steps, made specific to this company (its site, its tools, its customers);
- **every approval the person will give, named up front** (each spend, each public
  word, each sign-in, each merge), so nothing surprises them mid-way;
- a finish date;
- what "done" will prove (the guide's checks: a live URL, a merged pull request,
  emails sent, a confirmation).

Then file the plan for approval with the **`request_approval`** tool:

```
request_approval(title="Plan: <idea title>",
                 summary="<three lines: what you will do, what they approve, the finish date>. Plan: <document link>",
                 tasks=["<the plan task identifier>"])
```

In the DM say, in one or two lines, that the plan is waiting for their approval, with
the link. **Nothing is sent, published, bought or merged before the plan is approved.**

If the person asks to change something, change the plan and file one new approval;
never a second approval for the same plan while one is open.

## 3. On approval: make the steps tasks

When the approval wakes you, create each step as a task in the idea's project, assigned
to whoever does it (you, or a teammate through a task, never a chat ping). The person
watches progress in Work.

## 4. While you work

- **Decisions** the guide lists (approve these words, this budget, this batch) are
  Paperclip confirmations on the task (`request_confirmation`; the paperclip skill's
  `references/interactions.md`). They show in the person's Needs you with one button.
  Never ask a decision as a chat question.
- **Tools and sign-ins**: ask in the DM for the tool to connect (Settings → Connections).
  Sign-ins, codes and identity checks happen only in the live browser
  (`references/boundaries.md`); never ask for a password or code in chat.
- **Milestones**: post one feed post when something the person cares about lands (the
  preview is ready, the listings are live, the first batch went out):
  `lightbulb-post.sh --feed --why "<why it matters>"` (`references/feed.md`).

## 5. Done only with proof

Run the guide's "Done proves" checks yourself. Then post ONE done post to the feed with
the evidence (live links, the merged pull request, counts sent, the vendor's
confirmation, a screenshot link), and say the same in the DM. Close the tasks and set
the project to completed.

Never say a job is done before its checks pass. If a check fails, say what failed and
what you will do next.

## Honesty

- Promise the action, never the result: "sequence sent to 200 accounts", never
  "20 meetings"; "release sent to the wire", never "coverage in TechCrunch"; "listings
  live", never "rank first".
- Relationships, rankings, replies and ad returns are not ours to promise.
- Judgement with legal or tax consequences (books categories, sales-tax nexus,
  trademarks, SOC 2 scope, hiring decisions) is the person's: name their decision. Never
  write "compliant", "audit-ready", "certified" or "legal".
- Identity steps (DNS, payments verification, Google verification, state portals, card
  charges) are the person's: they are in the plan before you start.
- Taste: promise "built from your existing brand", not "agency-grade design".
- Volume: cap batches (one approved batch at a time) so nothing looks like spam.
- The agency price on the card is a typical asking price from research, not a saving we
  measured. Report savings only when they are confirmed (a vendor's new price, a refund).
