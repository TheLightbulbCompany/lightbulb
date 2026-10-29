# App integration mechanisms: getting a teammate message into a chat app

Date: 2026-10-23. Scope: cross-cutting mechanisms (MCP, macOS routes, pull patterns, prior art), independent of any one vendor. Clean-room: all sources are public docs, cited by URL. Items marked (UNVERIFIED) rest on third-party or incomplete sources.

## Summary matrix

| # | Mechanism | Verdict | Push into a live chat? | Setup auto-wire can do |
|---|---|---|---|---|
| 1 | MCP `notifications/claude/channel` (Claude Code only) | Works today (research preview) | Yes, true push, CLI only | Already the Claude Code path. Not for apps |
| 2 | MCP `resources/updated`, `list_changed`, logging, progress | Fragile / mostly no-op | No. Clients drop them or only refresh lists | Do not rely on it |
| 3 | MCP sampling (server asks client to run a turn) | Needs vendor change; now deprecated | Would be push, but apps do not support it (UNVERIFIED for Desktop) | Nothing |
| 4 | MCP elicitation (server asks user a form) | Works only inside an open tool call | No. Only while a tool call is in flight; Cursor supports it | Nothing for push; usable inside `check_inbox` |
| 5 | Streamable HTTP long-lived stream / `subscriptions/listen` | Spec only; client UI does not surface it | No | Nothing |
| 6 | MCP Tasks extension | Spec only; no client-initiated surfacing | No | Nothing |
| 7 | `claude://claude.ai/new?q=...` deep link | Works today | Partial: opens new chat with prompt PREFILLED, user presses Enter | Register a handler/notification action |
| 8 | Accessibility / AppleScript / System Events typing | Fragile | Yes in principle (types into focused field) | Offer as opt-in, never default |
| 9 | Menu-bar helper + notification + "Paste/Open" action | Works today (our own code) | Semi: user one-click | Install helper, ask for notification permission |
| 10 | Unread note appended to every tool result | Works today, all clients | Only on the next tool call | Add to server `instructions` + tool results |
| 11 | `check_inbox` tool with wait up to N seconds | Works today, all clients | Only when model calls it | Ship tool + instruction text |
| 12 | Scheduled/looping reminder prompt | Works with vendor feature (Claude Code `/loop`, app schedulers UNVERIFIED) | Periodic | Not auto-wirable for apps |

Bottom line: no chat app exposes a supported "inject as next prompt" door. The best honest tiering for apps is: notification with one-click open (deep link prefill) as the push substitute, plus pull (note on every tool result, `check_inbox`) for in-conversation freshness. Accessibility typing is the only true push and should be opt-in and loudly fragile.

## 1. MCP server-to-client features (state as of late 2026)

Spec revision 2026-07-28 is current (changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog.md, fetched 2026-10-23). Relevant facts:

- Stateless. No `initialize` handshake, no `Mcp-Session-Id`; the protocol-level session is gone. Each request carries version and capabilities in `_meta`.
- `subscriptions/listen` replaces the HTTP GET endpoint and `resources/subscribe`: one long-lived POST-response stream carrying `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resources/updated` for chosen URIs (https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions.md). Request-scoped `notifications/progress` and `notifications/message` flow only on the response stream of their own request. Logging notifications are emitted only when the request set a log level.
- Server-initiated requests (sampling, elicitation, roots) were replaced by Multi Round-Trip Requests: a server may only ask for input by answering a request with `input_required`; the client retries with the answers. So "server asks mid-nothing" does not exist; a server can only ask while a client request is open. This matters: server push to the model is not a protocol feature.
- Roots, Sampling, Logging are DEPRECATED in 2026-07-28 (12-month window). Do not build on sampling.
- Tasks moved to extension `io.modelcontextprotocol/tasks` (poll via `tasks/get`, `tasks/update`); servers may return task handles unsolicited. Still client-polled.
- SSE resumability removed; a broken stream loses the in-flight request.
- Net: the protocol gives a server a way to say "something changed" (list_changed/resources/updated), never "put this text in the conversation". Whether the model ever sees a notification is purely client policy.

Client reality (all dates 2026; thin sources, treat as indicative):
- Claude Code silently accepts then drops `notifications/resources/updated`; issue closed "not planned" 2026-05-24 (https://claudeissues.com/issue/47823-mcp-notifications-resources-updated-not-handled-per-spec). list_changed partly landed in 2.1.0 (https://claudeissues.com/issue/31893-claude-code-mcp-spec-compliance-list-changed-progress-sampling-messages-and-asyn).
- Claude Code instead has its own `claude/channel` experimental capability (see section 4 below, and https://code.claude.com/docs/en/channels-reference, fetched 2026-10-23): stdio server emits `notifications/claude/channel {content, meta}`; it arrives as a `<channel source=...>` tag, queued and delivered on the next turn; needs v2.1.80+, `--channels` or `--dangerously-load-development-channels`, interactive session only, org policy `channelsEnabled`. Sender gating is the server's job.

Client support matrix (what a server can initiate). Sources: https://modelcontextprotocol.io/extensions/client-matrix.md (extension table; cell values were stripped in fetch, so not usable), https://cursor.com/docs/context/mcp, https://mcpbundles.com/blog/state-of-mcp-clients, https://www.developersdigest.tech/mcp-servers/clients/claude-desktop, https://images.glama.ai/mcp/clients/claude-desktop (conflicts with the previous one), https://developers.openai.com/api/docs/guides/developer-mode.

| Client | Transports | Server can push into chat? | Notes |
|---|---|---|---|
| Claude Code | stdio, HTTP | YES via `claude/channel` (CLI only, preview) | resources/updated dropped |
| Claude Desktop | local stdio (config/extensions), remote custom connectors | No documented path | Sources conflict on sampling/roots/elicitation; one says tools/resources/prompts only (UNVERIFIED) |
| Claude.ai (web) | remote connectors only | No | Remote HTTPS + OAuth only |
| ChatGPT | remote only (SSE, streamable HTTP), Developer Mode; write tools need Business/Enterprise/Edu per one source | No | stdio unsupported; public HTTPS required, so a local Mac server needs a tunnel or our hosted relay; notifications/elicitation support not documented (UNVERIFIED) |
| Cursor | stdio, SSE, streamable HTTP | No push; elicitation and roots supported | Cursor agent has its own hooks (our existing path) |
| VS Code Copilot | stdio, http | No documented push | MCP Apps experimental; sampling/elicitation not stated on the page fetched (UNVERIFIED) |
| Windsurf | stdio, streamable-http (`serverUrl`) | Not documented | UNVERIFIED |
| Codex | stdio, `url` HTTP, OAuth | No documented push | Hooks give push for CLI (our path) |
| Gemini CLI | stdio, url | No documented push | UNVERIFIED |
| Zed | stdio, remote w/o headers | Not documented | UNVERIFIED |

Conclusion for (1): MCP alone cannot push into any app. Elicitation is the only server-to-user channel and only inside an in-flight tool call. Recommendation: do not spend effort on MCP push features for apps; the spec itself is moving away from them.

## 2. macOS routes when no API exists

a) URL scheme deep link (works today, Claude). `claude://claude.ai/new?q=<urlencoded>` opens Claude Desktop with the prompt prefilled for review; q truncated ~14,000 chars; app launches if closed. `claude://code/new?q=` does the same for the Claude Code composer. Source: https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link (fetched via search 2026-10-23). It prefills, never sends. Also steals focus and starts a NEW chat, not the existing one. No equivalent verified for ChatGPT/Grok desktop; web `?q=` style links for chatgpt.com and grok.com exist in practice but I did not verify them (UNVERIFIED). Permissions: none.
Verdict: works today, but only as "click to open a new prefilled chat".

b) Menu-bar helper + user notification with action. Our own helper posts a macOS notification ("Aden: ..."), with actions "Open in Claude" (deep link above), "Copy". Pure public APIs; needs notification permission only. Verdict: works today and is the most honest push substitute. Note the Lightbulb desktop app already has native notifications (UNUserNotificationCenter, per project docs).

c) Accessibility API (AXUIElement) set value / AppleScript `System Events` keystroke or clipboard paste + Return. Requires the user to grant Accessibility (and for automation, Automation per target app) in System Settings, via a TCC prompt; the binary must be signed or the grant resets. Electron apps expose a sparse AX tree unless `AXManualAccessibility` is set (https://www.electronjs.org/docs/tutorial/accessibility; the docs say the user's assistive tools override the app setting). Failure modes: app not frontmost, wrong window, existing draft overwritten, the model mid-answer, field renamed on update, IME/paste quirks, user typing at the same moment (corrupts both), security concern of injecting text that triggers tool calls. I found no source confirming AXValue set works on contenteditable chat inputs (UNVERIFIED); clipboard paste + Return is the common workaround and clobbers the user's clipboard unless restored. Verdict: fragile. Only as explicit per-user opt-in ("Type incoming messages into Claude app: experimental"), only when the app is frontmost and the field is empty, never send Return automatically unless chosen.

d) Shortcuts / `shortcuts run`. Can launch apps and open URLs, paste clipboard text; same limits as (a)-(c), plus per-shortcut approval. No added reach. Verdict: skip.

e) Computer-use style drivers (e.g. the cua-driver, ghost-os tools already on this Mac) do (c) with a vision fallback. More robust to UI change, slow, costly, and need Screen Recording as well. Fine for our own testing, not shipped default.

## 3. Pull patterns (work everywhere)

- Unread note on every tool result: server appends "N unread Lightbulb messages from X, call read_messages" to the result of every tool it owns (and tells the model in server `instructions` to check). Works in every client that calls our tools. Weakness: only fires when the model calls one of our tools; add a cheap `whoami`/`presence` tool and tell the model in `instructions` to call it at the start of each turn. Verdict: works today. Auto-wire: add the instruction text; nothing to install.
- `check_inbox(wait_seconds=N)`: long-poll tool that returns as soon as a message arrives or on timeout. Keep N under client tool timeouts (many clients cap ~60 s; ChatGPT and hosted connectors may be shorter, UNVERIFIED), so default 20-30 s. Turns the model into a listener in agentic apps; chatty and token-costly if looped. Verdict: works today.
- Scheduled reminder: Claude Code has `/loop` and the schedule skill; consumer apps' schedulers (Claude Cowork tasks, ChatGPT Tasks) were not confirmed by my searches (UNVERIFIED). A local watcher process that raises the macOS notification is the reliable pattern; the third-party reminder servers I saw use exactly that split (MCP server never shows UI, a separate watcher does): https://glama.ai/mcp/servers/mem6uikn5v. Verdict: use notification (2b), not a model-driven schedule.
- Model-triggered notify tool: a notification server fires only if the model calls it (same search). Not a push.

## 4. Prior art (public docs only)

- Reason Network: described as a peer-to-peer messaging layer for coding-agent sessions with "delivery tiers": Claude Code, Codex, opencode, pi get messages mid-session and idle sessions are woken; Cursor, Gemini, Copilot get them at their next step; `check_inbox` where push is impossible; a `notify` option shows a message on screen for approvals. Offline recipients queue. Only secondary source found (EveryDev listing and llms.txt): https://www.everydev.ai/tools/reason-network and https://www.everydev.ai/tools/reason-network/llms.txt. No chat-app (Claude/ChatGPT) delivery mentioned (UNVERIFIED that none exists; I did not reach their README). This tiering matches our hooks + MCP design, and confirms the industry has the same gap for apps.
- claude-code-relay (third party) uses `notifications/claude/channel` to relay between Claude Code sessions on one machine, receive-only, localhost: found via the channels search, https://code.claude.com/docs/en/channels-reference lists the official Telegram, Discord, iMessage, fakechat channels as the same pattern.
- OpenClaw and similar: I did not retrieve public docs on their delivery into chat apps in this pass; not claimed.
- Pattern across all: chat apps are reached through messenger bridges (Telegram/iMessage channels) rather than injection into the vendor app, i.e. the delivery target moves to a surface that has a real API.

## 5. Recommendation per app, and what `setup` can auto-wire

Honest tier ladder (state it in UI so owners know what they get):
1. Push (live session): terminal agents via hooks/channels. Already shipped.
2. Nudge (one click): macOS notification from our helper with "Open in Claude" deep link prefilled with the message plus a reply hint. Works today for Claude Desktop. For ChatGPT/Grok desktop: notification with "Copy" (clipboard) and "Open app"; prefill URLs unverified.
3. Pull: MCP server (local stdio for Claude Desktop/Cursor/VS Code/Codex/Gemini/Zed; remote HTTPS+OAuth endpoint for Claude.ai and ChatGPT) with `read_messages`, `check_inbox(wait)`, and unread note on every result, plus `instructions` telling the model to check at the start of each turn.
4. Opt-in experimental: Accessibility/paste typing into the focused field.

Setup-time auto-wire that is realistic:
- Claude Desktop: write the local MCP server entry (or install as a desktop extension) in its config; may need app restart; no consent dialog besides the app's own. Register the `claude://` helper notification.
- Cursor, VS Code, Windsurf, Codex, Gemini CLI, Zed: write each client's own MCP config file (paths and key names differ: `mcpServers` vs `servers` + `type: "http"` in VS Code; `serverUrl` in Windsurf; `config.toml` for Codex). Already close to what setup does for the terminal agents.
- Claude.ai and ChatGPT: cannot be auto-wired locally. They need a remote connector the user adds in settings (ChatGPT: Developer Mode toggle, plan-gated, public HTTPS). `setup` can only print the URL and steps, or open the settings page. A deep link for auto-adding connectors was requested but did not exist at last check (https://claudeissues.com/issue/33368-feature-request-deep-link-url-scheme-for-auto-adding-mcp-connectors).
- Never auto-grant Accessibility; `setup` may open the right System Settings pane and explain.

Vendor changes that would change the verdict (worth asking for): Claude Desktop adopting `claude/channel`; any app honoring MCP `resources/updated` as a visible context update; a `?q=&send=1` deep link.

## Open questions / uncertainty

- Per-client sampling, elicitation, roots, list_changed support: sources conflict or silent for Claude Desktop, ChatGPT, VS Code, Windsurf, Codex, Gemini CLI, Zed. The official matrix page at modelcontextprotocol.io/clients did not load as a feature table; verify each vendor page before putting any in user-facing copy.
- ChatGPT or Grok desktop URL schemes: not verified.
- Whether `claude/channel` will reach Claude Desktop: no evidence found.
- Tool-call timeout ceilings per client (limits `check_inbox` wait): not measured.
