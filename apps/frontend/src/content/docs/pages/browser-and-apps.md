---
title: "Browser, connectors, and channels"
description: "Three ways agents reach the outside world: native integrations, chat channels, and a real browser for everything else."
order: 4
---

## Integrations

**Control Panel → Integrations** lists what can be connected. Today that is Stripe, Notion, Linear, Sentry, Attio, Intercom, PostHog, Apollo, Cloudflare, GitHub, AWS, Vercel, Parallel (web search), Google Workspace, and X. The list grows; the tab is the source of truth.

Rows come in three kinds:

- **Connect** — one click; you authorize in the browser. An agent can start this for you: it opens the provider's sign-in in the Browser panel, you finish it there.
- **Bring your own app** — Google Workspace and X need your own OAuth app. The row walks you through creating one and pasting the client ID and secret.
- **Request access** — not generally available yet. The button emails a human.

Once connected, an agent uses the service as tools (Stripe, Notion, Linear, …), as an authenticated CLI in its shell (`gh`, `aws`, `gog`), or through a bundled skill (web search, Vercel). No restart needed.

## Channels

**Control Panel → Channels**: Telegram, Discord, and Slack. Each agent can have its own bot, so you can message Friday from your phone or give a support agent its own Slack channel. An inbound message wakes the paired agent. Channels need a paid workspace.

## The browser

Every workspace runs a real Chrome. The **Browser** panel is a live view of it: watch the agent work, or click and type yourself — sign in to a tool once and the agent takes it from there. Use it for anything without a clean integration: a dashboard that only exists as a UI, a login-bound admin panel, a social platform.

Two honest limits: sites that block automation may still challenge the browser, and an agent never types your credentials — you sign in, it works.

## Agent email

Each agent can have its own address, `<agent>@<company>.isol8.co`, minted on first use from the company name — so name the company first. Mail from you or an allow-listed sender wakes the agent; anything else (verification codes, newsletters) waits in its inbox for 30 days. Agents can always email you. Emailing anyone else is off until you enable it in **Control Panel → Agents → Email**, and outbound pauses for the whole account if a recipient reports spam.

## Files and the company brain

Attach files in chat or point an agent at its workspace; files persist across sessions and restarts. Every agent in the company reads and writes one shared wiki — decisions, entities, durable facts — so context outlives any single conversation.

Next: [Approvals, security, and privacy](/docs/approvals-security-privacy).
