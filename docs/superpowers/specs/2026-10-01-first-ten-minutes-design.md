# The first ten minutes: the team shows up before you type

Date: 2026-10-01. Base: origin/main. Owner session: lightbulb-e3. Disposable per
CLAUDE.md: delete when the first piece ships.

## What we verified (prod and code, 2026-09-30 to 10-01)

- 42 prod workspaces; 37 have one member. Join links in 30 days: 5 minted,
  6 redeemed. 145 sessions ever shared, all by 3 people in our own workspace;
  0 share links, 0 pick-ups, 0 ask-to-join.
- Shafil (org_3K1uYfqun7Mx6TYNhFn4V1S1xxm): solo sign-up from Reddit's
  in-app browser, workspace ready in 51 s, read the CEO welcome, never typed.
  Paperclip board: 0 goals, 0 issues after three CEO heartbeats that all
  succeeded. Nothing broke.
- Onboarding today: Clerk sign-up, name a workspace, provisioning seeds six
  agents, two rooms, one welcome message and paused routines. No goal, no task.
  The owner's FIRST MESSAGE in #general is the intake: it creates the goal,
  project and BRIEF.md (`onboarding_brief.capture_first_workspace_message`,
  path hardcoded "new"). The old new/grow/idea questionnaire and bootstrap
  dispatcher were removed 2026-09-11 (`4c09a4285`).
- The nightly CEO wake is board hygiene only (`heartbeat_checklist.py`): room
  tools forbidden, owner email removed. The daily digest sends only when
  approvals, assigned issues or mentions exist; a fresh board has none.
- The welcome email promises "the CEO wakes overnight, moves one thing forward
  and emails you". For a silent owner that cannot happen.
- 27 of 42 workspaces signed up with free mail; the email domain says nothing.
  Workspace names are usually a business name.
- Emails in the last 7 days: 6 welcome, 82 digests to 16 people, 19 routine
  emails from agents to their owners (replies thread back to the agent).
  Opens, clicks and replies to platform mail are not tracked.
- Analytics: #3510 (door + sharing events) and #3508 (first-day events, trial
  events on the person) are merged and live in prod since 13:44Z; verified in
  PostHog. Codex's depth PR #3523 is a draft; coordination asks are on it.
- PostHog Workflows sends email from an event or cohort trigger with delays and
  conditions and records `$workflows_email_opened` / `_link_clicked`.
- Sending a message during provisioning is not gated (`gate_chat` checks
  subscription, cap, credits only); the work consumer retries "runtime is not
  provisioned yet" every 10 s until the runtime exists (`openclaw_work.py:443`).

## Owner rulings

1. Analytics first. Done.
2. The growth problem is the door and the first day, not the sharing surface.
   Zero sharing usage proves nothing; the feature is unannounced.
3. The wow moment must not wait for the user. The team shows up first.
4. One first message, static, zero tokens. Tokens are spent only when a human
   acts. Not a staged five-message transcript.
5. No hold. The chat is on screen immediately; the CEO's message streams in
   while the runtime sets up; a reply typed during setup goes through the normal
   path and is delivered when ready.
6. No acknowledgement line for returners; they wait.
7. Ask one question, once: what's the business (a link, a line, or "you pick").
8. Make the welcome email true. Follow-ups go through PostHog Workflows, from
   the CEO's address where possible.
9. Invite at the peak moment, and let members invite. Later.
10. Sharing beyond the workspace (helper link, hand-off link, download) waits
    until a second person exists. Cross-account pick-up test with Aden at the
    end of all of this. REVIEW - we will build this after, we had discussed making this publis so we may not need a second person but this is in the future. 

## The first piece (bounded)

### The message

Posted by the CEO when #general is created, replacing `DEFAULT_ROOM_WELCOME`.
Only `{Workspace}` is interpolated. Same idempotency, author and no-dispatch
posture as the welcome (`messaging.py` ~1812).

> Welcome to {Workspace}. I'm your CEO. Growth, Engineering, Finance,
> Analytics and Customer Ops are in this room with me.
>
> One thing gets us started: what's the business? A website or a sentence is
> plenty.
>
> → Here's our website: …
> → I'm starting …
> → Not sure yet. Pick something useful and show me how you work.
>
> I'll turn your answer into a plan and hand the first pieces to the team.
> You'll watch them work here.

### The pre-typed answers (approved 2026-10-01)

Three suggested replies above the composer, shown only while #general has no
human message yet. A tap PREFILLS the composer (cursor at the end); it never
sends. The person completes the sentence and presses send, so the human stays
the author of the first message. Same behaviour for all three.

| Chip text | Situation | Path the brief takes |
|---|---|---|
| `Here's our website: ` | existing business; they paste a link | `grow` — a URL in the first message switches the path from the hardcoded `new`, so the brief says "existing business" and the CEO researches it |
| `I'm starting ` | new idea; they finish the sentence | `new`, as today |
| `Not sure yet. Pick something useful and show me how you work.` | no idea yet; complete as written | the existing "you pick" block in the brief template |

Free typing is unchanged. Someone who ignores the chips gets today's behaviour.
The iPhone app gets the chips in a later piece; web covers the in-app-browser
case Shafil was in.

Each reply is the first message and creates the goal, project and brief
exactly as today.

### The screen

- Fresh trial workspace: `ChatLayout` renders the chat immediately. The
  stepper's container/ready phases become a one-line status above the room
  ("Your team is setting up") instead of wrapping it. Payment and provider
  phases for paid flows are unchanged.
- The CEO's message streams in with a typing reveal of the stored row
  (client-side, cosmetic).
- The three replies are suggested-reply chips that prefill the composer (see
  the table above). No agent-card mechanism.
- Nothing is held. A reply during setup is stored, dispatched, retried until
  the runtime answers.

### Build check before merging

The run ledger's never-started clock must exceed the remaining setup time, or
a reply at second ten could be closed as an error before the runtime answers at
second fifty. If it is tight, lengthen the clock; do not add a hold.

### Measured with what is live

`room_opened`, `room_message_sent{first_in_room}`, `onboarding_ready`:
share of new workspaces with a first message within 10 minutes, and within a
day. Nothing new to instrument.

### Not in this piece

Returner acknowledgement, nudges, email changes, questionnaire pages, new
tables or alarms, multi-message transcripts.

## After this, in order

1. **Make the welcome email true**, then follow-ups via PostHog Workflows:
   `onboarding_ready` → 10 min → no `room_message_sent` → webhook posts the
   CEO's nudge in the room; 24 h → email, opens and clicks tracked. One webhook
   route and copy on our side. Vet copy first.
2. **Invite at the peak**: when the first plan lands, the CEO asks who else
   should see it; join-link minting opens to members (one-line policy change in
   `routers/orgs.py`).
3. **Email tracking beyond PostHog's own sends**: UTM on every platform link now;
   SES open/click/bounce events into the existing `ses-events` webhook later.
4. **Depth analytics**: Codex's #3523 rebased onto main, one capture helper,
   reviewed and dev-verified by this session.
5. **Sharing beyond the workspace** and the Aden cross-account test, when the
   numbers show a second person to share with.
