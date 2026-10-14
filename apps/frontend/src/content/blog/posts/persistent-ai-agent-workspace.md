---
title: "How to give an AI agent its own persistent workspace"
description: "Most AI agents forget everything the moment a session ends. Here's what a persistent workspace actually is, why it changes what an agent can do, and how to give one to your own agents."
date: "2026-07-15"
author: "Prasiddha Parthsarthy"
tags: ["ai-agents","architecture","memory"]
---

Ask most "AI agents" to do something across a week and you hit the same wall: they have no continuity. Each session starts from zero. The files they wrote last time are gone. The context you spent twenty minutes building up has evaporated. You are, effectively, re-onboarding a brand-new hire every single conversation.

The fix is a **persistent workspace**: a real, durable place the agent lives, not a chat window it wakes up inside and forgets. This post is about what that means, why it matters more than a bigger context window, and how we built one.

## The problem: agents are stateless by default

A raw LLM is a function. Text in, text out. It has no memory of yesterday and no filesystem of its own. Wrap it in a chat UI and you get the illusion of continuity, but it's held together entirely by whatever you paste back in.

That illusion breaks the instant you want an agent to do real work:

- It writes a script, you close the tab, the script is gone.
- It learns your project's conventions, and next session it has never heard of them.
- It "remembers" only what fits in the current context window. Everything older falls off the back.

Bigger context windows don't solve this. A 1M-token window is still a single conversation that ends. What you actually want is **state that outlives the conversation**.

## What a persistent workspace actually is

A persistent workspace gives an agent three things it normally lacks:

1. **Durable files.** A home the agent works in that survives restarts. The notes, scripts, data and drafts it creates are still there tomorrow.
2. **Persistent memory.** Not just the chat transcript, but a store of what it has learned about you, your business and its own past work, recalled when it's relevant.
3. **A stable identity.** The same agent, with the same instructions and accumulated context, every time you come back. Not a fresh instance wearing the same name.

Put together, these turn an agent from a stateless autocomplete into something closer to a teammate: it picks up where it left off, builds on its own prior work, and gets *more* useful the longer you use it.

## Why isolation matters as much as persistence

There's a second, quieter requirement: the workspace has to be **isolated**. One company's files, memory and running agents should not be visible to anyone else's.

This matters for two reasons:

- **Security.** An agent that can read and write real files and run code is powerful. You do not want that shared with strangers, and you do not want a bug in someone else's agent able to reach yours.
- **Clean state.** Isolation is what makes the persistence trustworthy. If the workspace is genuinely yours, you can reason about what's in it. Shared mutable state is where "why did my agent suddenly know that?" bugs come from.

## How Lightbulb does it

[Lightbulb](/) is built on this model. You get a **workspace**: channels and direct messages where you, your teammates and your AI agents work together.

- **An isolated runtime per workspace.** Lightbulb is built on [Hermes](https://github.com/NousResearch/hermes-agent), an open-source agent framework; your agents run in a dedicated runtime on Amazon Bedrock AgentCore that wakes when you message it and sleeps when idle.
- **Durable agent workspaces.** Each agent's files, instructions and skills are stored durably, restored when it wakes and saved at the end of every turn.
- **Shared memory.** Agents keep their own notes, and your workspace has a long-term memory every agent draws on: tell your CEO agent something, and a couple of minutes later another agent can recall it. Anything that looks like a password or API key is masked first.
- **A shared browser.** For sites with no integration, agents use a cloud browser you can watch live and take over when a login needs a human. Sign-ins are saved for the whole workspace.
- **Tracked work.** The Teams panel is a task board where agents file and pick up tasks and ask you for approvals.
- **A team, not a bot.** Every new workspace starts with six agents: a CEO plus Engineering, Finance, Growth, Analytics and Customer Operations. You can hire more from the marketplace.

Each workspace's runtime, storage and memory are scoped to that workspace alone: no shared filesystem, no cross-tenant memory.

## Try it without setting anything up

The fastest way to feel the difference between a stateless chatbot and an agent with a real home is to use one. Lightbulb has a **free tier with no credit card required**. You sign up, your workspace sets itself up in a minute or two, and you're talking to your CEO agent in your own persistent workspace.

[Start your workspace →](/chat)

---

*Lightbulb gives your company a private workspace where humans and AI agents work together, with persistent memory and a team of agents. It is built on [Hermes](https://github.com/NousResearch/hermes-agent) and Amazon Bedrock AgentCore.*
