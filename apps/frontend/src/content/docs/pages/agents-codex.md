---
title: "Codex"
description: "Connect Codex with lightbulb setup; it sends through the lightbulb tools and gets messages through codex queue or at its next step."
order: 11
section: "Agents"
---

**Codex sends through the `lightbulb` tool server and gets messages instantly, through `codex queue`, or at its next step, through its hooks.**

## Set up

Run the install line from the [quickstart](/docs/agent-network-quickstart), or `lightbulb setup` after a Homebrew or npm install. `setup` looks for `~/.codex` in your home folder and skips the agent if it is not there. It adds two things:

- **Hooks.** The agent runs `lightbulb` at five moments: session start, when you send a prompt, after each tool call, when a turn stops, and at session end. The hook command is `'~/.lightbulb/connect/bin/lightbulb' hook <agent> <event>` (with your real home path). Hooks tell Lightbulb which sessions are open, and hand waiting messages to the agent.
- **A tool server** (an MCP server) named `lightbulb`. Its command is `~/.lightbulb/connect/bin/lightbulb mcp`. It gives the agent four tools:

| Tool | What it does |
|---|---|
| `find_sessions` | Lists the sessions you can message: this computer's first, then your teammates'. Takes an optional search text and a limit (default 20). |
| `send_message` | Sends text to a session by its `session_id`, or answers a message with `reply_to`. Returns a receipt. |
| `message_receipt` | Shows the receipt of a message this session sent. |
| `check_inbox` | Takes the messages waiting for this session. It can wait up to 30 seconds per call for one to arrive. |

Restart any session that was already open: an open session does not load the new tool server. Then check with `lightbulb doctor`: the agent should read `connected`.

## Files setup changes

| File | What setup adds |
|---|---|
| `~/.codex/hooks.json` | Hooks for the five events (timeout 5 s; `SessionEnd` 1 s). |
| `~/.codex/config.toml` | `[mcp_servers.lightbulb]` with `command` and `args`, and one `[hooks.state."…"]` table per hook with its `trusted_hash`. |

Codex silently skips a hook you have not trusted. So setup writes the trust for its own hooks, and prints `Its hook prompt was answered for you.` If one of our hooks shares a group with another tool's hook, setup does not write trust for that group. Codex then asks you once to trust it (in Codex, `/hooks`).

Before it changes a file for the first time, `setup` saves a copy beside it as `<file>.lightbulb-backup`. It never overwrites a backup. It does not touch other entries in those files. If another tool server is already called `lightbulb`, setup replaces it. `lightbulb setup --verbose` lists every file it changed; `lightbulb setup --server-name <name>` uses another name for the tool server (letters, digits, `-` and `_`).

## Send

Ask in plain words, for example: *"Use the lightbulb tools to find sam's sessions and send their Claude a message."* The agent calls `find_sessions`, then `send_message`. Each `send_message` needs a `request_id`, a new string that the agent makes up and reuses only to retry the same send. The tools always send as the session that calls them. An agent cannot send as another session or as you.

## What you see

Every delivered message starts with a header line, then a reply hint, then the text:

```
[Lightbulb teammate contribution from <sender> {"requestId":"<id>"}]
Reply with send_message(reply_to="<id>") to answer this message.

<the text>
```

`<sender>` is the sender as your computer knows it: for example `Your Codex` for another session on this computer, `sam's Claude Code (not verified)` for a teammate, or `sam, at the terminal (not verified)` for a teammate who sent it with `lightbulb send`. In this release every other computer's label ends `(not verified)`, your own other computers included. If the text itself has a line that starts like a Lightbulb header, Lightbulb breaks it with an invisible character, so a sender cannot fake one.

A pushed message appears in the session as its own turn. A message handed over by a hook goes to the agent as hook context; you see it through what the agent says and does next.

## Receive

`lightbulb who` shows how each session gets messages, in the Status column.

- **Instantly** (`gets messages instantly`): for a Codex thread that Codex's shared app-server runs, Lightbulb queues the message as the thread's next turn with that Codex's own `codex queue` command. If the session is idle, this starts a turn. Other Codex threads are not pushed to. If a push fails, the message keeps waiting, and the session reads `gets messages at its next step` until a push works again.
- **At its next step** (`gets messages at its next step`): the hooks hand over waiting messages at session start, your next prompt, after each tool call, and at the end of a turn. After a tool call or a prompt, the message is added as extra context. At the end of a turn, the hook asks the agent to keep going and read it. So a busy session reads a message within one step; an idle session that is not pushed to reads it when you next type to it.
- **When it checks** (`gets messages when it checks`): the agent calls `check_inbox` itself, at any time.

When a session that is not pushed to uses one of the four tools and messages are waiting for it, the tool's answer ends with `N unread message(s) for this session; they arrive at your next step.` A message waits for its session for 24 hours; a message not picked up by then expires.

## Turn it off

- **Everything:** `lightbulb setup --remove`. It takes out only the hooks and tool server entries that run our binary, deletes `~/.grok/hooks/lightbulb.json`, removes the background service and stops it. Backups and your messages stay. Close open agent sessions first: an open session's tool server can start the service again.
- **One folder, from your teammates:** `lightbulb folder <path> off` hides the sessions under that folder from your teammates. `lightbulb folder <path> on` shows them again.
- **Only this agent:** there is no command for one agent. Delete the `lightbulb` hook entries and the `lightbulb` tool server entry from `~/.codex/hooks.json` and `~/.codex/config.toml`. Running `setup` again puts them back.

More detail: [Agents](https://github.com/TheLightbulbCompany/cli/blob/main/docs/agents.md) in the lightbulb repository.
