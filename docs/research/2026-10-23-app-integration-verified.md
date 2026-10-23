# App integration for chat apps: verified pass (2026-10-23)

Scope: can Lightbulb Connect treat the chat APPS the way it treats the four terminal agents (register a session, deliver a teammate's message, auto-wire in `setup`)? This pass replaces the three drafts dated 2026-10-23 (`…-anthropic-openai.md`, `…-openai-xai-cursor.md`, `…-mechanisms.md`). Every claim carries one label:

- **VERIFIED** — a vendor or spec page fetched directly on 2026-10-23; the quote and the URL are given. Search snippets, third-party blogs, GitHub issues and the npm `mcp-client-capabilities` registry do not count, and claims that rested only on them were deleted.
- **TESTED** — checked read-only on the owner's Mac on 2026-10-23 (bundle metadata, config key names, file names, process names, our own daemon state). No conversation content was opened, no app was launched, no setting was changed.
- **UNVERIFIED** — not settled; the exact test or source that would settle it is given.

## Apps on this Mac

- **TESTED** `/Applications` has `Claude.app` 3.6556.2 (`com.anthropic.claudefordesktop`), `ChatGPT.app` 26.1007.30912 (bundle id **`com.openai.codex`**), `Cursor.app` 2.1.49, and `Grok Bot.app` 0.68.1 (bundle id `com.anysphere.sand`, signed "Developer ID Application: Anysphere Incorporated (DCNK4UB866)", copyright "2026 SpaceXAI"). There is no separate `Codex.app` and no consumer `Grok.app`.
- **TESTED** So "the Codex app" and "ChatGPT desktop" are one app here, and "Grok Bot" is a real installed product made on Cursor's agent stack (its `package.json` lists `@anysphere/agent`, `@anysphere/cursor-config`, `@anysphere/grok-bot`; `"homepage": "https://cursor.com"`).

## Capability matrix

| App / surface | Register sessions? | Delivery | Auto-wire by `setup`? | What the user sees |
|---|---|---|---|---|
| **Claude app, Code tab, local session** | Yes, by doc: same hooks as the CLI (VERIFIED). Not yet seen live (TESTED: no Code-tab session in our DB) | Next step: `UserPromptSubmit` context, `Stop` block → continue (VERIFIED for Claude Code; Desktop firing UNVERIFIED). True push via channels: UNVERIFIED in Desktop (CLI-flag only per doc) | Yes, already done: `~/.claude/settings.json` hooks and `~/.claude.json` MCP both reach the Code tab (VERIFIED) | Nothing new to set up; a session should appear in `who` |
| **Claude app, chat** | No hook path (no doc names one) | Pull only: our tools when the model calls them. Sampling and resource subscriptions not supported (VERIFIED) | Yes: `mcpServers` in `claude_desktop_config.json`, then a full quit and restart (VERIFIED); or a `.mcpb` the user installs by hand (VERIFIED) | Tools under Connectors; a per-action approval |
| **Claude.ai (web)** | No | Pull only, through a remote connector | No: the user adds a custom connector by URL; the server must be reachable from Anthropic's IP ranges (VERIFIED) | Settings click-through plus OAuth |
| **ChatGPT desktop = Codex app, local threads** | Probably: the app embeds the full `codex` binary and shares `~/.codex` (TESTED); hooks in the app are not documented (UNVERIFIED) | Next step via Codex hooks: `UserPromptSubmit`/`SessionStart` context, `Stop` block → new continuation prompt (VERIFIED for Codex; in-app firing UNVERIFIED) | Yes: `~/.codex/hooks.json` + `[mcp_servers]` in `~/.codex/config.toml`, which "The ChatGPT desktop app, Codex CLI, and IDE extension share" (VERIFIED). Hook trust is per hash (VERIFIED); ours are already trusted (TESTED) | One-time hook trust review, if not already trusted |
| **ChatGPT web / desktop "Cloud" Work chats** | No local signal | **Push exists**: MCP Events webhook into the subscribed chat (VERIFIED), but only to a public remote MCP server on protocol 2026-07-28 | No: the user adds a custom MCP server in Plugins (VERIFIED) | User asks ChatGPT to monitor; events arrive in that chat |
| **Cursor IDE (agent)** | Yes: `sessionStart`/`stop`/`beforeSubmitPrompt` carry `conversation_id`, `workspace_roots`, `transcript_path` (VERIFIED) | Next step: `stop` → `followup_message` auto-submitted, default cap 5 (VERIFIED); `sessionStart`/`postToolUse` `additional_context` (VERIFIED) | Yes: `~/.cursor/hooks.json` + `~/.cursor/mcp.json` (VERIFIED); ours are written (TESTED). **Risk:** Cursor also runs `~/.claude/settings.json` hooks by default (VERIFIED) | Hooks run silently; follow-ups look like a typed message |
| **Grok Bot app** | No documented local hook config (UNVERIFIED; no `hooks.json` string in the bundle, TESTED) | Pull: a Bot can run approved commands on the Mac (VERIFIED), so it could call `lightbulb-connect`. Push: none found | No documented local MCP/hook file. Apps go through Cursor's connector backend (VERIFIED) | Per-command approval on the Mac |
| **Grok (grok.com consumer)** | No | Pull only, through a custom connector | No: grok.com/connectors > New Connector > Custom; localhost URLs are rejected (VERIFIED) | Manual connector, tunnel needed for a local server |

## Evidence by app

### 1. Claude app (`Claude.app` 3.6556.2)

Code tab:

- **VERIFIED** Hooks run in the Desktop app: "Claude Code fires the same hook events wherever it runs: sessions in the terminal, IDE extensions, the Desktop app, and cloud sessions." and "Cloud sessions don't read your local `~/.claude/settings.json`." — https://code.claude.com/docs/en/hooks
- **VERIFIED** Shared config: "Desktop and CLI read the same configuration files, so your setup carries over" … "**Hooks** and **skills** defined in settings apply to both" … "**MCP servers** configured in `~/.claude.json` or `.mcp.json` work in both". — https://code.claude.com/docs/en/desktop (section "Shared configuration")
- **VERIFIED** The Code tab runs its own Claude Code: "In a local session, the **Code** tab runs its own copy of Claude Code, which has its own version number. The desktop app downloads and updates that copy, so it can differ from the `claude` command in your terminal". — https://code.claude.com/docs/en/desktop
- **TESTED** That copy is on disk at `~/Library/Application Support/Claude/claude-code-vm/{2.1.304,2.1.308}/claude`; the terminal CLI here is `~/.local/share/claude/versions/2.1.308`.
- **VERIFIED** Desktop MCP feeds the Code tab too: "The Desktop app loads MCP servers from `claude_desktop_config.json` into local Code tab sessions, alongside servers from `~/.claude.json` and `.mcp.json`." and "The app also re-delivers stdio servers from `~/.claude.json` to the embedded CLI in local sessions." — https://code.claude.com/docs/en/desktop
- **TESTED** `~/.claude/settings.json` has our Connect hooks on `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd` (`lightbulb-connect hook claude <event>`), and `~/.claude.json` has a `lightbulb-connect` MCP server. By the two quotes above, both reach a local Code-tab session with no extra `setup` work.
- **VERIFIED** Hook payload gives registration data: common fields `session_id`, `transcript_path`, `cwd`, `hook_event_name`; `UserPromptSubmit` "can't replace the prompt; it only injects `additionalContext`"; `Stop` takes "`decision: "block"`, `reason`" and exit 2 "Prevents Claude from stopping, continues the conversation". — https://code.claude.com/docs/en/hooks
- **TESTED** The daemon has not seen a Code-tab session yet. `lightbulb-connect who` lists one session (`claude-1`), whose process is a child of `/Applications/Lightbulb.app/.../lightbulb-desktop`, not of `Claude.app`. All 4 rows in `~/.lightbulb/connect/state.db` `sessions` have executable `~/.local/share/claude/versions/2.1.308` or none; none point at `claude-code-vm`. No file under `claude-code-sessions/` changed since 2026-10-16, so the Code tab has not been used since Connect was installed (identity key written 2026-10-23 01:55). The absence proves nothing either way.
- **TESTED** A Code-tab session would be distinguishable: our `sessions.executable` column records the binary path, and the Code tab's binary lives under `claude-code-vm/`.
- **UNVERIFIED** That the hooks actually fire and the injected context reaches the model in a Code-tab session. Settle with live test L1.
- **VERIFIED** Channels are CLI-flag driven: "Being in `.mcp.json` isn't enough to push messages: a server also has to be named in `--channels`." Custom channels need `--dangerously-load-development-channels`, and "If you pass it in non-interactive mode with `-p` or through the Agent SDK, Claude Code ignores the flag". "Events only arrive while the session is open". "If several notifications arrive while Claude is busy, they're delivered together on the next turn". — https://code.claude.com/docs/en/channels.md and https://code.claude.com/docs/en/channels-reference
- **VERIFIED** A protocol trap for our MCP server: "On the v2 MCP client runtime, a channel can also fail to register because Claude Code doesn't register a channel server that negotiates protocol revision 2026-07-28." — https://code.claude.com/docs/en/channels.md
- **TESTED** The string `notifications/claude/channel` does not occur in `Claude.app/Contents/Resources`. (Weak evidence: the Code tab runs a separately downloaded binary.)
- **UNVERIFIED** Whether the Code tab can load a channel at all; the Desktop page never mentions channels and gives no way to pass CLI flags. Settle by asking Anthropic, or with live test L2.
- **VERIFIED** Anthropic ships a competing feature: "Cross-session messaging separately lets Claude message your other Claude Code sessions, including terminal sessions." — https://code.claude.com/docs/en/desktop

Chat surface:

- **VERIFIED** Config path and format: "**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`", with `{"mcpServers": {"<name>": {"command": …, "args": […]}}}`; "completely quit Claude Desktop and restart it". Logs: "`~/Library/Logs/Claude`", "`mcp.log`", "`mcp-server-SERVERNAME.log`". — https://modelcontextprotocol.io/docs/develop/connect-local-servers
- **TESTED** The owner's `claude_desktop_config.json` exists but has **no `mcpServers` key** (keys: `coworkUserFilesPath`, `preferences`). Today `setup` does not wire the chat surface.
- **VERIFIED** What Claude's client supports: "Tools, prompts, and resources" and text/image results; "Claude doesn't yet support these MCP features … Resource subscriptions; Sampling; Advanced or draft capabilities". Limits for "claude.ai and Desktop": "~150,000 characters", "240 seconds per tool call". — https://claude.com/docs/connectors/building/index.md
- **UNVERIFIED** Elicitation support in the Claude chat surface. The page above neither lists it as supported nor as unsupported. Settle by logging client capabilities in our MCP server on connect (the 2026-07-28 spec puts them in every request's `_meta`, see section 6).
- **VERIFIED** MCPB: "a zip archive containing a local MCP server and a `manifest.json`"; Node is "Included with Claude Desktop on macOS and Windows"; install is "Double-click the `.mcpb` file", drag into the window, or "**Settings > Extensions > Advanced settings > Install Extension…**", and "Each of these install methods opens an installation UI where the user reviews extension details and permissions". "the directory no longer accepts MCPB submissions". — https://claude.com/docs/connectors/building/mcpb.md
- **VERIFIED** Deep link: `claude://claude.ai/new?q=…` prefills a new chat so you "review and send it"; `claude://claude.ai/chat/{conversation-id}` opens an existing chat; `claude://code/new` "Opens Claude Code in Claude Desktop with the composer prefilled"; `q` "is truncated to about 14,000 characters". — https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link
- **TESTED** `Claude.app` registers the `claude` URL scheme (`CFBundleURLSchemes`).
- **UNVERIFIED** Whether `q` also prefills when opening an existing chat (`/chat/{id}?q=`). Settle with live test L5.

Claude.ai (web):

- **VERIFIED** "your MCP server must be reachable over the public internet from Anthropic's IP ranges"; Pro/Max add one at "Customize > Connectors", "+ Add", "Add custom connector"; Team/Enterprise need an Owner; "Local MCP servers configured in Claude Desktop via `claude_desktop_config.json` are a separate mechanism" and "those aren't available in Cowork or claude.ai". — https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- **VERIFIED** OAuth redirect "`https://claude.ai/api/mcp/auth_callback`"; Streamable HTTP, with legacy HTTP+SSE "being deprecated". — https://claude.com/docs/connectors/building/index.md

### 2. ChatGPT desktop app = Codex app (`ChatGPT.app` 26.1007.30912)

- **TESTED** Bundle id `com.openai.codex`; URL schemes include `codex`; `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` is a full Codex binary, and it contains the strings `UserPromptSubmit` and `hooks.json`. The UI bundle `app.asar` contains `hooks/list`, `Review hooks`, `hooksTrust`, `trusted_hash`.
- **TESTED** The app writes into the CLI's store: of 29 October rollouts in `~/.codex/sessions/2026/10/`, 17 have `originator: "Codex Desktop"` (only that field and `source` were read). `~/.codex/config.toml` has a `[desktop]` table of app preferences (e.g. `followUpQueueMode`, `preventSleepWhileRunning`).
- **TESTED** The newest Codex Desktop rollout is dated 2026-10-22; our `~/.codex/hooks.json` was written 2026-10-23 01:55. So the app has never run with our hooks in place, and `state.db` has no Codex session rows.
- **VERIFIED** MCP config is shared: "The ChatGPT desktop app, Codex CLI, and IDE extension share this configuration." Servers are `[mcp_servers.<server-name>]` with `command`/`args`/`env` or `url`; in the app, "Open Settings, select MCP servers, then Add server". Codex reads server "instructions … from the MCP `instructions` field during initialization". — https://learn.chatgpt.com/docs/extend/mcp?surface=cli (redirect from developers.openai.com/codex/mcp)
- **TESTED** `~/.codex/config.toml` `[mcp_servers]` already contains `lightbulb-connect`.
- **VERIFIED** Hooks: events `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStop`, `Stop`, `Interrupt`, `SessionStart`, `SubagentStart`, `SessionEnd`; locations `~/.codex/hooks.json`, `~/.codex/config.toml`, `<repo>/.codex/…`; "Hooks are enabled by default". For `Stop`, `decision: "block"` "tells Codex to continue and automatically creates a new continuation prompt that acts as a new user prompt", with `reason` as the text. `additionalContext` is documented for SessionStart, SubagentStart, UserPromptSubmit, PreToolUse, PostToolUse. — https://learn.chatgpt.com/docs/hooks (redirect from developers.openai.com/codex/hooks)
- **VERIFIED** Trust: "Before a non-managed hook can run, Codex requires you to review and trust the exact hook definition. Codex records trust against the hook's current hash, so new or changed hooks are marked for review and skipped until trusted." "Use `/hooks` in the CLI". — https://learn.chatgpt.com/docs/hooks.md
- **TESTED** Trust is stored in `~/.codex/config.toml` under `hooks.state."<source>:<event>:<i>:<j>".trusted_hash`, and all ten entries for `~/.codex/hooks.json` already have one. Implication: any change `setup` makes to our hook commands changes the hash and silently stops the hook until the user re-trusts.
- **VERIFIED** The app page names no hooks and no `~/.codex` sharing: it says "Download ChatGPT for macOS, Windows, or Linux" and "Choose ChatGPT or Codex". — https://learn.chatgpt.com/docs/app
- **UNVERIFIED** That the app's local threads fire our hooks and that `Stop` block / `additionalContext` reach the model there. Settle with live test L3.

### 3. ChatGPT web and desktop "Cloud" Work chats

- **VERIFIED** Custom MCP servers: "ChatGPT Plugins, select the plus button, then choose 'Add custom MCP server'"; "The MCP server is reachable through a public HTTPS endpoint or" through "Secure MCP Tunnel"; streamable HTTP "typically at `/mcp`"; "Account and workspace policies apply". — https://developers.openai.com/plugins/deploy/connect-chatgpt (redirect from developers.openai.com/apps-sdk/deploy/connect-chatgpt)
- **VERIFIED** "ChatGPT web uses remote MCP tools through plugins" and "doesn't read local Codex configuration files". — https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- **VERIFIED** **ChatGPT has a server→chat push**: "MCP Events lets ChatGPT subscribe to updates from your MCP server, such as new messages"; "ChatGPT subscribes through your MCP server and supplies a callback URL and signing secret"; "Your server sends matching events to that URL"; "ChatGPT receives the event in the subscribed chat and follows the user's instructions for how to respond". Scope: "available in Work chats on ChatGPT web, in Work chats in the desktop app with **Cloud** selected, and with dots". Requires "MCP 2.0 (protocol version `2026-07-28`)", methods `events/list`, `events/subscribe`, `events/unsubscribe`; "Polling, streaming, and the draft's `gap` and `terminated` control notifications are not supported". — https://developers.openai.com/plugins/build/mcp-events.md
- **VERIFIED** Local MCP in ChatGPT exists but is gated: "Desktop-only plugins require local execution on the user's computer"; "Local MCP submissions are limited to approved partners." — https://developers.openai.com/plugins/deploy/desktop-only.md
- **UNVERIFIED** Whether an MCP Event wakes an idle chat immediately or starts a task run later ("ChatGPT can group separately delivered events into one task run" is the only hint), and which plans have it. Settle with live test L6.
- **UNVERIFIED** "Developer mode" and plan eligibility: `developers.openai.com/api/docs/guides/developer-mode` returns 404; "Work with Apps" (`help.openai.com/en/articles/10119604`) returns 403. Settle by reading the current ChatGPT Help Center in a browser.

### 4. Cursor IDE (`Cursor.app` 2.1.49)

- **VERIFIED** Hook locations, priority "Enterprise → Team → Project → User", user file `~/.cursor/hooks.json`; common input includes `conversation_id` ("Stable ID of the conversation across many turns") and `workspace_roots`. — https://cursor.com/docs/hooks.md
- **VERIFIED** `stop`: "When provided and non-empty, Cursor will automatically submit it as the next user message"; "The default limit is 5 auto follow-ups per script, configurable via the `loop_limit` option". `beforeSubmitPrompt` outputs only `continue` and `user_message` (it cannot add context). `sessionStart` "runs as fire-and-forget" and returns `additional_context` for "the conversation's initial system context". `postToolUse` `additional_context` is "Extra context injected into the conversation after the tool result". — https://cursor.com/docs/hooks.md
- **VERIFIED** "User-level hooks (`~/.cursor/hooks.json`) are not available in cloud agents." — https://cursor.com/docs/hooks.md
- **VERIFIED** MCP: "Create `~/.cursor/mcp.json` in your home directory for tools available everywhere"; Cursor lists Tools, Prompts, Resources, Roots, **Elicitation** and Apps as "Supported". — https://cursor.com/docs/mcp.md
- **VERIFIED** **Double-hook risk**: "Cursor can load and execute hooks configured for Claude Code"; they load "when **Include Third-Party Plugins, Skills, and Other Configs** is enabled … The setting is on by default", from `~/.claude/settings.json` among others; "All matching hooks from every source run"; for Claude `Stop`, "a `decision` of `"block"` with a `reason` is treated as an automatic follow-up". — https://cursor.com/docs/reference/third-party-hooks.md
- **TESTED** Our `setup` writes both `~/.cursor/hooks.json` (`lightbulb-connect hook cursor <event>`) and `~/.claude/settings.json` (`lightbulb-connect hook claude <event>`). Under the doc above, a current Cursor agent turn would run both, which could register the same conversation twice (once as Claude) and deliver a message twice.
- **TESTED** The installed Cursor 2.1.49 bundle has no `Include Third-Party` or `.claude/settings` strings, so this build likely predates the feature; the risk arrives with an update.
- **UNVERIFIED** Actual double firing on a current Cursor. Settle with live test L4.

### 5. Grok Bot (`Grok Bot.app` 0.68.1)

- **VERIFIED** What it is: "Grok Bot gives you Bots you can keep around … Each Bot works on a persistent cloud computer with a browser, filesystem, and terminal"; "included with every paid individual Cursor plan and with the Cursor Teams plan" or a SuperGrok link. — https://cursor.com/docs/grok-bot.md (also listed in https://docs.x.ai/llms.txt under "Grok Bot")
- **VERIFIED** Local access: "A Bot runs commands on your local computer only under the local execution policy: per-command approval by default, settable to always allow, ask every time, or never." — https://cursor.com/docs/grok-bot/work.md
- **VERIFIED** Apps: "OAuth tokens are held on Cursor's connector backend"; "Grok Bot inherits your team's Cursor connector policy. There is no separate Grok Bot connector list". — https://cursor.com/docs/grok-bot/work.md and https://cursor.com/docs/grok-bot/teams.md
- **VERIFIED** Its "inference hooks" are an Enterprise policy webhook, not a local hook: they "send every governed Grok Bot model request to an AI security server your organization runs", and "Other Cursor features aren't covered … the CLI". — https://cursor.com/docs/grok-bot/inference-hooks.md
- **TESTED** Local state: `~/.grokbot/settings.json` has `mcpBoxServers` (a list of server-id strings, empty here) and `mcpCustomInstructions*`; `~/.grokbot/` has `local-exec-daemon.json`/`.log`, so the local-execution daemon is installed. The bundle contains Cursor's hook event map (including `UserPromptSubmit → beforeSubmitPrompt`, `Stop → stop`) and a `followup_message` field, but no `hooks.json` string, and it has code that resolves `~/.claude`, `~/.claude.json` and the Codex home.
- **UNVERIFIED** Whether Grok Bot runs any user-configured hooks or imports MCP servers from `~/.claude.json`/`~/.codex`. Settle by asking Cursor, or by checking Grok Bot's settings UI for an import option (live test L7).

### 6. Grok consumer (grok.com)

- **VERIFIED** "To add a custom MCP connector: 1. Go to grok.com/connectors. 2. Click **New Connector**, then select **Custom**. 3. Enter the MCP server URL"; "Your MCP server must be reachable over the public internet." — https://docs.x.ai/grok/connectors.md
- **VERIFIED** "Servers running on `localhost` or a private network address … are not directly reachable, and Grok will reject these URLs." — https://docs.x.ai/grok/connectors/custom-mcp-tunneling.md
- **UNVERIFIED** A consumer Grok macOS app and its MCP support: none is installed here and `x.ai/grok` returns 403. Settle by installing it on a test Mac and looking for a connectors page.

### 7. MCP protocol (spec 2026-07-28)

- **VERIFIED** "Make MCP stateless: remove the `initialize`/`notifications/initialized` handshake. Every request now carries its protocol version and client capabilities in `_meta`". — https://modelcontextprotocol.io/specification/2026-07-28/changelog.md
- **VERIFIED** "Replace the HTTP GET endpoint and `resources/subscribe`/`resources/unsubscribe` with `subscriptions/listen`" for `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions`. — same URL
- **VERIFIED** "Multi Round-Trip Requests (MRTR) pattern introduced which replaces the previous approach of sending server-initiated requests, such as `roots/list`, `sampling/createMessage`, or `elicitation/create`." — same URL
- **VERIFIED** "Deprecate the Roots, Sampling, and Logging features"; tasks move to the extension `io.modelcontextprotocol/tasks` with polling via `tasks/get`. — same URL
- **UNVERIFIED** (inference, not a quote) No core-protocol message puts text into a conversation; the only pushes into a chat found in vendor docs are a vendor extension (`claude/channel`) and a vendor feature (ChatGPT MCP Events). Settle by reading the full 2026-07-28 notification and subscription sections, not only the changelog.
- **VERIFIED** The official client matrix covers extensions only: "This matrix shows which MCP clients support each official extension", and the Claude (web) and Claude Desktop rows are empty. — https://modelcontextprotocol.io/extensions/client-matrix.md. (`modelcontextprotocol.io/clients` now redirects to the intro page.) There is no official per-client table for sampling, elicitation or notifications; per-client support must come from each vendor's own page.

## Deleted from the drafts (no primary support)

- "Grok Bot is not a product I could find" — wrong; see section 5.
- "ChatGPT: pull only, no server push" — wrong for remote servers; see MCP Events in section 3.
- "No evidence the ChatGPT Mac app runs local stdio MCP servers" — the app reads `[mcp_servers]` in `~/.codex/config.toml` (section 2).
- The npm `mcp-client-capabilities` rows (Codex elicitation yes, Claude.ai sampling no, and so on), Glama and directory claims, the claudeissues.com items, the Reason Network prior art, the aident.ai and GitHub-issue claims about Codex Desktop hook trust, the 2024 ChatGPT plain-text chat store, the community "chatgpt-mcp" UI driver, the Zuplo/Boomi vendor claims about ChatGPT features, and the per-client rows for VS Code, Windsurf, Gemini CLI and Zed. Each rested on third-party sources only.
- "`claude://` always starts a new chat" — `claude://claude.ai/chat/{id}` opens an existing one (section 1).
- Accessibility-API and System Events typing as a delivery route: no primary source covers typing into these apps. It is kept only as an unexplored option, not as a finding.

## Live tests that still need the owner

Each is one step; observe the named thing in `lightbulb-connect who` and in `~/.lightbulb/connect/daemon.log`.

- **L1 Claude Code tab.** Open a new local Code-tab session in any folder and send one prompt. Observe: a new `Claude Code` row whose executable is under `claude-code-vm/`, status `busy` then `idle`. Then send it a message from another session; observe that it appears on its next turn.
- **L2 Channels in the Code tab.** In that session run `/mcp` and `/status`. Observe: whether `lightbulb-connect` is listed and whether a "Channels (experimental)" notice ever appears. (No notice means channels are not available there.)
- **L3 ChatGPT/Codex app.** Open a local Codex thread in the app and send one prompt. Observe: whether a trust review appears, then a new `Codex` row in `who`; then a message delivered on `Stop`.
- **L4 Cursor double hooks.** After Cursor updates past 2.1.49, run one agent turn. Observe: one row or two rows (one `Cursor`, one `Claude Code`) for the same folder; check the Third-Party Imports setting.
- **L5 Claude deep link.** Run `open 'claude://claude.ai/new?q=test'` and observe a prefilled, unsent composer. Then try `claude://claude.ai/chat/<id>?q=test` and observe whether the existing chat is prefilled.
- **L6 ChatGPT MCP Events.** Only if we build a hosted endpoint: add a test server with one `message.created` event in a Work chat, POST one event, and observe whether the chat responds at once or later.
- **L7 Grok Bot.** Open Grok Bot settings and observe whether any option imports MCP servers or hooks from Claude Code, Codex or Cursor.

## Recommendation, ranked by evidence strength

1. **Claude Code tab: no new code, confirm with L1.** Strongest evidence (two Anthropic statements plus the binary on disk). Add a label in `who` from the executable path (`claude-code-vm/` → "Claude app").
2. **Fix the Cursor double-hook risk before Cursor updates.** Primary docs plus our own config show the overlap. When a `hook claude …` call comes from a Cursor process (or its payload carries Cursor fields such as `cursor_version`), drop it. Then confirm with L4.
3. **ChatGPT/Codex app: no new code, confirm with L3.** Config sharing is documented and the hooks are trusted on disk; only in-app hook firing is unproven. Warn in `setup` that changing a hook command needs a re-trust (hash-based trust).
4. **Claude chat surface: pull only.** Add `lightbulb-connect` to `mcpServers` in `claude_desktop_config.json` (documented path; needs a full restart). The same entry also reaches the Code tab, so keep the name equal to the `~/.claude.json` entry to avoid a duplicate. Label it "messages when Claude checks", not live.
5. **ChatGPT web/Cloud and Claude.ai/Grok: defer.** All need a public remote MCP endpoint and a manual connector step. ChatGPT MCP Events is the only documented push into a chat app, so if we ever host a relay endpoint, ChatGPT is the first target, not Claude.ai.
6. **Grok Bot: defer.** No local hook or MCP config is documented. A Bot could call the CLI under local-execution approval, which is pull only.
