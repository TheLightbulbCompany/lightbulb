# App integration research: ChatGPT, Grok, Cursor IDE (2026-10-23)

Scope: can Lightbulb Connect treat these apps like the four terminal agents (register sessions via hooks, push a teammate's message in as the next prompt, wire automatically in `setup`)? Sources were official docs where reachable. Several official pages returned 403/404 to my fetcher, so those points rest on search summaries and vendor write-ups and are marked **[unverified]**. Clean-room: no code or text copied.

## Summary

| App | Register sessions? | Push delivery? | Auto-wire in `setup`? | Effort / risk |
|---|---|---|---|---|
| Cursor IDE (agent pane) | Yes, strong. Same `~/.cursor/hooks.json` events (sessionStart, beforeSubmitPrompt, stop, sessionEnd) carry `conversation_id`, `workspace_roots`, `transcript_path`. | Partial, in-turn only. `stop` returns `followup_message`, which is auto-submitted as the next user message (default `loop_limit` 5). `sessionStart`/`postToolUse` can inject `additional_context`. Nothing wakes an idle chat. | Yes, fully local: write `~/.cursor/hooks.json` and `~/.cursor/mcp.json`. | Low. We likely already do half of it for the Cursor agent; confirm the IDE fires the same hooks. |
| ChatGPT desktop (macOS) | No hook or config path. Only OS-level: Accessibility API (the app itself uses it for "Work with Apps"). Local chat store existed in 2024 (plain text, then encrypted after a fix); not a viable reader. | No. Pull only, via a tool call the model chooses to make. | Not locally. Needs a hosted HTTPS MCP server, auth, and a manual UI step in the user's ChatGPT settings. | High effort, low fidelity. Recommend "pull only", or skip. |
| ChatGPT web (chatgpt.com) | Only through our own connector's tool calls (the model can call `register_session`). No passive registration. | No. Pull only. | Same as desktop: manual connector creation, no API to create one [unverified]. | Same. |
| Grok web / app | Same as ChatGPT: tool-call registration only. | No. Pull only. | Manual at grok.com/connectors, needs public URL; unclear for the macOS app. | High effort, low fidelity. |
| "Grok Bot" | Not a product I could find. No official xAI "Grok Bot" turned up. Closest are Telegram distribution of Grok and third-party Slack/Discord bridges. Treat as unresolved; ask the owner what it refers to. | n/a | n/a | n/a |

Bottom line: only Cursor can reach the "automatic, same as terminal agents" bar. ChatGPT and Grok can at best be pull-only tool clients behind a hosted, authenticated endpoint the user must add by hand.

## 1. Cursor IDE

Source: https://cursor.com/docs/agent/hooks (fetched 2026-10-23).

- Config locations, highest priority first: enterprise (`/Library/Application Support/Cursor/hooks.json` on macOS), team (dashboard), project `<root>/.cursor/hooks.json`, user `~/.cursor/hooks.json`. Project hooks run only in trusted workspaces.
- Every hook receives `conversation_id`, `generation_id`, `model`, `workspace_roots`, `transcript_path`, `user_email`, `cursor_version`. That is enough to register a session: folder = `workspace_roots[0]`, repo from git, id = `conversation_id`. `transcript_path` gives a transcript we can read.
- Busy/idle: `beforeSubmitPrompt` (receives `prompt`, can block with `continue:false`) marks busy; `stop` (receives `status`, `loop_count`) marks idle; `sessionStart`/`sessionEnd` bracket the session (`sessionStart` also gives `composer_mode`, `is_background_agent`). `workspaceOpen` fires with no conversation, usable as a presence ping.
- Delivery:
  - `stop` can return `followup_message`, auto-submitted as the next user message. This is the closest analogue to our Stop-hook delivery for terminal agents. Limit: `loop_limit` default 5, so it cannot chain forever.
  - `sessionStart`, `postToolUse`, `postToolUseFailure` can return `additional_context`, so a pending message can be injected at the next tool boundary or at session start.
  - `beforeSubmitPrompt` cannot modify the prompt (only block). `preToolUse` has `updated_input` but is not a message channel.
  - An idle chat the user is not touching cannot be woken by any hook. Delivery waits for the next turn boundary, the same constraint as the terminal agents.
- Known gaps: hooks have been reported flaky in some Cursor versions on Windows (forum threads, 2025-2026) [not verified on macOS]; `ask` is not enforced for `preToolUse`; cloud agents skip user-level hooks and MCP hooks, so background/cloud agents are not reachable.
- MCP: Cursor IDE reads `~/.cursor/mcp.json` (stdio supported), so our existing local stdio tool server applies unchanged. I did not re-verify the current mcp.json schema in this pass.
- Auto-wire: `setup` merges entries into `~/.cursor/hooks.json` and `~/.cursor/mcp.json`; no hosted endpoint, no OAuth. The user sees: a one-time trust of the hooks, tools appearing in the agent pane, and a follow-up message arriving as if typed.
- Open check: confirm the IDE and CLI share `hooks.json` semantics and that `stop` fires per agent turn in the IDE composer, with a real run. `stop` follow-ups in the IDE vs CLI is the key thing to test.

## 2. ChatGPT (desktop and web)

### Registering conversations
- No hooks, no config-file integration points. The official local integration is "Work with Apps" (https://help.openai.com/en/articles/10119604-work-with-apps-on-macos; page returned 403 to me, details via search summary): ChatGPT reads content from supported apps (Xcode, VS Code and forks via an extension, JetBrains IDEs, TextEdit, Terminal/iTerm/Warp) using the macOS Accessibility API. That is ChatGPT reading other apps, not other tools reading ChatGPT. It requires ChatGPT for macOS 1.2025.057 or later.
- We could do the reverse with our own Accessibility code (read the window title, detect typing/streaming), but that is brittle, needs the user's Accessibility grant, breaks on UI changes, and gives no stable conversation id. Not recommended as the primary path.
- Local chat store: in June 2024 the Mac app kept chats in plain text under `~/Library/Application Support/com.openai.chat/`; OpenAI patched it to encrypt (computing.co.uk, heise.de, 2024-07). The current format is not documented; do not depend on it. Privacy risk is also high.
- A community pattern exists in the other direction (MCP servers that drive the ChatGPT Mac app via the "Ask ChatGPT on Mac" shortcut and Accessibility): glama.ai/mcp/servers/xncbf/chatgpt-mcp. This lets an MCP client type into ChatGPT, which is a push-by-UI-automation route. Fragile, English-only per its notes, and it needs foreground focus.

### Remote MCP connectors (the only sanctioned extension point)
- Sources: https://developers.openai.com/apps-sdk/deploy/connect-chatgpt (fetched), https://developers.openai.com/api/docs/guides/developer-mode (404 to my fetcher; content from search summary), vendor guides (Zuplo, Boomi, Terminal49, FastMCP).
- Requirements as documented or reported:
  - Public HTTPS endpoint, or OpenAI's "Secure MCP Tunnel" (https://developers.openai.com/api/docs/guides/secure-mcp-tunnels, not read). Localhost / VPN / private network is not reachable; a tunnel (ngrok, Cloudflare) is the dev workaround.
  - Transport: streamable HTTP, typically at `/mcp`. Older SSE is reported as supported by some vendors but OpenAI's current page names streamable HTTP; choose that.
  - Auth: configured when adding the server; "authentication discovery must work" for tools that need an account. Vendors report options of OAuth, no auth, or mixed (unauthenticated initialize/list-tools, authenticated tool calls). OAuth details (DCR, PKCE, protected-resource and authorization-server metadata) are not on the page I read; a community thread reports "Failed to resolve OAuth client for MCP connector" when authorization-server metadata is incomplete **[unverified]**. Expect to need standard MCP OAuth 2.1 discovery plus dynamic client registration.
  - Adding it is a UI step: Plugins page, "Add custom MCP server", name, URL or tunnel, auth, accept risk warning, review discovered tools. Older docs call this "Developer mode" under Settings > Apps > Advanced. Plan eligibility is inconsistent across sources (Pro/Plus/Business/Enterprise/Edu variously); workspace policy can block custom servers. Check the live page.
  - Write-capable tools prompt the user for confirmation per call unless the user relaxes it; tool annotations (read-only hint, etc.) drive this. After a server change the user must hit Refresh.
- Server-to-client features: vendor docs (Zuplo) say ChatGPT does not consume prompts, resources, roots, sampling or elicitation from remote servers; developers are asking for sampling on the OpenAI forum. Server-initiated notifications: no source addresses it; assume not usable for pushing messages into a chat. Conclusion: **pull only**. A teammate's message is delivered only if the model decides to call `check_inbox`, which depends on the user prompting it (a "check my Lightbulb inbox" habit) or on custom instructions saying to call it every turn.
- Does the connector work in the desktop app? Connectors are account-level and show up in the web and Mac apps [unverified for Mac]. No evidence that the Mac app runs local stdio MCP servers; searches found only third-party bridges.
- User experience: the user adds a connector once by hand, signs in via our OAuth page, and sees tool-call cards with confirmation prompts. Messages never arrive on their own.

### What `setup` could do
Nothing locally. We could print a checklist and a deep link, and host the endpoint ourselves. Hosting means a server that holds the user's inbox, which conflicts with the peer-to-peer, local-first design unless it is only a thin authenticated relay to the Mac (via OpenAI's secure tunnel or our own tunnel). That adds an always-on component and a security surface (prompt injection through a web-facing agent that can send messages to teammates).

## 3. Grok (xAI)

- Developer API: remote MCP tools in the xAI API, streaming HTTP and SSE only; `require_approval` and `connector_id` from the OpenAI Responses API are not supported (https://docs.x.ai/developers/tools/remote-mcp, from search summary). This is for apps calling Grok, not for the consumer Grok app.
- Consumer Grok: custom MCP connectors are added at grok.com/connectors > New Connector > Custom (https://docs.x.ai/grok/connectors/custom-mcp-tunneling, fetched). Grok's servers must reach the URL over the public internet; localhost/private addresses are rejected; use ngrok or Cloudflare Tunnel (Cloudflare quick tunnels do not carry SSE; streamable HTTP is fine). OAuth or API-key flows are completed inside Grok after the URL is added. Connectors launched on Grok Web around May 2026 with mobile "coming" (beginnersinai.org, low-authority) [unverified].
- Grok macOS app: I found no official launch page or docs for a Mac app or for local stdio MCP in it. One personal blog claims the new Mac app dropped local MCP; unconfirmed. Treat the Mac app as unknown until someone opens it and looks at Settings.
- Sampling/elicitation/notifications: nothing documented. Assume pull only.
- Registration: same as ChatGPT, only via tool calls. No hooks, no known on-disk logs. Accessibility scraping is possible but brittle.
- "Grok Bot": no official product found. If the owner means Telegram's Grok distribution, that is a Telegram bot surface, not a Mac app, and out of scope for a local hook design. If they mean a third-party bridge (e.g. OpenAB, Slack/Discord to an ACP CLI via `grok agent stdio`), that is the Grok CLI, which we already cover.

## 4. Recommendation

1. Ship Cursor IDE support first: reuse the Cursor hooks, verify IDE parity with a real run, document the `stop` follow-up and its loop limit. Small, local, low risk.
2. For ChatGPT and Grok, decide whether a pull-only hosted connector is worth it. One endpoint (streamable HTTP, MCP OAuth with discovery and DCR) would serve both, plus Claude.ai, which accepts the same shape. Cost: a hosted/tunneled endpoint, OAuth, manual user setup per app, and a clear prompt-injection story. Value is low without push.
3. Do not build Accessibility-API scraping or UI-typing automation for ChatGPT/Grok now. It is fragile and a privacy liability, and it needs the foreground app.
4. Before committing anything, run three quick tests: (a) Cursor IDE `stop` follow-up delivery, (b) add a throwaway HTTPS MCP server to ChatGPT and see whether the Mac app lists it and whether any server-initiated message surfaces, (c) open the Grok Mac app and check for a connectors/MCP settings page.

## Sources (accessed 2026-10-23)
- https://cursor.com/docs/agent/hooks
- https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- https://developers.openai.com/api/docs/guides/developer-mode (not fetched; via search)
- https://help.openai.com/en/articles/10119604-work-with-apps-on-macos (via search)
- https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- https://docs.x.ai/developers/tools/remote-mcp (via search)
- https://zuplo.com/docs/mcp-gateway/connect-clients/chatgpt.md (sampling/elicitation claim; vendor)
- https://help.boomi.com/docs/Atomsphere/Platform/Connect_chatgpt_setup (vendor)
- https://www.computing.co.uk/news/4332673/chatgpts-mac-app-exposed-user-conversations; https://heise.de/-9789679
- https://glama.ai/mcp/servers/xncbf/chatgpt-mcp
- https://techcrunch.com/2025/05/28/xai-to-invest-300m-in-telegram-integrate-grok-into-app
