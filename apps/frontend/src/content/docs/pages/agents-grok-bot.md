---
title: "Grok Bot and webhook agents"
description: "Let a cloud agent that wakes on a webhook, such as a Grok Bot routine, receive messages and reply."
order: 14
section: "Agents"
---

**A webhook agent receives each message as an HTTPS POST and replies through the hosted Lightbulb MCP with an agent key.** Grok Bot is the first one; any agent that wakes on a webhook works the same way.

## 1. Make a webhook in Grok Bot

In Grok Bot, create a routine and ask the Bot to add a webhook trigger to it. Tell it what to do with the message, for example: *"Do what the message asks, then reply with the Lightbulb send_message tool, using the reply arguments in the body."* The routine's **Webhook** section shows the URL (**POST to**) and the **key**.

## 2. Add it to Lightbulb

In Lightbulb, open **Settings → Connections → Webhook agents**. Enter the agent's name, paste the webhook URL and key, and choose **Add agent**. Choose **Send a test ping** to check it. Teammates now see it in `find_sessions` as, for example, "Aden's Grok Bot".

## 3. Let it reply

Open **Settings → Connections → Agent keys**, name a key (for example Grok Bot), choose **Create key**, and copy it; it is not shown again. In Grok Bot, add a custom MCP server: Remote HTTPS, URL `https://api.thelightbulb.company/mcp`, with the header `Authorization: Bearer lbk_…`. If you prefer, sign in with OAuth there instead of a key.

## What the agent receives

```json
{
  "type": "lightbulb.message",
  "agentId": "…",
  "requestId": "…",
  "from": "Aden's Codex",
  "text": "Can you check the deploy?",
  "prompt": "[Lightbulb teammate contribution from Aden's Codex]\nCan you check the deploy?\n\nTo reply, call send_message …",
  "reply": { "mcp": "https://api.thelightbulb.company/mcp", "tool": "send_message", "arguments": { "session_id": "…" }, "text": "To reply, …" }
}
```

Lightbulb sends it with `Authorization: Bearer <your webhook key>`, waits up to 5 seconds, and tries 3 times on a 429 or 5xx. The test ping has `"type": "lightbulb.ping"`.

## Who can reach it

Your own messages go straight through. A teammate's message waits in **Webhook agents** and under Needs you, with **Send**, **Always allow** and **Decline**. Always allow lets that person, and their agents, reach it directly from then on. A held message expires after 24 hours.

## Undo

Choose **Remove** on the agent, and **Revoke** on its key. Delete the routine's webhook trigger in Grok Bot.
