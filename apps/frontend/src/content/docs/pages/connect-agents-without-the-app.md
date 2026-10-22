---
title: "Connect your agents without the app"
description: "Install the lightbulb command with one line, connect this Mac, and share Claude Code, Codex, Cursor and Grok CLI sessions with your workspace."
order: 6
---

**One command connects the coding agents on your Mac to your Lightbulb workspace.** You do not need to open the Lightbulb window. You need a Mac with Apple silicon.

## Install

```sh
curl -fsSL https://thelightbulb.company/install | sh
```

Read the script first at [thelightbulb.company/install](/install). It is short and uses no `sudo`. It:

1. Downloads the latest Lightbulb.app, signed and notarized by Apple.
2. Checks the code signature, the signing team (`WZX4U3C22Y`) and Gatekeeper. It stops if any check fails.
3. Installs the app to `~/Applications`, or upgrades your copy in `/Applications` if you already have one.
4. Links `~/.local/bin/lightbulb` to the app's binary and tells you if `~/.local/bin` is not on your `PATH`.
5. Runs `lightbulb join`.

Run it again at any time to upgrade. Add `-s -- --no-join` after `sh` to install without connecting. The `lightbulb` command lives inside the app because macOS shows Allow and Decline notifications only for a signed app. The app also updates itself.

## Join

```sh
lightbulb join
```

It prints a link and a code and opens your browser. Sign in (or sign up) and approve **Connect this Mac**. Then `join`:

- sets up every agent it finds (the same as `lightbulb setup`);
- turns on **Share every new session** (the same as `lightbulb share on`);
- starts a background service, `lightbulb serve`, with no window.

After that, `claude` and `codex` in a new terminal start shared sessions. Cursor and Grok CLI sessions connect through their hooks.

## What gets edited

Lightbulb writes only its own entries and keeps what you had.

| Where | What |
|---|---|
| `~/.claude/settings.json`, `~/.claude.json` | Claude Code hooks and the `lightbulb` MCP server |
| `~/.codex/hooks.json` and Codex's MCP list | Codex hooks and the `lightbulb` MCP server |
| `~/.cursor/hooks.json`, `~/.cursor/mcp.json` | Cursor hooks and MCP server, if Cursor is installed |
| `~/.grok/hooks/lightbulb.json`, `~/.grok/config.toml` | Grok CLI hooks, MCP server and its permission rule, if Grok CLI is installed |
| `~/.zshrc`, `~/.bashrc`, your bash login file | One marked block that defines `claude` and `codex` functions |
| `~/Library/LaunchAgents/company.thelightbulb.serve.plist` | The background service |
| `~/Library/Application Support/company.thelightbulb.desktop` | This Mac's pairing and session state |
| Keychain | This Mac's two device keys |

## Undo it

- `lightbulb share off` removes the shell block. New `claude` and `codex` sessions run as usual and are not shared.
- `lightbulb leave` stops and removes the background service and turns sharing off. The hooks stay but do nothing without the service. To remove them too, turn session sharing off in the Lightbulb app.
- The Mac stays paired after `leave`. Disconnect it from your workspace in the Lightbulb app to unpair it.
- To remove everything, delete `Lightbulb.app` and `~/.local/bin/lightbulb` after the steps above.

## What leaves this Mac

- **At join:** the public halves of this Mac's two device keys and the Mac's name. The private keys stay in your Keychain.
- **While a session is shared:** that session's terminal and messages go to your workspace, where its members can see them. Sessions you do not share are not sent.
- **Not sent:** your files, your screen and your browser. `join` pairs with Shell, Browser, Files and Screen access all off, so agents cannot run commands on this Mac. You can turn each one on later in the app.
- A message from a teammate or an agent that needs your OK arrives as a Mac notification with **Allow** and **Decline**.
