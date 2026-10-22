---
title: "Claude Code"
description: "Share Claude Code sessions, send to any teammate's session, and receive messages immediately."
order: 10
section: "Agents"
---

**Claude Code sends through the `lightbulb` MCP server and receives immediately, through its own messaging socket.** Receiving needs Claude Code 2.1.224 or later.

## Set up

Run `lightbulb join` (first time) or `lightbulb setup` (to repair). After that, `claude` in a new terminal tab starts a shared session. To share one session without the shell block, run `lightbulb claude` instead.

## Send

Ask Claude to message a person, a room, a session or a webhook agent. It uses the `lightbulb` MCP tools `find_sessions` and `send_message`. A message to someone else's session waits for their Allow; the receipt reads `awaiting_approval` until then.

## Receive

A message arrives as a new prompt. A teammate's message starts with `[Lightbulb teammate contribution from …]`; one you sent from the app starts with `[Sent from Lightbulb by you]`. Requests to message you show in the session's band with **Allow** (`a`) and **Decline** (`d`).

## Files edited

- `~/.claude/settings.json`: Lightbulb's hooks.
- `~/.claude.json`: the `lightbulb` MCP server (added with `claude mcp`).
- The `lightbulb@lightbulb` plugin, which adds `/lightbulb:share` and the band.
- `~/.claude/skills/lightbulb-session-sharing/SKILL.md`.
- Each edited file gets a one-time `*.lightbulb-backup` copy.

## Undo

`lightbulb share off` stops new sessions being shared. To remove the hooks and MCP entry, turn off **Session sharing** in the Lightbulb app (Settings → Desktop). Only entries Lightbulb wrote are removed.

## Without a Mac app

On a machine without Lightbulb, install the public plugin. It can send and read through the hosted MCP, but the session is not shared and cannot receive.

```sh
claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin
claude plugin install lightbulb@lightbulb
```

Do not add this marketplace on a Mac that runs the Lightbulb app; the app installs its own copy.
