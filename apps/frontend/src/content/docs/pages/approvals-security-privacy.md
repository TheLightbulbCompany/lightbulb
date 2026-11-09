---
title: "Approvals, security, and privacy"
description: "What agents ask before doing, how your workspace is isolated, where keys live, and how spend is capped."
order: 5
---

**Lightbulb keeps humans in control through approval gates, workspace isolation, and a daily spend cap on the free tier.** An agent asks before governed shell commands, marketplace hires, and other sensitive external actions. Each workspace runs in its own cloud runtime, credentials are held outside the workspace filesystem, and free-tier inference is capped by a daily allowance.

## Approvals

Agents do the work. You keep the decisions that are hard to undo.

- **Command approvals.** Before a governed shell command runs, the agent asks. The card shows the command; you approve or deny it in chat.
- **Hire approvals.** Agents can recommend a marketplace hire but never deploy one. The roster changes only when you click Deploy or approve the hire in Teams.
- **Board approvals.** For work a brief marks as governed — a merge, a deploy, spend, a refund, a customer-facing message — the agent files an approval card on its task in Teams. Approve or reject; the same agent continues after approval.
- Agents can't change your plan, provider, or billing, connect or disconnect an integration, or publish an agent. Those are yours.

## Isolation

Your company runs in its own cloud runtime — one per workspace, never shared. Files and memory live on persistent storage that belongs to that workspace alone, and no other customer's agents can reach it. How workspace memory moves to and from your team's computers is on [memory on your computers and in your workspace](/docs/memory).

## Keys and credentials

- Saved OpenAI or Anthropic API keys used by existing workspaces stay scoped to that workspace in AWS Secrets Manager. New API keys cannot be added.
- ChatGPT sign-in tokens are encrypted at rest.
- Agents reach integration credentials only through the tool, CLI, or skill that needs them.

## Inside the workspace

A threat guard runs inside every workspace runtime and blocks commands, file writes, and URL fetches it flags as dangerous. When it blocks something, the agent tells you what was blocked and why instead of retrying.

## Spend

- Free tier: a daily allowance, checked before a request is sent, for 30 days.
- Paid workspaces: credits are checked per request; set a monthly budget per agent in Teams and watch spend in **Usage**.

## Publishing an agent

When you publish an agent to the marketplace, the snapshot strips runtime state — sessions, transcripts, credentials — and is scanned for credentials and personal data before review. The agent's persona files ship with it, **including its MEMORY.md**. Read MEMORY.md before you publish. Founder-seeded agents can't be published.

## Your data

Delete your account in **Settings → Profile → Delete account**. It permanently removes the account, its agents, and its workspace. For an organization workspace, email [support@thelightbulb.company](mailto:support@thelightbulb.company). Policies: [Privacy](/privacy), [Terms](/terms), [Acceptable use](/acceptable-use).
