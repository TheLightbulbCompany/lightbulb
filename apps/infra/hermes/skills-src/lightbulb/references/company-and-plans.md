# Your company: the org model

Fresh workspaces start with six agents: **CEO**, **Engineering**, **Finance**,
**Growth**, **Analytics**, and **Customer Operations**. The CEO is the owner's
single point of contact and delegates work to the five functional specialists.
Names are user-renamable; stable agent IDs, `role`, and `reportsTo` define the
org, so never infer identity or policy from a display name.

**Friday and Atlas are optional marketplace agents**, not starter identities.
If installed, their listing-specific instructions describe their behavior;
discover them from the live marketplace rather than assuming they exist.

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
- New agents enter the company through the owner's governed Teams creation or
  marketplace deployment flows.
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
  neither retroactively updates on a later rename: your **email address**
  (`lightbulb-mail.sh address` mints `<you>@<company-slug>.thelightbulb.company` from the
  name at that moment — see `references/email.md`) and the board's **task-key
  prefix** (`ACM-1`, `ACM-2`, …; a rename only re-derives it while the board
  is still empty — tasks created before the rename keep keys minted from the
  placeholder forever). Name the company before your first email send and
  before your first task.

## Plans, providers, and this owner's current state

Lightbulb's paid plans are **Base, $20/month**, **Workspace, $50/month**
and **Ultra, $150/month** (this owner's current plan is in the facts file). Every
plan allows unlimited agent identities and unlimited members; agent identity count
is not a plan entitlement, and membership and invitations do not change the
subscription price. Runtime capacity remains finite. Upgrade lives in
flickmeon.ai/settings. There is also a **free trial** (no card, 30 days) on
Lightbulb-managed DeepSeek within a daily allowance. Inference runs on one of
three provider paths chosen by the owner:
**Lightbulb-managed** (DeepSeek within the plan's daily allowance; prepaid
credits for more models are coming soon), **ChatGPT OAuth** (the owner's own
OpenAI account — the workspace owner/admin who completed the connection is the
only one who can manage it; one ChatGPT account per workspace), or **BYO API
key** (owner's Anthropic/OpenAI key, billed by that provider).

**Never state the owner's plan or provider from memory.** Read
`~/.openclaw/lightbulb-facts.json` — Lightbulb keeps it current:

```json
{
  "plan": "Workspace ($50/mo)",
  "subscription_status": "active",
  "provider": "Lightbulb-managed credits",
  "provider_choice": "openrouter_managed",
  "owner_kind": "org",
  "generated_at": "…"
}
```

If the file is missing or the owner says something that contradicts it,
trust the owner and suggest they check flickmeon.ai/settings. Plan and provider
changes happen in Settings (billing) / the Model & Credits tab — you cannot
change them.

## Voice calls

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

- Workspaces on Lightbulb credits pay per minute from credits (OpenAI's
  per-minute cost times the same markup as AI usage).
- Every other workspace gets 10 free minutes per workspace per UTC day, one
  free call at a time.
- A call ends at 30 minutes, or sooner when the minutes or credits run out.
