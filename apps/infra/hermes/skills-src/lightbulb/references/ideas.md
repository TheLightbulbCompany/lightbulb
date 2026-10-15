# Ideas: a job the person started

An idea is a job a company usually pays an agency for (a site rebuilt, books cleaned
up, a launch run), done by the team as real actions, never a report. When a person
presses **Let's do it** on an idea, Lightbulb:

- makes a project for it in Work, with you as the lead;
- puts the idea's tips in your profile at `$HERMES_HOME/ideas/<idea>.md`;
- posts the person's own message in your DM with them: the idea quoted above
  "Let's do it!". That message wakes you.

People see only that message. Your turn also carries a note only you see: the idea,
what to deliver, the tips file, and **this start's Work project id**.
Two people can start the same idea with you; each start has its own project and DM.
Anything you file for this job goes in the project the note names, never one found by
its title. The same record stays in `$HERMES_HOME/ideas/starts/<project id>.md` (who
started it, the DM, the deliverables), so read it there if the note has scrolled away.

## Start working right away

Trust yourself: start on the job in this turn. There is no plan to file and nothing to
wait for. Say in one or two lines what you are doing first, then do it.

- **The tips file is tips, not required steps.** Read it, use what fits this company,
  skip what does not.
- Find out what you can yourself first: the brief, the feed, connected tools, the
  company's site. Do not ask the person what you can look up.
- **Ask the person only when you genuinely need to**: information only they have, a
  sign-in, or a real choice between options. One short question in the DM.
- **Sign-ins, codes and identity checks happen only in the live browser**
  (`references/boundaries.md`); never ask for a password or code in chat. To connect a
  tool, send them to Settings → Connections.

## Approval only before acting in their name

Use the **`request_approval`** tool only before an action in the outside world, in the
person's name: sending a batch of emails, publishing or posting publicly, spending or
taking a payment, submitting a form to a vendor or an authority, merging to their live
site. **Always pass this start's project id** (`projectId`): it is how the approval shows
in the person's DM with Approve / Change something. Link the task it is about too:

```
request_approval(title="<the action, e.g. Send the 40 follow-up emails>",
                 summary="<what goes out, to whom, and what it costs if anything>",
                 projectId="<this start's Work project id>",
                 tasks=["<the task identifier in this start's project>"])
```

It shows in their DM and in For you with Approve / Change something. Wait for it before
that one action; keep working on everything else meanwhile. Drafts, research, builds on
a preview and work inside their tools need no approval. Decisions inside a task can also
be Paperclip confirmations (`request_confirmation`, the paperclip skill's
`references/interactions.md`).

## While you work

- Make tasks in this start's project for the work you are doing, so the person can follow
  it in Work.
- **Progress**: post one feed post when something the person cares about lands (the
  preview is ready, the listings are live, the first batch went out):
  `lightbulb-post.sh --feed --why "<why it matters>"` (`references/feed.md`).

## Done only with proof

When the job is done, check it yourself (the tips file's "Done proves" when it has them).
Then post ONE done post to the feed with the evidence (live links, the merged pull
request, counts sent, the vendor's confirmation, a screenshot link), and say the same in
the DM. Close the tasks and set the project to completed.

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
  charges) are the person's: ask for them when you reach them.
- Taste: promise "built from your existing brand", not "agency-grade design".
- Volume: cap batches (one approved batch at a time) so nothing looks like spam.
- Never quote an agency price or a "saving" from the idea. Report a saving only when it is
  confirmed (a vendor's new price, a refund).
