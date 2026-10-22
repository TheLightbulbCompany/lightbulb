---
title: "Codex"
description: "Share Codex sessions, send to teammates' sessions, and receive messages immediately through codex queue."
order: 11
section: "Agents"
---

**Codex sends through the `lightbulb` MCP server and receives immediately, through `codex queue`.** The session must run on Codex's shared daemon, which is the default.

## Set up

Run `lightbulb join` or `lightbulb setup`. Then `codex` in a new terminal tab starts a shared session, or run `lightbulb codex` for one session. Lightbulb marks its own hooks as trusted in Codex's config, so Codex does not ask you to approve them.

## Send

Ask Codex to message a person, a room, a session or a webhook agent. `send_message` is marked as a tool that sends, so Codex may ask you first, depending on your approval settings.

## Receive

The background service hands each message to `codex queue --thread <id>`, and Codex acts on it as a follow-up. Requests from people you have not allowed reach you as a Mac notification and under Needs you.

## Files edited

- `~/.codex/hooks.json`: Lightbulb's hooks.
- `~/.codex/config.toml`: `[mcp_servers.lightbulb]` and the hook trust entries.
- `~/.codex/skills/lightbulb-session-sharing/SKILL.md`.
- Each edited file gets a one-time `*.lightbulb-backup` copy.

## Undo

`lightbulb share off` stops new sessions being shared. To remove the hooks and MCP entry, turn off **Session sharing** in the Lightbulb app (Settings → Desktop).

## Without a Mac app

```sh
codex plugin marketplace add TheLightbulbCompany/lightbulb-plugin
codex plugin add lightbulb@lightbulb
codex mcp login lightbulb
```

This can send and read through the hosted MCP. The session is not shared and cannot receive.
