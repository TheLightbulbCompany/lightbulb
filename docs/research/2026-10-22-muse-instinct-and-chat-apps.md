# Muse, Instinct, and the Claude / ChatGPT apps: how each could connect to Lightbulb

Researched 2026-10-22. Mostly third-party sources; Meta and Anthropic pages were read directly where noted. "UNVERIFIED" marks anything not confirmed by the maker's own docs.

## Local check (this Mac)

- No `muse` or `instinct` binary on PATH. Nothing named Muse/Instinct in /Applications or ~/Applications. No ~/.muse, ~/.instinct, or Application Support folder. (Bash checks run 2026-10-22.)
- `~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.0/skills/using-superpowers/references/muse-tools.md` exists. It maps skills to **Muse Code** tools (`read_file`, `bash`, `subagent_spawn`, `request_user_input`), says Muse reads `CLAUDE.md`/`AGENTS.md`, has `muse skills`, and injects the plugin bootstrap at `SessionStart` via a plugin hook. So "Muse" in the superpowers plugin is Meta's terminal coding agent, not the consumer app.

## There are two different "Muse" products from Meta

| | Muse Code | Muse (consumer app) |
|---|---|---|
| What | Terminal / CI coding agent (Meta Model API account) | Personal AI agent, launched 2026-09-08 |
| Platforms | macOS, Linux (Windows tab exists, no support statement) | iOS, Android, web (muse.ai), WhatsApp; Mac release 2026-09-18; US + adults only at launch |
| Sessions | Local process, local Unix socket between your own sessions | Agent runs in a Meta-cloud "Secure VM" (state storage location not stated) |
| Price | Three monthly plans (prices not listed on the official post; one secondary source says from $5) | Free (card required), Power $20/mo, Maximum $100/mo |

Sources: [Muse Code extending docs](https://dev.meta.ai/docs/muse-code/extending), [Muse Code plans and features post](https://dev.meta.ai/resources/blog/muse-code-new-plans-and-features/), [AI Agents Library](https://www.aiagentslibrary.com/blog/meta-muse-mcp/), [Parallel guide](https://parallel.ai/articles/meta-muse-custom-integrations.md) (written by an MCP vendor, not Meta), [Runtimewire](https://runtimewire.com/article/meta-muse-code-out-of-beta-monthly-plans) (price, secondary).

### Muse Code (official docs read directly)

- **MCP**: yes. `mcp_servers` block in the settings file; `stdio` (command/args/env) or `streamable_http` (url/headers); `${VAR}` interpolation; `enabled`, `mode` (required/optional). Remote OAuth via `muse mcp login <server>` / `logout`; dynamic client registration can be disabled. Tools can be selected by `mcp__server__*`. stdio servers get `MUSE_SESSION_ID`. Tools only; prompts/resources not mentioned. `/mcp` lists servers. `/migrate` imports MCP servers and memory from Claude Code or Codex.
- **Hooks**: yes. One shell command per event, from `<project>/.muse/hooks.json` (needs folder trust), user settings, or a managed file. Events: SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PostToolUseFailure, PostToolBatch, PreLLMCall, PostLLMCall, PreCompact, PostCompact, SubagentStart, SubagentStop, Notification, Stop, StopFailure, SessionEnd. The docs page gives no hook JSON snippet and does not say whether a hook can inject context back into the model (UNVERIFIED; that decides whether it can match Claude's hook-injection path).
- **Skills/plugins**: skills read from `.agents/skills`, `~/.agents/skills`, and also `.claude/skills` / `.codex/skills`; `muse skills install|import --from claude`. Plugin bundles can contribute skills (no more detail).
- **Headless**: `muse exec "prompt"` (also `--json`, `--prompt-file`, `--session-id` to resume, `muse export`). TypeScript SDK `@muse-code/sdk` (developer preview) drives a local `muse` host over stdio using the Muse Session Protocol ([post](https://dev.meta.ai/resources/blog/muse-code-new-plans-and-features/); repo github.com/meta-models/muse-code-sdk from the post, not opened).
- **Push into a running session**: Muse Code has inter-session messaging over a local Unix socket; the agent finds peers and sends with two built-in tools. The official post does not say an outside process can send. A secondary source ([AI Weekly](https://aiweekly.co/alerts/meta-graduates-muse-code-out-of-beta-with-inter-session-messaging-workflow)) mentions a `session-message send|serve` CLI. UNVERIFIED against Meta docs. If real, a local Lightbulb daemon could write to the socket for live push on the same machine.

### Muse consumer app

- **MCP**: no "add MCP server" setting. You ask Muse in chat to build a custom connector; it writes an MCP client on its VM (official MCP SDK), connects over streamable HTTP, and saves it as a skill. Bearer/API-key headers fit best; OAuth-only servers are awkward. Server must be public HTTPS; a server on your machine is unreachable (VM is not on your network). Keys entered in a secure prompt, not chat. Meta does not review custom connectors. ([Parallel](https://parallel.ai/articles/meta-muse-custom-integrations.md), [CData](https://www.cdata.com/blog/muse); both vendor guides, not Meta docs.) Separately, a Meta AI Connectors developer preview lets a service connect by API or MCP; limited preview ([AI Agents Library](https://www.aiagentslibrary.com/blog/meta-muse-mcp/)).
- **API / hooks / webhooks**: Parallel says there is "no public API for driving your Muse, adding connectors, or reading its skills from outside"; hooks and webhooks not mentioned. Muse VM has crons and sub-agents. UNVERIFIED against Meta's own pages.
- **Delivery tiers for Lightbulb**:

| Tier | Muse Code | Muse app |
|---|---|---|
| (a) live push | Possibly, via local session socket (UNVERIFIED), or via the SDK/MSP if Lightbulb runs the session | No |
| (b) next step via hooks | Yes in principle (UserPromptSubmit / Stop / SessionStart hooks calling a Lightbulb CLI); context injection UNVERIFIED | No |
| (c) pull via MCP tools | Yes (stdio or streamable HTTP, OAuth supported) | Yes, via a custom connector the user asks Muse to build; needs public HTTPS + bearer key |
| (d) webhook agent | Only as `muse exec` run by a Lightbulb-side runner | No inbound endpoint known; could only work via its crons polling MCP |

## Instinct

- **Who/what**: Personal AI assistant you text (iMessage, WhatsApp, reportedly Slack) or call; sign-in by phone number; web workspace at app.instinct.com for connections and settings. Made by Spear Street Technology, Inc. (San Francisco, registered April 2026, registrant reported as Noah Shinn); invite-only beta, free during beta, no published pricing. Funding reports (about $350M raised) are from secondary sources. ([Gyld](https://gyld.ai/blog/instinct-ai-review-impressive-agent-real-privacy-risks), [eesel review](https://www.eesel.ai/blog/instinct-ai-review), [Pear](https://pearmcp.com/guides/instinct)).
- **Not a phone app, desktop app, or CLI**: it is a cloud agent reached by messaging. Each user reportedly has a persistent cloud computer with a browser and cached credentials. Sessions are therefore cloud-side; one continuous thread. Data location not stated.
- **MCP**: not documented. Pear (checked 2026-10-04): "not found official public instructions for adding a custom MCP server". Bland's docs say Instinct's public site does not document custom MCP servers and tell users to ask Instinct first ([Bland](https://docs.bland.ai/integrations/mcp/instinct)). Conflicting signal: [Awaken's docs](https://awaken.tax/docs/consumer/instinct-mcp) reportedly describe "add Awaken as a new MCP connection" in Instinct with a bearer token (that page returned 403 to me; seen only through a search snippet). Instinct's own documented connection path is Composio-style managed app connectors (link texted to you). UNVERIFIED whether a custom MCP URL field exists.
- **Hooks / plugins / skills / webhooks / API**: none found. No public API found. (An Agent37 guide on building an Instinct-like assistant is explicitly not affiliated; ignore it for Instinct facts.)
- **Delivery tiers**:

| Tier | Instinct |
|---|---|
| (a) live push into session | No known way |
| (b) hooks | No |
| (c) pull via MCP tools | Maybe, only if its MCP connection feature exists; needs public streamable-HTTP URL + bearer. Ask Instinct / test in an invited account |
| (d) webhook agent | Not as a receiver. Reverse is plausible: Lightbulb texts Instinct's number or email identity as a human channel (UNVERIFIED, would be a messaging bridge, not an integration) |

## Claude apps (as of Oct 2026)

Source: [Anthropic help: connect remote MCP](https://support.claude.com/en/articles/11175166-how-to-connect-remote-mcp-integrations-to-claude) (read directly), plus [Boomi](https://help.boomi.com/docs/Atomsphere/Platform/Connect_claude_setup) (secondary).

- Custom connectors using remote MCP: Free (limit one), Pro, Max, Team, Enterprise. Added on claude.ai / Claude Desktop (Customize > Connectors > Add custom connector). Once added, they work in every Claude client including mobile; the Anthropic page does not say whether you can add one from the iOS app (one older Anthropic article said you cannot; UNVERIFIED now).
- Auth: OAuth (Claude's identity, dynamic registration, or your own client), or no sign-in, or fixed request headers (API key / bearer). Team: Owners add, members connect.
- Connections come from Anthropic's cloud, so the server must be public or allowlist Anthropic IPs. Page covers tools only (prompts/resources not described).
- Tiers: (c) yes. (a) live push into a running chat: no. (b) hooks: no (Claude Code has hooks; the apps do not). (d) no inbound webhook.

## ChatGPT apps (as of Oct 2026)

Source: OpenAI help article 12584461 (could not fetch, 403; content seen via search summary), [Coworker.ai](https://coworker.ai/blog/chatgpt-mcp), [Boomi](https://help.boomi.com/docs/Atomsphere/Platform/Connect_chatgpt_setup).

- Full custom MCP apps and developer mode: Business and Enterprise/Edu, on ChatGPT web, admin must enable; write actions in beta there. Plus/Pro: reported read/fetch-only custom MCP with developer mode; Free: no. Third-party guides conflict; OpenAI docs themselves reportedly conflict on Plus/Pro writes. Verify live before relying on it.
- Mobile/desktop: no source confirms custom connector creation on mobile or desktop apps; treat as web-only for adding (UNVERIFIED for use after adding).
- Tiers: (c) yes on eligible plans (HTTPS remote server, bearer/OAuth). (a), (b), (d) no.

## Bottom line for Lightbulb

1. **Muse Code** is the real fit: it has MCP (stdio and OAuth HTTP), hooks, skills that read `.claude/skills`, `muse exec`, an SDK, and `/migrate` from Claude Code. Treat it like Codex/Cursor: MCP server plus hooks, tier (b)+(c); check the socket / `session-message` route for (a).
2. **Muse app**: tier (c) only, through a public HTTPS MCP endpoint with a bearer key that the user asks Muse to wire up. No push.
3. **Instinct**: nothing confirmed. First step is asking an invited account whether it has a custom MCP field; otherwise skip.
4. **Claude / ChatGPT apps**: pull-only connector, so the existing Lightbulb remote MCP (OAuth) is enough; no push or hooks.

## Open questions

- Does Muse Code hook output get injected into model context? Does `session-message` accept outside senders? (Read dev.meta.ai/docs/muse-code session messaging and hooks pages.)
- Does Instinct have a custom MCP field (Awaken says yes, Instinct docs silent)?
- Exact ChatGPT Plus/Pro rules (open the OpenAI help article in a browser).
- Whether Claude iOS can add a connector.
