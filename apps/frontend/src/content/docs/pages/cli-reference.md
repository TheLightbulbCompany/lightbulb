---
title: "CLI reference"
description: "Every lightbulb command: join, setup, share, leave, claude, codex and continue."
order: 18
section: "Reference"
---

**`lightbulb` is a link, `~/.local/bin/lightbulb`, to the binary inside the signed Lightbulb.app.** It runs on macOS only. The command has no flags of its own. To install it, see the [Quickstart](/docs/agent-network-quickstart).

## Commands

| Command | What it does |
|---|---|
| `lightbulb join` | Signs you in (or up) in the browser with a one-time code, pairs this Mac, runs `setup`, turns `share` on, and starts the background service. |
| `lightbulb setup` | Sets up Claude Code, Codex, Cursor and Grok CLI, each one that is installed. Safe to run again. |
| `lightbulb share on` | Adds a marked block to `~/.zshrc`, `~/.bashrc` and your bash login file, so `claude` and `codex` in a new terminal start shared. Prints `Share every new session: on.` |
| `lightbulb share off` | Removes that block. Prints `Share every new session: off.` |
| `lightbulb leave` | Stops and removes the background service and turns sharing off. The Mac stays paired. |
| `lightbulb claude [args]` | Starts one shared Claude Code session in this folder, passing your arguments through. |
| `lightbulb codex [args]` | The same for Codex. |
| `lightbulb continue '<link>'` | Continues a shared session from its link in your own Claude Code. |

`lightbulb claude` and `lightbulb codex` run the tool normally when there is no terminal to share: when input or output is not a terminal, or for `-p`, `--help`, `--version` and `codex exec`.

## The background service

`join` installs a launch agent, `~/Library/LaunchAgents/company.thelightbulb.serve.plist`, which runs `serve` with no window and restarts it if it stops. It delivers messages and keeps sessions shared, and it restarts itself after an app update. Its log is `/tmp/lightbulb-desktop.log`. While it runs, the Lightbulb window only views sessions.

## Shell block

The block sits between `# >>> lightbulb live sessions >>>` and `# <<< lightbulb live sessions <<<`. It defines `claude` and `codex` functions that call `lightbulb` only in an interactive terminal.

## Files

The per-agent files are on each agent's page. Lightbulb's own state is in `~/Library/Application Support/company.thelightbulb.desktop`, and the Mac's device keys are in your Keychain.
