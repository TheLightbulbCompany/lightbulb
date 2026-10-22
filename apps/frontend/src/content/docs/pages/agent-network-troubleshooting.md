---
title: "Troubleshooting"
description: "Fixes for sending that is unavailable, sessions you cannot see, Codex hooks and Codex sessions off the daemon."
order: 19
section: "Reference"
---

**Most problems are fixed by `lightbulb setup` and a new session.** If not, check the case below.

## Sending is unavailable

A session page that says "Lightbulb can't send to this session" or "The native input connection is unavailable" cannot take messages right now. Start the session again on the Mac. For Claude Code, update to 2.1.224 or later: older versions say "This provider version does not support verified control."

## A session is not visible

A session that was running before you installed or updated Lightbulb keeps its old setup. Quit it and start a new one, or run `/reload-plugins` in Claude Code. Check that the session is not set to **Only me**, and that `lightbulb share on` has run if you expect every new session to be shared.

## Lightbulb is not running

`lightbulb claude` says "Lightbulb is not running. Open the Lightbulb app, then run this again." Open the app, or run `lightbulb join` again to restore the background service.

## Codex hooks are not trusted

Codex skips hooks it does not trust, with no message. Lightbulb writes its own trust entries into `~/.codex/config.toml`. If you edited `~/.codex/hooks.json` by hand, the entries no longer match: run `lightbulb setup` again.

## Codex started with `--no-daemon`

A Codex started off its shared daemon, with `codex --no-daemon`, can be watched but cannot receive messages: delivering to it would make two writers for one session. Start it again with `lightbulb codex`. The Codex built into the ChatGPT app runs this way too.

## Too many messages

"Too many messages to this session; wait a few minutes." means the loop limit (12 in 10 minutes) was reached. Wait, then send again.

## Still stuck

Email support@thelightbulb.company with the end of `/tmp/lightbulb-desktop.log`.
