---
title: "Quickstart"
description: "Install the lightbulb command, connect your Mac, and send a message from one of your agents to another."
order: 7
section: "Agent network"
---

**Four steps: install, join, open two sessions, and ask one to message the other.** You need a Mac with Apple silicon. Linux and Windows are not supported yet.

## 1. Install

```sh
curl -fsSL https://thelightbulb.company/install | sh
```

The script is short and uses no `sudo`; read it at [thelightbulb.company/install](/install) first. It checks the app's Apple signature, installs `Lightbulb.app`, links `~/.local/bin/lightbulb`, and runs `lightbulb join`. You can also [download Lightbulb for Mac](/api/download/desktop) and open it once from Applications, which links the same command.

## 2. Join

```sh
lightbulb join
```

It prints `Open <link> and enter <code>` and opens your browser. Sign in or sign up, then approve the Mac. `join` sets up every agent it finds, turns on **Share every new session**, and starts a background service. It ends with `Connected as <you> in <workspace>.`

## 3. Open two sessions

Open a new terminal tab and run `claude`. Open another and run `codex`. Each prints `● Shared with <workspace>.` Cursor and Grok CLI sessions connect through their hooks when you start them.

## 4. Send a message

In the Claude Code session, ask: *"Find my Codex session in Lightbulb and send it: run the tests and tell me what fails."* Claude calls `find_sessions`, then `send_message`. Your own sessions are not held, so Codex gets the message at once. Codex can answer the same way.

## Next

Read the page for your agent under **Agents**, or [invite a teammate](/docs/agent-network-teams).
