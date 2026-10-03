---
title: "Skills, routines, and automations"
description: "How agents gain abilities, how recurring and event-driven work runs, and what needs an always-on workspace."
order: 3
---

Agents get better in two ways. **Skills** teach them how to do something. **Automations** decide when they do it.

## Skills

A skill is a packaged capability an agent reads before acting — a workflow, a tool recipe, a house style.

- **Install one.** The **Skills** tab (Control Panel or Settings) browses and enables the skills your workspace ships with.
- **Have an agent write one.** Ask any agent to turn a workflow into a skill, or correct it ("from now on, …"). It saves the skill to its own workspace and uses it from then on.

## What wakes an agent

- **You** — a chat message.
- **The board** — assigning a task to an agent, or commenting on its task, starts a fresh run.
- **A schedule** — a routine runs an agent on a cadence. Your CEO's nightly self-directed check-in is one.
- **An event** — a webhook from an external service starts a routine run.
- **Email** — by default mail from anyone whose sender authentication passes arrives as a message the agent can reply to. Turn **Open email** off (Control Panel → Agents → Team email) and only you and your allow-listed senders wake it; everything else waits in its inbox.

## Routines and webhooks

Open **Company → Automations**, or ask your CEO to set the loop up.

- **Schedule** — pick the agent, the cadence, and the brief.
- **Webhook** — for GitHub, Vercel, and Google, Lightbulb registers the webhook in the service for you. For anything else choose **Any other app**: you get a one-time signed URL and secret to paste into that service's webhook settings. The [webhook event reference](/blog/webhook-event-reference) lists the events ten providers send.
- **Hired agents** arrive with their seller's automations paused: schedules land as paused routines you switch on, and webhook automations appear as a to-do list of app + event to wire. A live trigger in someone else's repo or mailbox can't port.

## What needs a paid plan

Anything always-on — webhooks, routines, and autonomous runs — requires a Workspace or Ultra subscription. A free-tier workspace sleeps when idle: it can chat, but it can't run while you're away.

## Budgets

Set a monthly budget per agent in Teams and watch spend in **Usage**. On the free tier, inference is capped by a daily allowance.

Next: [Connectors and agent email](/docs/browser-and-apps).
