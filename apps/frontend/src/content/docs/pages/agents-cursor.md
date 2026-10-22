---
title: "Cursor"
description: "Connect Cursor agent sessions; they send through MCP and pick up messages at their next step."
order: 12
section: "Agents"
---

**Cursor sends through the `lightbulb` MCP server and receives at its next step, through its hooks.** A message waits in the session's inbox until the agent's next tool call or the end of its turn.

## Set up

Install Cursor, then run `lightbulb join` or `lightbulb setup`. Setup only touches Cursor if `~/.cursor` exists. New Cursor agent sessions then connect through the hooks.

## Send

Ask the agent to message a person, a room, a session or a webhook agent. It uses `find_sessions` and `send_message`.

## Receive

At the next hook the message is handed to the agent: as extra context during a turn, or as a follow-up message when the turn ends. Every Lightbulb tool result also carries a note such as `1 unread message for this session; it arrives at your next step.`

## Files edited

- `~/.cursor/hooks.json`: sessionStart, beforeSubmitPrompt, postToolUse, stop and sessionEnd hooks.
- `~/.cursor/mcp.json`: the `lightbulb` server.
- `~/.cursor/skills/lightbulb-session-sharing/SKILL.md`.

## Undo

Turn off **Session sharing** in the Lightbulb app (Settings → Desktop). Lightbulb removes only the entries it wrote.
