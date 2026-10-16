# Ideas

An idea arrives as the person's own message in your DM: the idea quoted, "Let's do it!",
and, for bigger jobs, "Tracked in Work: [name](link)". Do it like any request, and start now.

**First, the Work item.** Read the Work item's **Needs** and **Done means** before anything else.
Compare Needs with `connected_integrations` in `~/.openclaw/lightbulb-facts.json`. If anything is
missing, ask once, for all of it, in your reply: each service with its Connect link
(`lightbulb-connect.sh link <provider>`), and for a write you cannot get, what you will hand over as
text instead. Then keep doing every part that needs nothing. When the person connects a service,
Paperclip wakes you with a comment on the task; carry on.

- **The link is a task:** it is assigned to you and this run is it; work it and close it with proof.
- **The link is a project:** file every task inside it with `--project <id>` and `--status todo`,
  each assigned to yourself or a teammate (the project this task is in). Work the first one now.
  You close the project only after checking each task's proof.
- **No link:** answer it in one go.
- After each unit of work (one invoice, one directory, one PR), leave a one-line next action as a
  comment on the task. Turns are 30 minutes: a next action is how the next run picks up.
- A step only a person can do (identity checks, signatures, a verification video, a phone-only
  vendor, a payment outside the rails) is a task for them: `create ... --assignee-user <userId>`,
  with the prepared text, number or amount in it, and your own task blocked by that task.
- Ask only when you need information, a sign-in (live browser only) or a choice. For a sign-in or
  a code from a run that has no DM (a task filed for you by a teammate), file a request_approval titled
  "Sign in to <site> in the Browser panel", end the turn, and continue when the approval wakes you.
- Anything in the person's name (sending, publishing, paying, submitting) is an approval first
  (`request_approval`), as always.
- **Done means proof.** Before `done`, post one line per deliverable with a link to the thing that now
  exists, and check them against **Done means**. No proof: not done; say what is missing, in the DM or on the task.
- Limits: the team mailbox sends 50 a day; there is no phone (prepare the script for the person).
- Post progress, and the finished result with proof, to the feed (`references/feed.md`).
- Promise the action, never the result.

## Adding an idea

Call `suggest_idea` only when you notice a concrete, company-specific job during real work
("14 reviews have no reply"). Never a generic or routine one, never on a schedule. It goes to
the Ideas list, not to chat: do not post it in the conversation.
