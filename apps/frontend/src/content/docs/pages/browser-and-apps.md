---
title: "Connectors and agent email"
description: "How agents reach the outside world: native integrations, an authenticated CLI, bundled skills, and their own email address."
order: 4
---

**Agents reach the outside world through connected tools.** Connect a service once and every agent can use it as a tool, as an authenticated CLI in its shell, or through a bundled skill.

## Integrations

**Control Panel → Integrations** lists what can be connected. Today that is Stripe, Notion, Linear, Sentry, Attio, Intercom, PostHog, Apollo, Cloudflare, GitHub, AWS, Vercel, Parallel (web search), Google Workspace, and X. The list grows; the tab is the source of truth.

Rows come in three kinds:

- **Connect** — one click; you authorize in the browser.
- **Bring your own app** — Google Workspace and X need your own OAuth app. The row walks you through creating one and pasting the client ID and secret.
- **Request access** — not generally available yet. The button emails a human.

Once connected, an agent uses the service as tools (Stripe, Notion, Linear, …), as an authenticated CLI in its shell (`gh`, `aws`, `gog`), or through a bundled skill (web search, Vercel). No restart needed.

## Agent email

Each agent can have its own address, `<agent>@<company>.thelightbulb.company`, minted on first use from the company name — so name the company first. Email is open by default: anyone whose mail passes sender authentication can email an agent and wake it, and the agent can email anyone (bulk and automated mail is dropped, and everything it receives is kept in its inbox for 30 days). Turn **Open email** off in **Control Panel → Agents → Email** and only you and the senders you allow-list wake the agent; it can email you and reply to those senders, and no one else. Outbound is subject to a daily limit and pauses for the whole account if a recipient reports spam.

## Files

Attach files in chat or point an agent at its workspace; files persist across sessions and restarts.

Next: [Approvals, security, and privacy](/docs/approvals-security-privacy).
