# App integration research: Anthropic apps and OpenAI Codex app (2026-10-23)

Scope: can Lightbulb Connect treat the Claude desktop app, Claude.ai and the Codex app like the four terminal agents (register sessions, push a teammate's message, auto-wire in `setup`)? Sources are official docs fetched 2026-10-23 unless marked. Anything marked UNVERIFIED needs a live test before we build on it.

## Summary table

| App | Register sessions? | Push into open chat? | Auto-wire from `setup`? | Effort / risk |
|---|---|---|---|---|
| Claude desktop, **Code tab** | Yes. Reads the same settings files as the CLI, so Claude Code hooks apply (UNVERIFIED per event, see 1.2) | Yes via hooks (`UserPromptSubmit`/`Stop` context) the same way the CLI does; "channels" MCP push is a research preview | Yes: `~/.claude/settings.json` hooks + MCP already done for Claude Code | Low. Mostly free once the CLI path works; verify in app |
| Claude desktop, **chat tab** | Weak. No hooks. Only "a tool was called" signal | No. Pull only (tool call). No sampling, no resource subscriptions | Yes: `claude_desktop_config.json` `mcpServers`, or a `.mcpb` bundle. Needs app restart | Medium. Works as pull-only "inbox" tool |
| **Claude.ai** (browser) | No (no hooks, no local access) | No | No. Needs a remote HTTPS MCP server + OAuth + user adds connector by hand | High. Needs hosted relay; manual step |
| **Codex app** (macOS) | Probably yes. Hooks UI exists in the app; trust gate must be passed (UNVERIFIED end to end) | Yes if hooks run: `UserPromptSubmit`/`SessionStart` add context; `Stop` with `decision:"block"` starts a continuation prompt | Yes: `~/.codex/hooks.json` or `config.toml` + MCP in `config.toml`. Trust prompt is a manual step | Low to medium. Trust gating is the risk |

Bottom line shape: apps that run Claude Code or Codex engines under the hood (Claude Code tab, Codex app) inherit our hook path. Pure chat surfaces (Claude desktop chat, Claude.ai) get a pull-only tool at best.

## 1. Claude desktop app for macOS

### 1.1 Surfaces inside the app
- The app has a chat surface and a **Code** tab. In the Code tab each conversation is a session with its own folder; sessions run in parallel and can each use a git worktree (`<project-root>/.claude/worktrees/`). Local, Cloud, SSH sessions exist. https://code.claude.com/docs/en/desktop (fetched 2026-10-23)
- Local sessions have "direct access to your files and your `~/.claude` config"; cloud sessions "have no access to your local `~/.claude`". So only **Local** (and arguably SSH, on the remote host's home) sessions can use our hooks. Cloud sessions are out of reach.

### 1.2 Hooks in the Code tab
- Doc: "The desktop app reads the same settings files as the CLI." The same page discusses `disableAllHooks`, `allowManagedHooksOnly` and `strictPluginOnlyCustomization: ["hooks"]` as controls that Desktop reads, and says setting them stops "Claude Code hooks from user, project, and local settings". That implies user-level hooks in `~/.claude/settings.json` do run in Desktop local sessions.
- UNVERIFIED: whether every event (`SessionStart`, `UserPromptSubmit`, `Stop`) fires in Desktop and whether a hook's added context reaches the model. One third-party report shows hook commands failing in the Claude Code Desktop app on Windows (https://claudeissues.com/issue/29560-bug-hook-commands-not-executing-on-windows-desktop-app, date not checked). Test on macOS before promising.
- Registering a session: hook stdin gives `session_id`, `cwd`, `transcript_path` (same as CLI). Folder and repo come from `cwd`. Busy/idle from `UserPromptSubmit` and `Stop`. This reuses our CLI code unchanged. Tell Desktop sessions apart from CLI sessions by an env var or parent process (UNVERIFIED; check what the hook environment carries).

### 1.3 Push into a session
- Hook path (same as CLI): on `UserPromptSubmit`/`Stop`, a hook can return context or block-and-continue with a prompt. Works only when the session is active or just finished a turn. A truly idle session cannot be woken by a hook; nothing runs until the human types.
- **Channels** (Claude Code research preview): an MCP server declares the `claude/channel` capability and sends `notifications/claude/channel` with `content` and `meta`; Claude Code renders them as `<channel>` tags in the session. Needs Claude Code v2.1.80 or later; Team/Enterprise orgs must enable; custom channels are not on the approved allowlist during preview, so local use needs `--dangerously-load-development-channels`. https://code.claude.com/docs/en/channels-reference (search result snippet only; the full page was not read). UNVERIFIED whether the Desktop Code tab honors channels or that flag. Do not depend on it until tested; it would be the one true push (wakes an idle session).

### 1.4 Chat tab: MCP via `claude_desktop_config.json`
- macOS path: `~/Library/Application Support/Claude/claude_desktop_config.json` (third-party docs agree; Settings > Developer > Edit Config opens it). Format: `{"mcpServers": {"name": {"command": "...", "args": [...], "env": {...}}}}`. Restart required. The official config page I fetched did not state the path, so treat the path as well known but not officially cited.
- MCP client features, official: Claude supports tools, prompts, resources, text and image results. **Not supported: resource subscriptions, sampling, advanced/draft capabilities.** Tool call timeout 240 s, result cap about 150,000 chars. https://claude.com/docs/connectors/building/index.md
- Machine-readable registry (npm `mcp-client-capabilities`): Claude.ai and Claude Code both show sampling no, elicitation no; Claude Code roots yes. Claude Desktop is not listed. https://cdn.jsdelivr.net/npm/mcp-client-capabilities@0.0.14/README.md. Conflicting third-party claims that Claude Desktop supports elicitation/sampling (Glama directory, undated; NCP guide says elicitation missing as of Oct 2025) are not backed by Anthropic docs. Treat as no.
- `notifications/tools/list_changed` is a tool-list refresh signal only; it does not put text in a chat. No server-to-chat push exists in the chat tab.
- So in the chat tab a teammate message can only be **pulled**: the model must call `read_messages`/check-inbox. Mitigation: a tool description and server `instructions` that tell the model to call inbox at the start of each turn; a tool result can carry a "you have N unread" banner on every call. Still depends on the model.

### 1.5 Desktop Extensions (`.mcpb`, formerly `.dxt`)
- A `.mcpb` is a zip with `manifest.json` plus a stdio MCP server; Node is bundled with the app on macOS and Windows so Node servers need no runtime from the user. CLI: `npm i -g @anthropic-ai/mcpb`, `mcpb init`, `mcpb pack`. `user_config` in the manifest auto-generates a settings UI. Spec: https://github.com/modelcontextprotocol/mcpb (MANIFEST.md). Docs: https://claude.com/docs/connectors/building/mcpb.md
- Install is **always user-driven**: double-click the file, drag it into the window, or Settings > Extensions > Advanced > Install Extension. Each opens a review UI for permissions. No documented silent install. Directory submission for MCPB is closed ("deprecated"); distribution is by file or by bundling into a plugin.
- Enterprise: keys `isDesktopExtensionEnabled`, `isDesktopExtensionSignatureRequired`, `managedMcpServers`, `isLocalDevMcpEnabled` in managed config (MDM). https://claude.com/docs/third-party/claude-desktop/configuration.md. Not for us.
- Verdict: for `setup`, editing `claude_desktop_config.json` is more automatic than `.mcpb` (no click-through). A `.mcpb` buys a nicer permission UI and no Node path issues. Offer config edit by default, `.mcpb` as the "no terminal" path.

### 1.6 Other signals for chat-tab conversations
- No hook, no event API, no documented session log for chat conversations. Conversation data lives in the app's own storage (Electron/IndexedDB-style; UNVERIFIED, not inspected). Reading it would be fragile and could touch private content; do not.
- Accessibility API (macOS AX): Electron apps expose a tree, so a window title and "is a response streaming" might be readable, and typing into the composer is possible with AX or synthetic keys. UNVERIFIED and brittle; needs the Accessibility permission; breaks on UI changes. Last resort, opt-in only. Our own repo already has AX tooling elsewhere (desktop app), but do not rely on it for a launch feature.
- What the user sees: the extension/server appears under the connectors/tools menu; tool calls show an approval prompt per tool unless "always allow" is chosen. Each prompt for `send_message` is visible friction, and arguably good.

## 2. Claude.ai in the browser

- Only **remote** MCP connectors (Streamable HTTP, legacy SSE being deprecated). Local stdio servers do not work in the browser. Same connector infrastructure backs claude.ai, desktop, mobile, Cowork and Claude Code. https://claude.com/docs/connectors/building/index.md
- Auth: OAuth 2.0 (specs 2025-03-26, 2025-06-18, 2025-11-25), Dynamic Client Registration or manual client registration, redirect `https://claude.ai/api/mcp/auth_callback`; or a static header credential set once by an org Owner; or none. https://claude.com/docs/connectors/building/authentication.md (linked, not read).
- Adding: user (or org Owner on Team/Enterprise) pastes the URL as a **custom connector** in settings; no Anthropic review. This is a manual step, so `setup` cannot automate it. Directory listing needs review.
- Cannot: register sessions (no signal), push (no sampling/subscriptions/elicitation per the registry), run on the user's Mac. Can: pull tools (find sessions, send, receipt, inbox) against our relay if we host a public HTTPS MCP endpoint with OAuth. Results are sanitized by Anthropic infrastructure; 240 s tool timeout.
- The Lightbulb relay already exists as a hosted backend (api.thelightbulb.company), so a remote MCP facade is plausible, but identity mapping (Clerk to connector OAuth) is a real build. Also a Claude.ai chat is not a "session with a folder"; it would be a person-level inbox, not a session.
- Effort: high. Value: low until sessions mean something there. Recommend deferring.

## 3. OpenAI Codex desktop app

### 3.1 What it is now
- The quickstart says "Download ChatGPT for macOS, Windows, or Linux"; Codex is delivered inside the ChatGPT desktop app, with parallel projects, scheduled tasks, worktrees and a Hooks page. https://learn.chatgpt.com/docs/app (redirected from developers.openai.com/codex/app). A third-party write-up (2026-07-17) describes the Codex-to-ChatGPT desktop merger and CLI detection workarounds: https://codex.danielvaughan.com/2026/07/17/codex-chatgpt-unified-desktop-app-cli-migration-codex-app-detection-workarounds/ (not read in full). Naming and paths may have moved; re-check on a real install.

### 3.2 Hooks
- Official hooks page: https://learn.chatgpt.com/docs/hooks. Events: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`.
- Locations: `~/.codex/hooks.json`, `~/.codex/config.toml` (`[hooks]` tables), `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`. All matching sources load. On by default (`[features] hooks = false` turns off; `codex_hooks` is the old alias).
- Handlers: `command` (JSON on stdin with `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, plus `prompt`/tool fields) or `mcp_tool` (calls a tool on an already-connected MCP server; does not start servers). `prompt` and `agent` handlers are skipped.
- Output: exit 0 ok; exit 2 blocks; JSON `continue`, `systemMessage`, `hookSpecificOutput.additionalContext` (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStart). Plain stdout from SessionStart/UserPromptSubmit/SubagentStart becomes developer context. `Stop` with `decision:"block"` + `reason` starts a continuation prompt (this is our delivery lever for a just-finished turn). Context over about 2,500 tokens is truncated to a preview.
- **Trust gate:** non-managed hooks must be reviewed and trusted before they run (`/hooks` in the CLI). This is the main automation obstacle: `setup` can write the file but the user must approve once.
- **Does the app run hooks?** The page does not say. Evidence the app has them: a Hooks UI in the Windows app lists a user SessionStart hook but did not execute it (https://github.com/openai/codex/issues/35863, date not checked), and a third-party post says Desktop silently skips untrusted project hooks until trusted via CLI `/hooks` (https://aident.ai/blog/fix-codex-desktop-project-hooks-not-running-trust). So: hooks exist in the app, trust-gated, with at least one platform bug. macOS behavior UNVERIFIED.

### 3.3 MCP
- Config: `[mcp_servers.<name>]` with `command`/`args`/`env` in `~/.codex/config.toml` (third-party guides agree; official MCP page URL I tried returned 404). A macOS app GitHub issue shows Settings > MCP Servers reading `~/.codex/config.toml` and allowing per-server toggles (https://github.com/openai/codex/issues/13075, date not checked). CLI and IDE extension share the file; the app appears to as well.
- Client features per the registry: tools yes, resources/prompts no, sampling no, tasks no, roots no, **elicitation yes** (OpenAI Codex row). ChatGPT row: tools only. https://cdn.jsdelivr.net/npm/mcp-client-capabilities@0.0.14/README.md (version/date not shown).
- Elicitation is a server-initiated prompt to the human, not a message into the model's chat. It could surface "Alice sent you a message, read it?" as a dialog, but only during an in-flight tool call (elicitation is requested while handling a request). It cannot arrive unprompted. Not a push channel.

### 3.4 Sessions on disk
- Codex CLI writes rollouts under `~/.codex/sessions/` (my memory of CLI behavior; not re-verified today). The hook payload's `transcript_path` is the supported pointer, so use that instead of scanning disk.

### 3.5 What the user sees
- First run: Codex shows a hooks trust prompt/Hooks UI entry for the Lightbulb hook; after approving, a teammate message appears as developer context or a continuation turn. MCP tools show as a server in Settings > MCP Servers with per-tool approvals.

## 4. Push versus pull, by client (late 2026)

| Mechanism | Claude.ai | Claude desktop chat | Claude Code tab | Codex app |
|---|---|---|---|---|
| Tools (pull) | yes | yes | yes | yes |
| `list_changed` notifications | refreshes tool list only | same | same | same (UNVERIFIED) |
| Resource subscriptions | no (official) | no (official) | n/a | resources unsupported |
| Sampling | no | no | no | no |
| Elicitation | no | no (UNVERIFIED) | no | yes (in-flight only) |
| Hooks adding context / continuing | none | none | yes (settings.json) | yes (trust-gated) |
| True wake of an idle session | none | none | channels preview only | none found (`Interrupt` is not a wake) |

No client lets a server push text into an idle chat except Claude Code channels in preview. Everything else delivers at the next turn boundary (hook) or when the model chooses to call a tool.

## 5. Recommendation order

1. **Claude Code tab**: confirm on a real Mac that our existing `~/.claude/settings.json` hooks fire for a Local Desktop session and tag them `app: claude-desktop`. Likely near-zero code.
2. **Codex app**: write `~/.codex/hooks.json` and the `config.toml` MCP entry; tell the user about the one-time trust approval; test that the app (not just CLI) runs them on macOS.
3. **Claude desktop chat**: ship an `mcpServers` edit (and optionally a `.mcpb`) giving the pull tools plus server `instructions` and an unread banner on every tool result. Label it "inbox by tool call", not live.
4. **Claude.ai**: defer. Needs hosted MCP + OAuth and has no session concept.
5. Watch Claude Code channels; if it graduates from preview, it is the only genuine push.

## 6. Open questions needing a live test
- Do `SessionStart`/`UserPromptSubmit`/`Stop` hooks fire in the Claude desktop Code tab on macOS, and does injected context reach the model?
- Does the Desktop Code tab load channels / `--dangerously-load-development-channels`?
- Does the Codex macOS app execute hooks after trust, and does the trust prompt appear in-app?
- Exact Codex app config path now that Codex ships inside the ChatGPT app.
- Whether Claude desktop chat sends `initialize` capabilities with elicitation (log the client capabilities in our MCP server on connect; one afternoon of work, settles every UNVERIFIED above).
