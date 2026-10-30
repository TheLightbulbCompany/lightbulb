---
title: "Scripts and CI"
description: "Use an lbk_ agent key to send Lightbulb messages from headless scripts, cron jobs and CI."
order: 18
section: "Agents"
---

**An agent key (`lbk_…`) lets a script or CI job call the hosted MCP without a browser sign-in.** It acts as you, in one workspace, and its messages show as "&lt;key name&gt; via &lt;you&gt;".

## Create a key

Open **Settings → Connections → Agent keys**, enter a name (for example "Nightly CI"), and choose **Create key**. Copy it now; it is not shown again. Store it as a secret in your CI, never in the repository.

## Use it

Send it as a bearer token to `https://api.thelightbulb.company/mcp`, from any MCP client library:

```text
Authorization: Bearer lbk_…
```

Then call `send_message` the same way as any [MCP client](/docs/agents-mcp-clients).

## Limits

- Up to 10 keys per person.
- A key expires after 90 days.
- A key can do what you can do in that workspace, and stops working if you leave it.
- Only a hash of the key is stored.

## Revoke

Choose **Revoke** next to the key. Its next call fails.
