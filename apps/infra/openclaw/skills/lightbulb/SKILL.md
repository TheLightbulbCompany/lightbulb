---
name: lightbulb
description: >
  How the Lightbulb platform you run on actually works, and the only way to
  send email. Read it when the answer depends on Lightbulb's own mechanics
  rather than on the task's domain: emailing your owner or replying to mail
  (you have your own address and a helper script here — there is no Gmail,
  himalaya, or OpenClaw mail channel to find); connecting a service, channel,
  or webhook trigger; hiring, publishing, or importing an agent; plans,
  providers, credits, the coding harness; where a UI surface lives and what to
  tell the owner to click; setting up Stripe or a paid account for the company;
  what you are and are not allowed to do. Also read it before designing
  anything event-driven, scheduled, or "AI-native company" shaped — the
  platform already has those primitives and a greenfield design duplicates
  them. Never answer a Lightbulb capability question from memory: your priors
  describe other platforms, not this one.
---

<!--
  MAINTENANCE (Lightbulb engineers): every claim in this skill is anchored to
  code. This skill is the fleet's knowledge of the platform. It is SPLIT: a
  thin SKILL.md (routing table + non-negotiables) plus references/ read on
  demand. Each anchor below names the file that now carries the claim.
  - Capability inventory .... references/platform-primitives.md
                              <- routers/teams/issues.py (add_issue_comment wake) + core/services/paperclip_adapter_config.py;
                                 routers/channels.py (SUPPORTED_PROVIDERS — keep the channel list in lockstep);
                                 routers/ses_inbound.py (email wake — allowlisted senders only);
                                 extended-image gog binary (gmail settings watch = Pub/Sub)
  - Control Panel tabs ...... references/surfaces.md
                              <- apps/frontend/src/components/control/ControlSidebar.tsx (NAV_ITEMS)
  - Questions / secrets ..... references/surfaces.md
                              <- apps/frontend/src/hooks/useQuestions.tsx + components/chat/QuestionCards.tsx (question.requested/resolved,
                                 question.resolve incl. secretStoreAllowedHosts); AgentProgressCard.tsx (progressCard.get/changed);
                                 ChatInput.tsx editable queue; core/containers/config.py TOOL_DENY_BASE (ask_user + secrets are NOT denied)
  - Integrations wiring ..... references/integrations.md
                              <- apps/backend/routers/integrations.py; core/containers/config.py (integration_mcp_servers merge)
  - Marketplace flows ....... references/marketplace.md
                              <- apps/backend/routers/{marketplace_listings,marketplace_import}.py; core/services/{marketplace_service,agent_export,catalog_service,claude_code_import}.py
  - Seed team + roster ...... references/company-and-plans.md
                              <- apps/backend/core/services/paperclip_provisioning.py (provision_org); friday_identity.py; atlas_seed/
  - Plans / providers ....... references/company-and-plans.md
                              <- apps/backend/core/services/{billing_service,provision_gate,owner_facts}.py
  - Provisioning rail ....... references/money.md
                              <- apps/backend/routers/company_provisioning.py (container routes, refusal codes);
                                 core/services/provisioning_workflow.py (STATES); core/services/owner_facts.py (the `provisioning` facts list)
  - Channels ................ references/integrations.md <- apps/backend/routers/channels.py
  - Skills / self-learning .. references/marketplace.md
                              <- apps/backend/core/containers/config.py (skills.workshop block);
                                 apps/frontend/src/components/control/panels/{SkillsTab,SkillProposalsPanel}.tsx
  - Company wiki ............ references/platform-primitives.md
                              <- apps/backend/core/containers/config.py (memory_plugin_entries — memory-wiki shared global vault)
  - Brain panel ............. references/surfaces.md
                              <- apps/frontend/src/components/teams/panels/BrainPanel.tsx (Notes/Dreams tabs); BrainPage.tsx + BrainLanding.tsx + brainVault.ts (rendered pages, pills, Related, index cards); apps/frontend/src/components/control/panels/DreamsPanel.tsx (embedded prop)
  - Browser live view ....... references/surfaces.md
                              <- apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx + WorkspaceHeader.tsx (Chrome button);
                                 apps/backend/routers/browser_view.py (VNC relay); apps/infra/lib/stacks/container-stack.ts (Xvnc/matchbox/websockify boot);
                                 core/containers/config.py (browser.headless=false + executablePath — the agent drives real /usr/bin/google-chrome)
  - Sage threat guard ....... SKILL.md, the boundaries table
                              <- apps/infra/openclaw/Dockerfile (Layer 13c); apps/infra/openclaw/sage/config.json
  - Connectable catalog ..... references/integrations.md
                              <- apps/backend/routers/platform_info.py (live GET /api/v1/platform-info);
                                 core/services/credentials/registry.py (all_providers); core/containers/config.py (CHANNEL_PROVIDER_IDS);
                                 core/containers/ecs_manager.py (ISOL8_API_URL env the container reads)
  - Agent-started connect ... references/integrations.md
                              <- apps/backend/routers/connections.py (authorize_from_container — hook-token sibling of authorize;
                                 ISOL8_OWNER_ID + OPENCLAW_HOOK_TOKEN env in ecs_manager.py); core/services/onboarding_brief.py (_INTEGRATIONS_STEP);
                                 /app/skills/lightbulb/scripts/lightbulb-connect.sh (the mint door — never an inline header, see the redaction note)
  - Paperclip org model ..... references/company-and-plans.md
                              <- TheLightbulbCompany/paperclip fork: server/src/services/agent-permissions.ts,
                                 services/agents.ts (getChainOfCommand), adapters/registry.ts (openclaw_gateway)
  - Launch competition ...... references/surfaces.md
                              <- apps/backend/routers/launch.py; core/services/launch_{phase,settings,service,referral,close,revenue}.py
  - Email ................... references/email.md
                              <- /app/skills/lightbulb/scripts/lightbulb-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)
  - Hook-token calls ........ SKILL.md (the non-negotiable) + references/money.md (the status tables)
                              <- /app/skills/lightbulb/scripts/lightbulb-api.sh — every OPENCLAW_HOOK_TOKEN call goes through a script on disk; OpenClaw's
                                 transcript redaction masks that header including $VAR refs, so an agent-typed curl poisons its own next turn (401 loop)
-->

# The Lightbulb Platform

You are an agent running on **Lightbulb** (https://flickmeon.ai) — a platform where a
person (or team) subscribes and gets their own private AI company: an
isolated container running the OpenClaw agent runtime, a persistent
workspace, a chat UI, a project-management surface (Teams, powered by
Paperclip), and a marketplace of hireable agents.

**Your runtime, briefly:** you run inside your owner's dedicated container.
Your workspace files persist across sessions. Everything in this container
belongs to one owner (a person, or an organization whose members share it).
On the free tier the container sleeps when idle and wakes on demand.

## Where to read the detail

Read the one file the question lands on. Paths are relative to this skill
directory (`/app/skills/lightbulb/`).

| Read this | When |
|---|---|
| `references/platform-primitives.md` | Anything trigger-based, event-driven, scheduled, queryable or "AI-native company" shaped. **Read it before designing a system** — the primitives (Paperclip wakes, routines, the shared company wiki, webhook automations, the roster, `.run/`) already exist and a greenfield design duplicates them. |
| `references/surfaces.md` | Where a UI surface lives and what to tell the owner to click — Home, Chat, Control Panel tabs, Teams, Brain, Library, Marketplace, Settings, Browser panel, Docs/Blog, the desktop app. Also the `ask_user` / `secrets` cards and your `progressCard`. Also the Launch Competition. |
| `references/integrations.md` | Connecting an external service or channel, the `actor` rails (`mcp` / `cli` / `skill`), the live platform-info catalog, `requestable` / `byo_required`, one-click connects you can start yourself, and wiring a webhook automation trigger. |
| `references/email.md` | Sending or reading mail: your address, the inbox, verification codes, who can wake you, the three `send` forms, and the limits. |
| `references/money.md` | The owner's **Money** tab; getting the company its own Stripe account; and the Stripe Projects rail that creates paid accounts (analytics, hosting, a database…) on the company's identity and hands you the credentials as env vars. Carries the workflow-state and HTTP-status tables. |
| `references/marketplace.md` | Hiring an agent, recommending one, publishing (seller side), whole pre-built companies, the `skill_workshop` tool and Skill Proposals, and importing an existing OpenClaw / Claude Code setup. |
| `references/company-and-plans.md` | Friday and Atlas, the roster and `chainOfCommand`, **naming the company** (load-bearing, hard to undo), plans and prices, the three provider paths, and the ACP coding harness. |

## Non-negotiables

These hold whether or not you open a reference.

- **Never answer a Lightbulb capability question from memory or first
  principles.** Your generic priors describe other platforms. The catalogs
  change: read integrations live from `GET $ISOL8_API_URL/api/v1/platform-info`,
  marketplace listings live with `marketplace_search`, and plan/provider from
  `~/.openclaw/lightbulb-facts.json`. Never recite any of the three from memory.
- **Never write an `Authorization: Bearer …` header yourself** — not in a curl
  you type, not in a script you write. OpenClaw's transcript redaction masks
  that header *including* a `$OPENCLAW_HOOK_TOKEN` reference, so the copy you
  see next turn is broken and re-issuing it sends a literal `***` (a 401 that
  looks like a platform fault). Use the scripts on disk — their text never
  round-trips through your transcript:
  `/app/skills/lightbulb/scripts/lightbulb-api.sh`,
  `lightbulb-connect.sh`, `lightbulb-mail.sh` (absolute paths — your cwd is
  your workspace, not the skill root).
- **Never print, echo, or paste a credential value** — not in chat, an issue, a
  comment, a work product, or a file. The name, never the value. Ask for a
  credential with the `secrets` tool, never in a chat message.
- **Paperclip routines are the only scheduler.** Never `openclaw cron`: the
  container's cron service is for platform internals, and a job added there
  runs where no one can see it, outside the company's governance.
- **Teams is a panel, not a URL.** `flickmeon.ai/teams` is a 404 — never send
  the owner there. Its API (`/api/v1/teams/*`) is owner-only and never takes
  your container token; `lightbulb-api.sh` refuses those routes before sending.
  Manage routines, tasks, agents and projects through the Paperclip API.
- **Never hand the owner a `localhost` / `127.0.0.1` link.** Your container
  runs on AWS, so its localhost is yours, not theirs. Give the port and point
  at the Browser panel (Chrome button, top of flickmeon.ai/chat).
- **Operator mindset — connect and DO.** If a task needs an external
  capability, check the catalog and connect it (or send the owner to the
  Integrations tab) and then do the real work. Do not silently produce copy as
  a substitute. If nothing in the catalog offers the capability, say so
  plainly and offer a manual fallback — never imply you did something the
  platform cannot do.
- When you tell the owner where to click, name the surface and the tab:
  *"the Integrations tab in the Control Panel at flickmeon.ai/chat"*.

## What you cannot do (and what to say instead)

| You cannot | Instead say |
|---|---|
| Finish (sign in / authorize) or disconnect an integration; start a BYO-app or `requestable` one | One-click ones you can START yourself — mint the link and open it in your Chrome (see `references/integrations.md`). Otherwise: "Open the Integrations tab (Control Panel, flickmeon.ai/chat) and find <service>" — check its catalog flags before claiming it's one click |
| Deploy a marketplace listing | "Open flickmeon.ai/marketplace and deploy <listing> — I can take it from there" |
| Publish an agent to the marketplace | "Use the seller wizard at flickmeon.ai/marketplace — publishing is your action" |
| Change plan, provider, or billing | "That's in flickmeon.ai/settings (billing) or the Model & Credits tab" |
| Delete the owner's account | "Settings → Profile → Delete account (web or mobile) — it permanently deletes your account, agents, and workspace. Workspace/org accounts: contact support@thelightbulb.company" |
| Override an exec approval | Wait for the owner to approve; explain what the command does |
| Bypass a Sage security block | A command, file write, or URL fetch denied by Sage (the platform's threat guard) was flagged as dangerous — do NOT retry it or look for a workaround. Tell your owner what was blocked and why it was flagged |
| See or manage other owners' containers | Nothing — every container is single-owner and isolated |
| Hand the owner a `localhost` / `127.0.0.1` link | NEVER paste one as if they can open it. Your container runs on AWS, so its localhost is yours, not theirs — the link is dead in their browser. Open the page in your own container Chrome instead and say "it's running on port N — open the Browser panel (Chrome button, top of flickmeon.ai/chat) and you'll see it." Same for a dev server you started: give the port and point at the panel, never a URL they cannot reach |
| Import the owner's local OpenClaw setup | "Upload your `openclaw backup create` archive via the publish wizard → Import from an OpenClaw backup — I can't read files on your machine" |

## Quick answers

- **"How do I connect <service>?"** → `references/integrations.md`. Read the
  live platform-info catalog first; one-click rows you can start yourself.
- **"Can you build me an agent for X?"** → Yes: Friday routes a role spec to
  Atlas, or the owner deploys a pre-built one from flickmeon.ai/marketplace.
- **"How do I sell my agent?"** → Seller wizard on flickmeon.ai/marketplace;
  Lightbulb reviews before publishing. Atlas can build it; only the owner can
  publish. Details in `references/marketplace.md`.
- **"Can I bring my laptop's OpenClaw / Claude Code setup?"** → Yes, through the
  same import wizard — `references/marketplace.md`.
- **"What plan am I on?" / "Which model are you?"** → Read
  `~/.openclaw/lightbulb-facts.json`, answer from it.
- **"Why can't you do X?"** → Check the boundaries table above before
  improvising an explanation.
