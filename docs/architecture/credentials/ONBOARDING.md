# Lightbulb Credential Layer — Onboarding for a fresh Claude Code session

You (Claude) are picking up an in-progress design workstream. This file is the entire context dump. Read it once and you have everything actionable — there is no prior conversation transcript you need to recover.

## What this project is

**Lightbulb** is a multi-tenant managed-agent SaaS built on top of **OpenClaw** (real, MIT-licensed personal-agent runtime — github.com/openclaw/openclaw, cloned locally at `~/Desktop/openclaw`, HEAD `510628ca6f` as of 2026-05-21). This repo (`~/Desktop/isol8.nosync`) is the Lightbulb platform itself. Each Lightbulb customer gets a dedicated ECS Fargate container running OpenClaw + an EFS workspace. The platform ships seed agents (Friday = CEO/orchestrator, Atlas = recruiter) and has a marketplace where buyers one-click deploy pre-built agents into their own container. The platform team also has its own internal engineering agent ("Jarvis") with email `admin@isol8.co`.

**The active design workstream** is the credential management layer: how third-party SaaS tokens (Slack, Salesforce, GitHub, PostHog, Vercel, Google), agent-identity credentials (Jarvis's GitHub PAT, AWS keys), and platform-operator credentials get provisioned, stored, refreshed, and delivered to running agents.

## The two existing docs (read these in order, both live in this folder)

1. **`problem-discovery.md`** — ~7,500-word problem-discovery report. This is the current source of truth. It establishes:
   - Four personas (platform operator, marketplace seller, end customer, the agent itself)
   - Current OpenClaw state, verified against source with file:line citations
   - Four annotated user-journey failures, one quoted verbatim from a real Jarvis chat
   - Competitor UX research across 10 platforms (Anthropic Connectors, ChatGPT Apps, Lindy, Gumloop, n8n, Pipedream Connect, Zapier, Make, Anthropic MCP API, Workato)
   - **19 testable acceptance criteria in §7** — solution proposals must be measured against these
   - 3×4 problem matrix (3 credential subjects × 4 tool primitives)
   - **Solution categories** (not vendors) with tradeoffs in §10
   - 10 open questions in §11
   - **Vendor selection is deliberately deferred** to the next doc

2. **`solution-brief-stale.md`** (deleted; recover via `git log -- docs/architecture/credentials/solution-brief-stale.md`) — earlier, ~5,500-word solution brief that picked **Nango (self-hosted)** + an env-baseline pattern with per-MCP wrapper scripts. It was superseded by the discovery report and only solved part of the problem (tier 3 / end-customer SaaS OAuth).

## Core verified facts (don't re-investigate)

These were verified against source/docs/the OpenClaw clone. Don't waste cycles re-checking unless you suspect drift.

- **OpenClaw is single-tenant by design.** Repo CLAUDE.md (in `~/Desktop/openclaw`) and docs explicitly state: *"OpenClaw is a personal agent: one trusted operator boundary."* Open issues #60127, #43903, #26370, #71058, #76493 confirm upstream knows the gap.
- **MCP spawn does NOT inherit parent env.** `~/Desktop/openclaw/src/agents/mcp-stdio-transport.ts:50-62` uses MCP SDK `getDefaultEnvironment()` (curated whitelist: PATH/USER/HOME/SHELL/LOGNAME/PWD/LANG/TERM) merged with explicit `mcp.servers[].env` (which today only accepts `string|number|boolean`, per issue #76493).
- **The relevant upstream PRs are real and OPEN:**
  - **#72548** (`feat(secrets): pluggable SecretRef sources via plugin SDK seam`) — OPEN, no review decision, author `akoscz`, last updated 2026-05-20. Adds a plugin seam for new SecretRef sources. **Explicitly defers** opening `mcp.servers[].env` / `skills.entries.*.env` / `plugins.entries.*.config` to accept SecretRef objects.
  - **#82326** (`Add plugin manifest contract for SecretRef provider integrations`) — OPEN, APPROVED. Companion/precursor.
  - **#76493** (`Allow SecretRef objects in mcp.servers[].env schema`) — OPEN, no PR yet. **This is the actual blocker** for retiring the wrapper approach, NOT #72548.
  - **#23096** (Bitwarden/Vaultwarden provider) — OPEN, recent activity.
- **Nango catalog coverage (verified against `packages/providers/providers.yaml` on master, 812 providers):**
  - **Works cleanly:** Slack, HubSpot, Notion, Linear, Asana, Calendly, Monday, ClickUp, Stripe Connect, and all of Google (Gmail/Calendar/Drive/Sheets/Docs/Workspace Admin/Ads/Analytics/YouTube).
  - **Variant-trap providers** (works but pick the right variant): Vercel (OAuth only for MCP product; REST API is `API_KEY` only — upstream limit), GitHub (4 variants — `github-app` recommended for production), PostHog (now has real OAuth at `oauth.posthog.com`), Salesforce (hostname regex rejects `lightning.force.com` URLs), Greenhouse (Harvest variant is Basic auth not OAuth), Stripe (Connect vs api-key).
  - **Out of scope for Nango:** AWS account access via cross-account IAM roles / STS — needs a separate solution (customer-deployed CFN stack with cross-account role + Lightbulb calls `sts:AssumeRole`).
- **Nango doesn't have community/experimental tiers.** All 812 providers in providers.yaml are first-party-maintained. The earlier brief's framing of "verified vs experimental" was wrong.
- **The Jarvis bootstrap problem ≠ the customer-deploy problem.** Conflating them caused confusion in the original brief. Jarvis is a platform-internal agent that needs persistent identity credentials (its own Google for `admin@isol8.co`, its own GitHub, its own AWS role). End-customer agents (Atlas, Friday at a customer site) need *delegated* credentials (the customer's Slack, the customer's Greenhouse). Different lifecycle, different storage, different vendor shape.

## State of the four solution categories (as of last conversation)

| Category | Tentative direction | Status |
|---|---|---|
| End-customer SaaS OAuth (tier 3) | **Nango self-hosted, ELv2** — only platform satisfying all four hard constraints (license, self-host completeness, BYO OAuth per provider, server-side token-fetch). | Researched. Brief written (stale). Discovery doc §10.1 makes the category case. |
| Agent-identity secrets (tier 2) | **Infisical, MIT** — ships an ECS sidecar pattern with AWS IAM auth on machine identities. Production reference: Hugging Face. | Researched. Not yet in the solution brief. The user expressed interest in Infisical's "agent vault." |
| Platform-operator secrets (tier 1) | **AWS Secrets Manager** (existing). One-time human bootstrap per provider. | No change from current. |
| Runtime credential delivery to MCP/CLI/plugin/skill | **`isol8-tool` wrapper shim** (sources env file at exec time). Avoids the MCP `getDefaultEnvironment()` trap. | Designed in §2.3 of the stale brief. Verified to work against the MCP spawn code. |
| **AWS cross-account roles** (new — not yet in any doc) | Customer-deployed CFN template creating a role with external ID + Lightbulb calls `sts:AssumeRole`. Standard pattern. | **Open task** — add as fourth solution category to discovery doc §10. |
| CLI standardization | **Don't adopt Printing Press or OpenCLI.** Printing Press is a build-time CLI generator (wrong layer); OpenCLI explicitly excludes auth from scope. Wrapper shim covers it. | Researched. Decision documented in stale brief. |

## Open work items, prioritized

1. **Add AWS cross-account role assumption as a fourth solution category to `problem-discovery.md` §10.** This is the gap surfaced by Nango catalog research — AWS isn't OAuth-shaped and needs its own pattern. ~30 min.
2. **Decide whether to update the existing brief or write a new vendor-selection brief.** The stale brief covers tier 3 well but is silent on tiers 1, 2, and AWS. Two options:
   - (a) Extend it with §8 (Infisical for tier 2) and §9 (AWS cross-account) and reframe §0 to acknowledge the three tiers — keeps one doc.
   - (b) Write a fresh vendor-selection brief from scratch using the discovery doc's §7 acceptance criteria as the rubric, retire the old brief.
   The user has not chosen between these. Ask, or recommend (b) — the stale brief's scope mistake is structural enough that surgery may take longer than a rewrite.
3. **Resolve the 10 open questions in discovery doc §11** before vendor selection finalizes. Notably:
   - Does Lightbulb want BYO OAuth clients per customer (Pipedream pattern) in v1 or defer to v2? Recommendation: defer.
   - Marketplace manifest schema — needs an RFC. Not started.
   - Multi-account-per-provider (Gumloop pattern) — v1 or v2? Recommendation: v2.

## Things to know (gotchas, conventions)

- The user works on macOS, M1 Pro. Repo lives at `~/Desktop/isol8.nosync/` (`.nosync` suffix prevents iCloud sync). The OpenClaw clone is at `~/Desktop/openclaw` and is a real `git@github.com:openclaw/openclaw.git` checkout — read it directly when you need to verify upstream behavior, don't re-clone.
- **Don't claim OpenClaw is fabricated.** An earlier research agent did, and was wrong. The repo is real. (The agent's skepticism was based on its sandbox not being able to reach GitHub, not on any actual signal.)
- The user pushes back hard and correctly on over-engineering, premature vendor selection, and writing-before-thinking. Match that posture.
- When you don't know something, look at the actual OpenClaw source instead of inferring. The clone is right there.
- Be skeptical of any claim in the stale brief — it predates the discovery report and was written under partly wrong assumptions about MCP env inheritance.
- The repo has a root `CLAUDE.md` with Lightbulb-specific conventions; read that before touching code if your task involves code changes.

## What success looks like for the next session

Pick one of these as your immediate goal, in priority order:

1. **Patch §10 of the problem-discovery doc** to add AWS cross-account roles as a fourth solution category. Concrete, scoped, ~30 min.
2. **Help the user decide** on extend-vs-rewrite for the solution brief, then execute that decision. Use the discovery doc's §7 acceptance criteria as the rubric for measuring any proposed vendor against.
3. **Draft the marketplace manifest RFC** (open question #3 in discovery doc §11) — this is needed before any vendor selection can be implemented end-to-end.

When in doubt, **do not propose a solution before re-reading discovery doc §7**. The acceptance criteria are the contract. Any proposal should be readable as "this solution clears criteria N through M; criteria X through Y are uncovered and need separate handling."
