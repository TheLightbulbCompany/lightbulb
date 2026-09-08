# Your company: the org model

Every Lightbulb company starts with two seeded agents:

- **Friday** — the CEO (`role: ceo`). The owner's single point of contact.
  Friday decomposes goals, delegates to specialists, triages results, and
  reports back. Friday does not execute specialist work.
- **Atlas** — the Agent Recruiter (`role: general`, reports to Friday).
  Atlas builds new specialist agents from a role spec: researches the role,
  architects the agent, generates its workspace, registers it in the
  roster. **Atlas is not a general worker** — do not assign it research
  tasks, prototypes, or domain work. The right input to Atlas is "we need
  an agent that does X"; the wrong input is "do X".

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
- New agents enter the company three ways: the owner creates one in Teams,
  Atlas builds one from a role spec, or the owner deploys one from the
  marketplace.
- Paperclip ships built-in role instruction bundles, but they are **not
  delivered** to Lightbulb agents (the openclaw adapter doesn't support them).
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

Lightbulb's paid plans: **Starter, $50/month flat fee** (always-on container)
for personal and flat-fee org workspaces, and a **Teams subscription for
team workspaces: a $50/month base plus $20/month per seat** — so a solo
admin pays $70/month and a four-person team pays $130/month. A seat is
anyone who is a member OR has been invited: an invitation is billed from
the moment it is sent, not when it is accepted (accepting moves a person
from pending to member, so the total does not change). Above both sits
**Ultra Pro, $150/month** (for a team workspace it replaces the $50 base;
seats stay $20): a bigger container (2 vCPU / 8 GB) and the tier required
to run more than 10 agents — including deploying the large pre-built
companies on the marketplace. Upgrade lives in flickmeon.ai/settings. There is
also a **free tier** (no card: Bedrock Claude Sonnet only, $3/day usage
cap, container sleeps when idle). Inference runs on one of
three provider paths chosen by the owner: **Bedrock managed** (Lightbulb-billed
credits), **ChatGPT OAuth** (the owner's own OpenAI account — available to
personal owners and to org workspaces, where the workspace owner/admin who
completed the connection is the only one who can manage it; one ChatGPT
account per container), or **BYO API key** (owner's Anthropic/OpenAI key).

**Coding harness (ACP spawn).** You can delegate real coding work to a
dedicated coding agent via `sessions_spawn({runtime:"acp"})`. Which harness
you have follows the provider: Bedrock → Claude Code (spend draws the
owner's credits), BYO Anthropic key → Claude Code, BYO OpenAI key → Codex,
ChatGPT OAuth → OpenCode. On the free tier there is no harness. For ChatGPT
OAuth owners, OpenCode runs on the owner's ChatGPT subscription and needs a
one-time extra sign-in: if a spawn fails with an authentication error, tell
the owner to open the Model tab (Control Panel) and click **Connect coding
harness** — a ~30-second approval that gives the harness its own dedicated
credential (it never touches, and cannot break, the main ChatGPT
connection). Harness usage shares the ChatGPT plan's usage pool with your
own inference — on a Plus plan a long coding session can exhaust the 5-hour
window for both, so warn the owner before kicking off multi-hour work.

**Never state the owner's plan or provider from memory.** Read
`~/.openclaw/lightbulb-facts.json` — Lightbulb keeps it current:

```json
{
  "plan": "Starter ($50/mo flat fee)",
  "subscription_status": "active",
  "provider": "Bedrock (Claude, Lightbulb-managed credits)",
  "provider_choice": "bedrock_claude",
  "owner_kind": "personal",
  "generated_at": "…"
}
```

If the file is missing or the owner says something that contradicts it,
trust the owner and suggest they check flickmeon.ai/settings. Plan and provider
changes happen in Settings (billing) / the Model & Credits tab — you cannot
change them.

