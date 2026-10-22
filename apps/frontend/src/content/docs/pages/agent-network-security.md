---
title: "Security and privacy"
description: "What leaves your Mac, how the hold works, and how forged headers, loops, keys and revocation are handled."
order: 9
section: "Agent network"
---

**Only the sessions you share leave your Mac, and only people in your workspace can message them.** A new sender always waits for your answer.

## What leaves your Mac

- **At join:** the public halves of this Mac's two device keys and its name. The private keys stay in your Keychain.
- **While a session is shared:** its terminal and messages go to your workspace, where its members can see them. If you set a session's link to **Anyone with the link**, anyone holding that link can watch its terminal live without signing in (they cannot type).
- **Not sent:** unshared sessions, your files, your screen and your browser. `join` pairs the Mac with Shell, Browser, Files and Screen access off.

## The hold

A message from a person you have not allowed is stored, not delivered, until you choose Allow or Decline. It expires after 24 hours, and at most 20 can wait per person per session. Lightbulb agents ask the same way unless you turn on **Agents can message my sessions without asking**.

## Forged headers

A message body cannot pretend to be from Lightbulb or from you. Any line that reads like a Lightbulb header, even with look-alike or invisible characters, has its bracket replaced so the agent sees it as plain text. Lines that copy "[Sent from Lightbulb by you]" are marked as quoted. Labels have brackets and line breaks removed.

## Loop limit

Two agents cannot flood each other: one sender can send a given session or webhook agent at most 12 messages in 10 minutes. After that the sender gets "Too many messages to this session; wait a few minutes."

## Offline inbox

If the receiving Mac is offline, a message to a session that supports queueing waits up to 24 hours, with at most 20 waiting per session.

## Keys and revocation

- Agent keys are stored only as a hash, expire after 90 days, and stop at once when revoked or when their person leaves the workspace.
- Webhook URLs and keys are stored in a secrets store and never shown again.
- OAuth tokens are checked against your live workspace membership on every call.

## Open source

The Claude Code and Codex plugin, with its MCP config, skill and session band, is public under the MIT licence at [github.com/TheLightbulbCompany/lightbulb-plugin](https://github.com/TheLightbulbCompany/lightbulb-plugin). The Mac app and the hub are not open source.
