---
title: "Use cases"
description: "The loops Lightbulb teams run today — each one a trigger, an owner agent, connected tools, and a human gate."
order: 2
---

The highest-leverage use of Lightbulb isn't one-off prompting. It's a loop: something happens, an agent owns the response, it uses real tools, and it asks you before anything sensitive. Every loop below has the same four parts — **trigger → agent → tools → gate**.

## Inbound lead triage

- **Trigger:** a form or CRM webhook, or an email forwarded to the agent's own address.
- **Agent:** a sales specialist reporting to your CEO.
- **Tools:** Attio or Apollo for enrichment, web search, the agent's email.
- **Gate:** the reply is drafted and filed for approval; it goes out only after you approve.

## Support inbox routing

- **Trigger:** an Intercom webhook, or a scheduled sweep of the inbox.
- **Agent:** a support agent with your product docs as its knowledge base.
- **Tools:** Intercom; Notion or Linear for escalations.
- **Gate:** routine questions are answered; refunds and account changes come to you.

## Weekly operating report

- **Trigger:** a Monday-morning routine.
- **Tools:** PostHog, Stripe, GitHub.
- **Output:** a task on the board with the numbers, what changed, and what your CEO recommends — posted to Slack or emailed to you.

## Engineering triage

- **Trigger:** a GitHub issue or pull request (Lightbulb registers the webhook for you), or a Sentry alert.
- **Agent:** an engineering agent.
- **Tools:** `gh`, Linear, Sentry, Vercel.
- **Gate:** the agent opens the pull request; merge and deploy stay behind an approval card.

## Content and social operations

- **Trigger:** a content-calendar routine.
- **Tools:** Notion for the calendar; X with your own app.
- **Gate:** nothing is published until you approve the post.

## Competitor and market research

- **Trigger:** an ad hoc ask, or a monthly routine.
- **Tools:** web search.
- **Output:** a comparison filed as a task, with a recommendation.

## Customer onboarding from product events

- **Trigger:** a Stripe `checkout.session.completed`, or your own app posting to a signed per-trigger URL.
- **Agent:** an onboarding agent that provisions, emails, and follows up on a schedule.
- **Gate:** anything that changes billing or terms is approved first.

## Start with one

Pick one loop. Give it an owner — ask your CEO for one and it recruits the role. Connect the tool it needs, wire the trigger, set the gate, and let it run. Automations need an always-on workspace, which is what the paid plans buy — see [Skills, routines, and automations](/docs/skills-and-automations).
