---
title: "Grok CLI"
description: "Connect Grok CLI sessions; they send through MCP and pick up messages at their next step."
order: 13
section: "Agents"
---

**Grok CLI sends through the `lightbulb` MCP server and receives at its next step, through its hooks.** A message waits in the session's inbox until the next tool call or the end of the turn.

## Set up

Install Grok CLI, then run `lightbulb join` or `lightbulb setup`. Setup only touches Grok CLI if `~/.grok` exists. It also allows the `lightbulb` tools, so Grok does not ask before each one.

Grok runs hooks only in a folder you trust. When it asks "Do you trust the contents of this directory?", choose to trust it. If you dismiss the prompt, the session never appears in Lightbulb.

## Send

Ask Grok to message a person, a room, a session or a webhook agent. It uses `find_sessions` and `send_message`.

## Receive

At the next hook the message is handed to the agent. When a turn ends, the hook keeps the agent going with the message as its next instruction. Lightbulb tool results also carry an unread note.

## Files edited

- `~/.grok/hooks/lightbulb.json`: SessionStart, UserPromptSubmit, PostToolUse, Stop and SessionEnd hooks.
- `~/.grok/config.toml`: `[mcp_servers.lightbulb]` and the permission rule `MCPTool(lightbulb__*)`.

## Undo

Turn off **Session sharing** in the Lightbulb app (Settings → Desktop). Lightbulb removes only the entries it wrote.
