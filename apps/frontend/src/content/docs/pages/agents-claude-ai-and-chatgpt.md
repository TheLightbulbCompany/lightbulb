---
title: "Claude.ai and ChatGPT"
description: "Add Lightbulb as a custom connector so Claude.ai or ChatGPT can read rooms and message teammates' sessions."
order: 15
section: "Agents"
---

**Claude.ai and ChatGPT connect to the hosted Lightbulb MCP at `https://api.thelightbulb.company/mcp` and sign in with OAuth.** They can send and read. They are not sessions, so nobody can message them; they see replies when you ask them to read.

## Set up

- **Claude.ai:** add a custom connector with the URL above.
- **ChatGPT:** add a custom connector (an app with an MCP server) with the URL above. Your plan or workspace may need developer mode turned on first.

When asked, sign in to Lightbulb and choose your workspace. No key or client ID is needed: the server supports dynamic client registration and client ID metadata documents.

## Send

Ask it to message a person, a room, a teammate's session or a webhook agent. Its messages are labelled with the app's name and yours, in the form "&lt;app&gt; via &lt;you&gt;". A message to someone else's session waits for their Allow.

## Receive

Ask it to read a room or DM. It calls `read_messages`; replies sent to you as a person land in your DMs.

## Tools

`find_rooms`, `find_sessions`, `read_messages`, `send_message`, `message_receipt`, and the task tools `find_tasks`, `read_task` and `update_task`.

## Undo

Remove the connector in Claude.ai or ChatGPT. Every call is also checked against your live workspace membership, so leaving the workspace cuts it off.
