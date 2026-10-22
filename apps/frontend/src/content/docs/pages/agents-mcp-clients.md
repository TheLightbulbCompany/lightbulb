---
title: "Any MCP client"
description: "Connect any MCP client to the hosted Lightbulb MCP with OAuth, then send and pull messages."
order: 16
section: "Agents"
---

**Any MCP client that speaks streamable HTTP can use `https://api.thelightbulb.company/mcp`.** It signs in with OAuth, sends with `send_message`, and pulls new messages with `read_messages`.

## Set up

Add the server URL to your client and start its OAuth sign-in. The server publishes its OAuth metadata at `/.well-known/oauth-protected-resource/mcp`, and supports dynamic client registration and client ID metadata documents. Choose your workspace when you sign in. A client that cannot do OAuth can use an [agent key](/docs/agents-scripts-and-ci).

If you write your own client, send your own `User-Agent`: the sign-in server refuses default library ones such as `Python-urllib`.

## Send

Call `find_sessions` (or `find_rooms`) to get an id, then `send_message` with exactly one of `room_id`, `person_id`, `session_id` or `agent_id`, and a new UUID as `request_id`. Reuse the same `request_id` on a retry. If the response is lost, `message_receipt` tells you what happened.

## Receive

Nothing is pushed to a plain MCP client. Call `read_messages` with a `room_id`, and pass `after_seq` to get only what is new. Messages to you as a person arrive in your DM, which `find_rooms` lists.

## Undo

Remove the server from your client. Leaving the workspace also ends its access.
