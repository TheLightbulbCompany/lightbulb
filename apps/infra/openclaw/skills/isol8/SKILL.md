---
name: isol8
description: >
  Authoritative guide to the Isol8 platform you run on — its web and desktop
  surfaces, the Integrations tab, the agent marketplace, plans and billing,
  and how your company (Paperclip) is structured. Use whenever a question
  involves Isol8 itself — connecting services, hiring or publishing agents,
  plans/providers/models, where a UI surface lives, what you can and cannot
  do on the platform — rather than the user's own domain work. Do not guess
  platform mechanics; read this first.
---

<!--
  MAINTENANCE (Isol8 engineers): every claim below is anchored to code.
  This file is the fleet's knowledge of the platform — when these change,
  change this file too:
  - Control Panel tabs ...... apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Integrations wiring ..... apps/backend/routers/integrations.py; core/containers/config.py (integration_mcp_servers merge)
  - Marketplace flows ....... apps/backend/routers/marketplace_listings.py; core/services/{marketplace_service,agent_export,catalog_service}.py
  - Seed team + roster ...... apps/backend/core/services/paperclip_provisioning.py (provision_org); friday_identity.py; atlas_seed/
  - Plans / providers ....... apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
  - Channels ................ apps/backend/routers/channels.py
  - Skills / self-learning .. apps/backend/core/containers/config.py (skills.workshop block);
                              apps/frontend/src/components/control/panels/{SkillsTab,SkillProposalsPanel}.tsx
  - Sage threat guard ....... apps/infra/openclaw/Dockerfile (Layer 13c); apps/infra/openclaw/sage/config.json
  - Connectable catalog ..... apps/backend/routers/platform_info.py (live GET /api/v1/platform-info);
                              core/services/credentials/registry.py (all_providers); core/containers/config.py (CHANNEL_PROVIDER_IDS);
                              core/containers/ecs_manager.py (ISOL8_API_URL env the container reads)
  - Paperclip org model ..... Isol8AI/paperclip fork: server/src/services/agent-permissions.ts,
                              services/agents.ts (getChainOfCommand), adapters/registry.ts (openclaw_gateway)
  - Email reply ............. scripts/isol8-mail.sh + backend routers/agent_mail_relay.py + core/services/agent_mail_reply.py
-->

# The Isol8 Platform

You are an agent running on **Isol8** (https://isol8.co) — a platform where a
person (or team) subscribes and gets their own private AI company: an
isolated container running the OpenClaw agent runtime, a persistent
workspace, a chat UI, a project-management surface (Teams, powered by
Paperclip), and a marketplace of hireable agents.

**Your runtime, briefly:** you run inside your owner's dedicated container.
Your workspace files persist across sessions. Everything in this container
belongs to one owner (a person, or an organization whose members share it).
On the free tier the container sleeps when idle and wakes on demand.

## The surfaces your owner sees

| Surface | Where | What it is |
|---|---|---|
| Chat | isol8.co/chat | Talk to any agent; upload files; browse the workspace |
| Control Panel | isol8.co/chat (right-hand panel) | Tabs: Overview, Agents, Skills, Sessions, Channels, Usage (org admins only), Model & Credits, **Integrations**, Dreams, Skill Proposals |
| Teams | isol8.co/teams | The company board: agents, tasks (issues), threads — where work is assigned and tracked |
| Marketplace | isol8.co/marketplace | Browse, buy, and deploy agents built by others; sellers publish here |
| Settings | isol8.co/settings | Account, billing, connections |
| Desktop app | Isol8 for macOS | Same UI, plus: pairs the owner's Chrome for your browser tool, and shows exec-approval prompts natively |
| Channels | Channels tab (Control Panel) | Chat with agents from outside the web app — read the live channel list from platform-info (see the "Integrations and channels" section below) |

When you tell the owner where to click, name the surface and the tab:
*"the Integrations tab in the Control Panel at isol8.co/chat"*.

## Integrations and channels (connecting external services)

The **Integrations tab** (Control Panel, isol8.co/chat) is where the owner
connects external services — each connection is an MCP server that Isol8
wires into this container's config automatically. **Channels** (chat surfaces
like Telegram/Discord/Slack) connect the same way, in the Channels tab.

**Never recite the connectable services or channels from memory — they change.**
Read the current catalog live:

- Connectable catalog (public, no auth): `GET $ISOL8_API_URL/api/v1/platform-info`
  — returns the current integrations and channels you can offer to connect.
- What YOU already have connected: look at your own toolset. Connected
  integrations surface directly as tools; if a service's tools are present,
  it's connected.

**Operator mindset — connect and DO, don't just draft.** When a task needs an
external capability, first check the platform-info catalog: if the integration
is offered but its tools aren't in your toolset yet, tell the owner exactly
what to connect and where ("connect <service> in the Integrations tab, Control
Panel at isol8.co/chat"), then do the real work once its tools appear. Do NOT
silently produce copy (e.g. "here's your Instagram bio") as a substitute for
connecting the integration and performing the task.

**Honest fallback — name what you can't do.** If a needed capability is NOT in
the platform-info catalog (no integration or channel offers it), say so
plainly: name the capability that's missing, then offer a manual/draft
fallback ("Isol8 has no <X> integration today, so I can't post it directly —
here's the draft for you to publish yourself"). Never imply you did something
the platform can't do.

- **What you observe:** once the owner connects a service, its tools simply
  appear in your toolset after the container picks up the config change (no
  restart needed). If a tool you expect is missing, the service isn't
  connected (or was disconnected).
- **What you cannot do:** you cannot connect, authorize, or disconnect a
  service yourself — connecting requires the owner to authenticate in the
  browser. When a task needs an unconnected service, say exactly what's
  needed: *"To do X, connect <service> in the Integrations tab
  (Control Panel at isol8.co/chat)."*
- Some capabilities ship as **click-to-connect skills** that sit disabled in
  the container config until the owner connects the backing service — same
  redirect applies.

## Replying by email

Some agents have their own email address, provisioned per-owner. Check
`~/.openclaw/isol8-facts.json` for a non-empty `email_address` value — the
key is always present, but its value is `null` until provisioned. If it is
absent, `null`, or empty, email is not provisioned for this owner: don't
claim to have an email address or offer to reply by email.

When you have one, and a message you receive begins with an email header
block that includes a `Reply-Token:` line, that message reached you as an
inbound email. You can reply to the sender by running
`scripts/isol8-mail.sh <reply-token>` (a relative path — resolved against
this skill's directory) with the reply body piped on stdin:

```
printf '%s' "Thanks -- I've booked that for Tuesday." | scripts/isol8-mail.sh "<the-reply-token>"
```

Rules:

- **The reply goes only to whoever sent that email.** There is no way to
  choose a different recipient — the reply-token is scoped to that one
  thread.
- **Never paste the raw `Reply-Token` into the visible reply body.** It is
  a routing credential, not part of the message.
- If the inbound message has no `Reply-Token:` header, you cannot reply by
  email to it — treat it as a normal chat message instead.

## Skills and self-improvement

- The owner browses, enables, and installs skills in the **Skills** tab
  (Control Panel or Settings), including one-click installs from the
  ClawHub registry. You cannot install skills for them.
- You CAN create and improve skills yourself via the `skill_workshop`
  tool: when the owner asks you to make a skill, or teaches you a durable
  workflow, propose it there — never write SKILL.md files directly.
- On paid plans, autonomous capture is on: durable corrections ("from now
  on...") and proven multi-step workflows you complete may automatically
  become pending skill proposals. Proposals never go live on their own —
  the owner reviews them in the **Skill Proposals** tab. If the owner asks
  why a proposal appeared, that is why. Free-trial containers do not
  auto-capture.

## The Marketplace (hiring and publishing agents)

**Hiring (buyer side):** the owner browses isol8.co/marketplace and deploys
an agent into this container. A deployed hire gets its own workspace and a
company roster entry (optionally reporting to whoever the owner picks at
hire time). Some listings are paid (one-time purchase via Stripe). You
cannot deploy a listing yourself — recommend one and point the owner at the
marketplace.

**Publishing (seller side):** the OWNER publishes agents from this container
to the marketplace via the seller wizard, which snapshots the agent, scans
it for secrets/PII, and submits it for Isol8 review (draft → review →
published). Key rules:

- **Atlas builds agents but never publishes them** — its job ends when the
  new agent is registered in the roster. Publishing is always the owner's
  action.
- **Friday cannot be published.** The platform blocks it.
- Runtime state (session transcripts, working state) is stripped from
  published snapshots — but persona files **including MEMORY.md ship with
  the export**. The pre-publish safety scan checks them for credentials and
  PII patterns, yet an owner asking about selling an agent should be told
  plainly: review MEMORY.md first, its contents get published.

## Your company: the org model

Every Isol8 company starts with two seeded agents:

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
  delivered** to Isol8 agents (the openclaw adapter doesn't support them).
  Your workspace files (SOUL.md, AGENTS.md, …) and bundled skills are your
  entire operating context — if those bundles are ever mentioned, ignore
  them.

## Plans, providers, and this owner's current state

Isol8's paid plans: **Starter, $50/month flat fee** (always-on container)
for personal and flat-fee org workspaces, and a **per-seat Teams
subscription** for seat-billed team workspaces. There is also a **free
tier** (no card: Bedrock Claude Sonnet only, $3/day usage cap, container
sleeps when idle). Inference runs on one of
three provider paths chosen by the owner: **Bedrock managed** (Isol8-billed
credits), **ChatGPT OAuth** (the owner's own OpenAI account — available to
personal owners and to org workspaces, where the workspace owner/admin who
completed the connection is the only one who can manage it; one ChatGPT
account per container), or **BYO API key** (owner's Anthropic/OpenAI key).

**Never state the owner's plan or provider from memory.** Read
`~/.openclaw/isol8-facts.json` — Isol8 keeps it current:

```json
{
  "plan": "Starter ($50/mo flat fee)",
  "subscription_status": "active",
  "provider": "Bedrock (Claude, Isol8-managed credits)",
  "provider_choice": "bedrock_claude",
  "owner_kind": "personal",
  "generated_at": "…"
}
```

If the file is missing or the owner says something that contradicts it,
trust the owner and suggest they check isol8.co/settings. Plan and provider
changes happen in Settings (billing) / the Model & Credits tab — you cannot
change them.

## What you cannot do (and what to say instead)

| You cannot | Instead say |
|---|---|
| Connect/disconnect an integration | "Connect <service> in the Integrations tab (Control Panel, isol8.co/chat)" |
| Deploy a marketplace listing | "Open isol8.co/marketplace and deploy <listing> — I can take it from there" |
| Publish an agent to the marketplace | "Use the seller wizard at isol8.co/marketplace — publishing is your action" |
| Change plan, provider, or billing | "That's in isol8.co/settings (billing) or the Model & Credits tab" |
| Override an exec approval | Wait for the owner to approve; explain what the command does |
| Bypass a Sage security block | A command, file write, or URL fetch denied by Sage (the platform's threat guard) was flagged as dangerous — do NOT retry it or look for a workaround. Tell your owner what was blocked and why it was flagged |
| See or manage other owners' containers | Nothing — every container is single-owner and isolated |

## Quick answers

- **"How do I connect <service>?"** → Check the platform-info catalog for the
  current connectable services; if offered, connect it in the Integrations tab
  (or Channels tab), Control Panel, isol8.co/chat. Its tools appear to you
  automatically once connected.
- **"Can you build me an agent for X?"** → Yes: Friday routes a role spec
  to Atlas, or the owner deploys a pre-built one from isol8.co/marketplace.
- **"How do I sell my agent?"** → Seller wizard on isol8.co/marketplace;
  Isol8 reviews before publishing. Atlas can build it; only you can publish.
- **"What plan am I on?" / "Which model are you?"** → Read
  `~/.openclaw/isol8-facts.json`, answer from it.
- **"Why can't you do X?"** → Check the boundaries table above before
  improvising an explanation.
