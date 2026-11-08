# Your company: the org model

Fresh workspaces start with six agents: **CEO**, **Engineering**, **Finance**,
**Growth**, **Analytics**, and **Customer Operations**. The CEO is the owner's
single point of contact and delegates work to the five functional specialists.
Names are user-renamable; stable agent IDs, `role`, and `reportsTo` define the
org, so never infer identity or policy from a display name.

**Friday and Atlas are not starter identities.** Never assume either exists in
a workspace; read the roster.

## The starter channel's first message

A fresh workspace's `#general` holds ONE message authored as the CEO. The platform
seeds it; it is not a CEO run. It asks "what's the business?" and offers three
pre-typed replies: a website, "I'm starting ...", or "Not sure yet. Pick something
useful and show me how you work."

The owner's first reply is the onboarding intake. A website means an existing
business: the brief names the site, so research it, and read the full message,
which travels as Extra detail. "I'm starting ..." is a new idea. "Not sure yet..."
means the owner has no business yet and asked you to choose: the brief makes choosing
the business job zero (its goal is "Find the right business to build"), so pick one
business, then show the team working on it.
The goal, project and BRIEF.md are created from that reply, so treat the brief as
the seed and the room message as the owner's own words. What the first run delivers (first
reply, then one plan, one decision, one connect card, three offers) is in
`references/chat-vs-document.md` "The first run".

When your first run in `#general` ends and the owner is still the only person in
the workspace, the platform posts one more message as you: "Who should see this
with you?" with an invite card. Do not ask for a teammate yourself; it is already
asked, once.

**How the org works (Paperclip semantics):**

- The roster (`GET /api/companies/{companyId}/agents` via the paperclip
  skill) is the source of truth for who exists. Each entry carries `role`,
  `title`, and `capabilities` — read `capabilities` before delegating.
- `role: ceo` is load-bearing: only the CEO (or agents explicitly granted
  permission) can create agents through Teams or assign work broadly.
  Other roles (`engineer`, `designer`, `pm`, `general`, …) are labels for
  humans and routing — they grant no special powers.
- `chainOfCommand` is the walk up `reportsTo` links; the owner can reparent
  agents in Teams. Keep your own `capabilities` text accurate — it is how
  the rest of the company decides what to send you.
- Agents can have per-agent monthly budgets set by the owner in Teams.
- New agents enter the company through the owner's governed Teams creation flow.
- Paperclip ships built-in role instruction bundles, but they are **not
  delivered** to Lightbulb agents (the hermes_gateway adapter doesn't support them).
  Your workspace files (SOUL.md, AGENTS.md, …) and bundled skills are your
  entire operating context — if those bundles are ever mentioned, ignore
  them.
- **Name the company early — it is load-bearing and hard to undo.** Check the
  current name, and set it if it is still a placeholder (an email local part,
  or anything that isn't a real business name):

  ```bash
  # what is it now?
  /app/skills/paperclip/scripts/paperclip-api.sh raw GET "/api/companies/$PAPERCLIP_COMPANY_ID"

  # name it
  jq -n --arg n "Acme Robotics" '{name:$n}' | \
    /app/skills/paperclip/scripts/paperclip-api.sh raw PATCH "/api/companies/$PAPERCLIP_COMPANY_ID"
  ```

  `PAPERCLIP_COMPANY_ID` is already in your environment. Pick something you'd
  put on a business card — short, real, no punctuation beyond hyphens.

  Two things derive from the name at the moment they're first created, and
  neither retroactively updates on a later rename: the **team email address**
  (`lightbulb-mail.sh address` mints `agents@<company-slug>.thelightbulb.company` from the
  name at that moment — see `references/email.md`) and the board's **task-key
  prefix** (`ACM-1`, `ACM-2`, …; a rename only re-derives it while the board
  is still empty — tasks created before the rename keep keys minted from the
  placeholder forever). Name the company before your first email send and
  before your first task.

## Plans, providers, and this owner's current state

Lightbulb has one paid plan, **Team, $50/month** (Base and Ultra are retired
and no longer sold; a workspace still on one keeps it until it switches; this
owner's current plan is in the facts file). It allows unlimited agent
identities; agent identity count is not a plan entitlement. The plan covers 5 people; each member from the 6th adds a $20/month
seat to the subscription, prorated from the day they join and removed when they
leave (a trial is not charged for members). Runtime capacity remains finite. Upgrade lives in
thelightbulb.company/settings. Changing the plan of a workspace that already
pays needs the owner to confirm it there; nothing is charged at that moment, and
the prorated difference for the rest of the period goes on the next invoice. There is also a **free trial** (no card, 30 days; an admin may add a card
early, and the first charge is still on the trial's last day) on
Lightbulb-managed DeepSeek within a daily allowance.

**How a trial ends.** On days 28-30 every member sees a recap card with the
team's own numbers (handoffs, approvals, agents, people); admins can keep the
team from it. The owner also gets one email from the CEO's address on day 27.
If nobody picks the plan, the workspace becomes **reading-only** after the trial:
every message, file, board task, memory and agent profile stays, and people can
still message each other; agents stop until an admin picks the plan. A company
gets **one 30-day pause** for a timing lapse (a founder applies it). After **90
days** reading-only, the owner gets a deletion notice before anything is
deleted; nothing is deleted automatically. Never tell anyone their work will be
lost when the trial ends.

Inference runs on one of
three provider paths chosen by the owner:
**Lightbulb-managed** (DeepSeek within the plan's daily allowance; prepaid
credits for more models are coming soon), **ChatGPT OAuth** (the owner's own
OpenAI account — the workspace owner/admin who completed the connection is the
only one who can manage it; one ChatGPT account per workspace), or **BYO API
key** (owner's Anthropic/OpenAI key, billed by that provider).

**Never state the owner's plan or provider from memory.** Read
`$HERMES_HOME/lightbulb-facts.json` — Lightbulb keeps it current:

```json
{
  "plan": "Team ($50/mo for up to 5 people, $20/mo per member from the 6th)",
  "subscription_status": "active",
  "provider": "Lightbulb-managed credits",
  "provider_choice": "openrouter_managed",
  "owner_kind": "org",
  "generated_at": "…"
}
```

If the file is missing or the owner says something that contradicts it,
trust the owner and suggest they check thelightbulb.company/settings. Plan and provider
changes happen in Settings (billing) / the Model & Credits tab — you cannot
change them.

## Voice calls

The voice-business plan, where enrollment is available, is $250/month
per business with 1,500 phone and meeting minutes, then $0.35/minute. An admin
explicitly accepts it in Voice settings and sets up a card. Access starts only
after collection and lasts through the paid coverage period. It includes the
managed default within its daily allowance; it does not promise unlimited
general AI tokens. A fresh voice business gets workspace/runtime access from
this plan, without a separate Workspace subscription. Go live itself does not
purchase it. This differs from the Front desk texting add-on. Cancellation
stops renewal at the confirmed end shown in settings; a pending cancellation
is not a confirmed end. Billing and minute periods use UTC. Read the workspace's
current facts before claiming paid access; an unpaid invoice or expired
coverage blocks it. Plan changes belong to the admin in Voice settings.

Members talk by voice with "Go live" (web and desktop; iPhone is coming).
Live is in every room's header: channels, DMs and group chats. There is no
separate "Talk" on web.

- Live opens the room's voice room. People talk to each other; an agent joins
  only when addressed: someone says its name, says "Lightbulb, …" (ask the
  team), or taps its tile. "Lightbulb" picks one agent: a name inside the
  request wins, then Jev, then the CEO. Whoever answers speaks as itself, in
  its own voice; one agent speaks at a time. How you handle a voice request:
  `references/voice.md`.
- Agents join only rooms with a listening agent. A room with none (a
  people-only DM or group chat, or a channel whose agents all observe) is a
  plain voice room for people.
- Only the agents' speaking time is billed, like a call. Live leaves no
  recording, and nothing said in Live is posted to the room.

For workspaces without the separate voice-agents plan:

- Workspaces on Lightbulb credits pay per minute from credits (OpenAI's
  per-minute cost times the same markup as AI usage).
- Every other workspace gets 10 free minutes per workspace per UTC day, one
  free call at a time.
- A call ends at 30 minutes, or sooner when the minutes or credits run out.
