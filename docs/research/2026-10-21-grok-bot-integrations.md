# Grok Bot integrations: how it receives and sends (research, 2026-10-21)

Read-only research. No accounts were created or connected. Sources are the
official docs at `docs.x.ai` (every page there is also served as `.md`; the
index is <https://docs.x.ai/llms.txt>) and the Cursor docs and help center,
which host the Grok Bot help pages. `x.ai/changelog/bot` returned HTTP 403 to
automated fetches, so changelog items here come from secondary coverage and
are marked as such.

Confidence labels:
- **Verified**: stated in an official xAI or Cursor doc page.
- **Single-source**: one non-official source, or a doc implication that no page states outright.
- **Not found**: searched for and not documented.

---

## 0. Context that changes the picture

- **The vendor is now "SpaceXSI" (xAI), and Grok Bot is built on Cursor.**
  The Grok Bot docs say Bots run "in Cursor's cloud". You sign in with a
  Cursor account. Admin controls are in the Cursor dashboard
  (`cursor.com/dashboard/bot`). Connectors are Cursor MCP plugins, governed by
  the team's Cursor connector policy. Grok Bot also ships with Cursor plans.
  (Verified: <https://docs.x.ai/grok-bot/overview>,
  <https://docs.x.ai/grok-bot/teams-and-enterprises>,
  <https://cursor.com/docs/grok-bot/teams.md>)
- **Billing is "The X Subscription"**, one plan covering Grok, Cursor and X.
  Grok Bot is included on Premium ($30/mo), Super ($100/mo) and Ultra
  ($200/mo), and on team seats at the same prices. It is **not** included on
  Plus ($8) or Free. Legacy Cursor Pro, Pro+, Ultra and Teams plans, and
  SuperGrok or X Premium+ links, still grant access. (Verified:
  <https://docs.x.ai/x-subscription/pricing>,
  <https://docs.x.ai/x-subscription/grok-bot>,
  <https://cursor.com/help/grok-bot/plans.md>)

## 1. Product map

| Product | What it is | Where it runs | Always-on bot? |
|---|---|---|---|
| **Grok** (grok.com, iOS, Android) | Consumer chat assistant, with Connectors (built-in OAuth connectors, a catalog, and custom MCP) | xAI cloud, per chat | No. It runs only when a person chats. No schedules or triggers are documented in the current docs (see 2.9). |
| **Grok Bot** (official name; the X account is [@bot](https://x.com/bot)) | Named, persistent "Bots" (AI teammates). Each user has **one persistent cloud computer** (a Firecracker microVM with browser, filesystem and terminal) that all of that user's Bots share. Includes routines (scheduled and event-triggered), plugins (MCP), Team Bots (shared, with their own Slack app), agent email, and group chats where Bots message each other. | Cursor's cloud. Desktop (macOS, Windows, Linux) and mobile apps are thin clients. | **Yes.** This is the product a team runs as an always-on bot. Work continues with the laptop closed. |
| **Team Bots** (part of Grok Bot; public beta, announced 2026-09-28 per secondary coverage) | One Bot that a whole team talks to. Each teammate has a private chat, and the Bot has team memory. It can have its own Slack app. | Grok Bot computers (see 4.5) | Yes |
| **xAI API** (`api.x.ai`) | Developer API: Responses API (OpenAI-compatible), Chat Completions (legacy), server-side tools, remote MCP, function calling, Batch, WebSocket mode, Voice | xAI servers. Your code runs the loop and hosts any listener. | Only if you build and host it yourself |
| **Grok Build** (the `grok` CLI; the name "Grok CLI" maps here) | Coding agent: TUI, headless `grok -p`, ACP server (`grok agent stdio`), MCP client, hooks, `/loop` | Your machine or server | Only if you run it as a daemon. `/loop` dies with the process. |

Sources: <https://docs.x.ai/grok/overview>, <https://docs.x.ai/grok-bot/overview>,
<https://docs.x.ai/grok-bot/team-bots>, <https://docs.x.ai/build/overview>,
<https://docs.x.ai/developers/tools/overview>. The grok.com FAQ says Grok Bot
"is not the same as Grok on grok.com" (<https://docs.x.ai/grok/faq>).

---

## 2. Inbound: every way Grok Bot (and the others) can be triggered from outside

### 2.1 Grok Bot routine: webhook trigger (Verified)
- A routine can be started by "a webhook call". You ask the Bot in chat to add
  a webhook trigger to a routine.
- The routine's **Webhook** section shows three fields:
  - **POST to**: the URL.
  - **key**: the secret.
  - **header**: the full header, ready to copy.
- **Auth format:** `Authorization: Bearer <key>`.
- **Body:** optional JSON. "The Bot receives that body with the routine
  instruction."
- **Response:** `200` means "accepted and started a run". It does not mean the
  run finished. Results go to the Bot's chat. Any other status means no run
  started.
- **Exact URL format: Not found.** The docs show only that the URL appears in
  the routine's details. No host or path pattern is published.
- **Known issue:** some runs fail with "Automation run rate limit exceeded".
  This is a limit that all Grok Bot routines share. Failed runs are not
  retried, so the sender must call again.
- Source: <https://cursor.com/help/grok-bot/routines.md>

### 2.2 Grok Bot routine: schedule (Verified)
- Plain-language schedules ("every weekday at 8:00 AM", "every 2 hours") use
  the time zone set in Settings → Bot.
- Runs must be at least 5 minutes apart.
- Limits: up to 50 routines per Bot, and the app keeps the last 20 run
  records per routine.
- A new routine waits for its next scheduled time. **Test** runs it at once
  and does real work.
- Grok Bot can pause routines after the user has been away for a long time.
- Sources: <https://docs.x.ai/grok-bot/skills-routines-and-automations>,
  <https://cursor.com/help/grok-bot/routines.md>

### 2.3 Grok Bot routine: Slack trigger (Verified)
- A routine can start when:
  - the Bot is mentioned
  - a word or phrase is mentioned
  - you react to a message
  - any message is posted
- It can watch one channel or all of Slack.
- It needs Slack connected on the Cursor account that owns the Bot.
- A trigger "is part of a routine, not the Slack app".
- Only new messages count.
- Whether posts from other bots or apps fire a personal routine's trigger is
  **not stated**.
- Sources: <https://cursor.com/help/grok-bot/routines.md>,
  <https://docs.x.ai/grok-bot/skills-routines-and-automations>

### 2.4 Grok Bot routine: other event sources (Verified, list only)
- The routines page lists "a Slack message, a GitHub, Linear, Sentry, or
  PagerDuty event, an email, or a webhook call".
- These events come through "Cursor account integrations", which are separate
  from the Slack and GitHub plugins.
- Per-source setup details are not documented on the Grok Bot pages.
- Source: <https://cursor.com/help/grok-bot/routines.md>

### 2.5 Grok Bot agent email (Verified)
- A user can claim **one** address, `name@mail.grokbot.com`.
  - It cannot be renamed or deleted.
  - Addresses are never reissued.
- Anyone can send mail to it.
- Incoming mail does **not** wake a Bot or start a chat by itself. Only a
  routine acts on it. The routine can filter on:
  - any sender
  - specific addresses
  - whole domains (`*@example.com`)
- By default, routines run only for senders that pass email authentication
  checks. Routines cannot filter by subject.
- Every outbound email waits as a draft until a person presses **Send**. There
  is no unattended sending.
- On Cursor teams, an admin turns this on in **Bot Capabilities → Agent
  Email**. It is off by default.
- Source: <https://cursor.com/help/grok-bot/agent-email.md>,
  <https://cursor.com/docs/grok-bot/teams.md>

### 2.6 Grok Bot in Slack: Team Bot app (Verified)
- DMs and @mentions in channels reach a Team Bot. See section 4.

### 2.7 Grok Bot on X: tag @bot (Verified)
- A post or reply that tags `@bot` becomes a task for the user's **main** Bot.
- The Bot receives:
  - the user's post
  - the post it replied to
  - any quoted posts, including still photos
- `@bot` posts only a short confirmation on X. Results stay in the app.
- Requirements:
  - a Grok account with the X account connected
  - Grok Bot access
  - at least one Bot of the user's own
- Tagging is not available for accounts based in Australia, the UK, the EU,
  Iceland, Liechtenstein or Norway.
- It does not fire when the post also tags `@grok`, or when the post (or a
  post it replies to or quotes) has a video or GIF.
- Source: <https://docs.x.ai/grok-bot/tag-on-x>

### 2.8 Grok Bot: Bot-to-Bot handoff and group chats (Verified, internal only)
- A user's Bots can send asynchronous messages to each other. A group chat
  holds 2–6 Bots.
- This works only inside one account (or through a Team Bot acting for a
  teammate). It is not an external entry point.
- Source: <https://docs.x.ai/grok-bot/chat-and-collaboration>,
  <https://cursor.com/help/grok-bot/group-chats.md>

### 2.9 Not found for Grok Bot
- **A public API to send a Bot a message or start a run.** The Cursor Admin
  API and Organization API manage Grok Bot settings and computers only (for
  example `GET /organizations/teams/{teamId}/grok-bot/operations/*`, at 120
  requests per minute). (<https://cursor.com/docs/api.md>,
  <https://docs.x.ai/grok-bot/computers>)
- **MCP-based triggers.** MCP servers are outbound tools only.
- **Discord, Telegram and Microsoft Teams triggers.** None are documented for
  Grok Bot.
- **Grok consumer app (grok.com).** The current docs have no scheduled tasks,
  webhooks or other triggers. Third-party 2025 tutorials describe a "Tasks"
  page on grok.com. That is single-source, and the current official docs do
  not include it.

### 2.10 Inbound to the xAI API and Grok Build
- **xAI API:** a call is the trigger. Your service calls `POST /v1/responses`
  with `Authorization: Bearer $XAI_API_KEY`. Any webhook, Slack, or schedule
  listener is yours to host. (Verified)
- **Grok Build:**
  - `grok -p "<prompt>"` runs headless (`--output-format json|streaming-json`,
    `--session-id` to resume, `--always-approve`).
  - `grok agent stdio` runs it as an ACP server (JSON-RPC on stdin/stdout).
  - `/loop 5m <prompt>` repeats a prompt. The minimum interval is 60 s, loops
    expire after 7 days, and up to 50 can be active at once.
  - Monitors turn the output lines of a script into notifications.
  - All of these need a running process that you host. (Verified:
    <https://docs.x.ai/build/cli/headless-scripting>,
    <https://docs.x.ai/build/features/background-tasks>)

---

## 3. Outbound: what Grok Bot (and the others) can call

### 3.1 Grok Bot plugins (MCP connectors)
- Connectors install as plugins from **Connect apps**. They are account-wide,
  so every Bot on the account can use them. In a chat, `@` attaches a
  connector to a task. (Verified: <https://docs.x.ai/grok-bot/computer-and-apps>)
- **Apps named in the docs:**
  - Gmail
  - Google Calendar, Drive, Docs, Sheets and Slides (Grok signs in to Google as "Grok")
  - Notion (a second account is possible)
  - Slack (posts as the connected Slack **user**)
  - X (private data only; needs X API credits on your own X developer account)
  - Zoom (OAuth currently broken, error 4700)
  - SharePoint
  - GitHub
  - "Origin"
  - Email (agent email)
  - (Verified: <https://cursor.com/help/grok-bot/connect-apps.md>,
    <https://cursor.com/docs/grok-bot/teams.md>)
- **Apps named only in Team Bots launch coverage:** Salesforce, Gong, Linear,
  Hex, Datadog, Cursor, Databricks, Statsig.
  (Single-source: <https://presenc.ai/research/grok-team-bots-2026>; the doc
  example uses Datadog and Linear plugins.)
- **The full catalog is not published as a list.** It is whatever the team's
  Cursor Marketplace allows.
- **Custom MCP servers** come in two kinds (Verified, from the Team Bots
  plugin table, <https://docs.x.ai/grok-bot/team-bots>):
  - **Remote HTTPS:** uses "the Bot's own credential, or each person's sign-in
    if the server uses OAuth".
  - **Command** (stdio on the Bot computer): its command and arguments are
    readable on the computer, so it must not carry secrets.
  - The docs say "for a custom server that needs secrets, choose **Remote
    HTTPS**".
- **How a custom server is added: no official step-by-step was found.** A
  third-party guide (Composio, 2026-09-03) says:
  - You ask the Bot in chat ("Add a custom MCP server called X at https://…").
  - The Bot shows the name and URL, and you choose **Add it**.
  - For OAuth servers, a connect card asks you to **Authorize**.
  - For an API-key server, you give the header name and the key, and the Bot
    "stores it as a header on the server entry".
  - (Single-source: <https://composio.dev/content/how-to-add-mcp-servers-to-grok-bot>)
- **OAuth runs from Cursor's infrastructure.** The MCP endpoint and its
  `/.well-known/` OAuth discovery paths must be public over HTTPS. (Cursor
  staff reply, 2026-08-12, single-source:
  <https://forum.cursor.com/t/grok-bot-custom-remote-mcp-oauth-never-starts-fetch-failed-same-url-works-in-cursor-ide/168188>)
- **A reported protocol bug:** Grok Bot omits `MCP-Protocol-Version` on reused
  sessions, which causes empty HTTP 400 responses. (Single-source forum
  report:
  <https://forum.cursor.com/t/grok-bot-remote-mcp-requests-omit-mcp-protocol-version-on-reused-sessions-causing-empty-http-400/174114/5>)
- **OAuth tokens stay on Cursor's connector backend.** Bots call tools without
  receiving the tokens. (Verified: <https://docs.x.ai/grok-bot/teams-and-enterprises>)

### 3.2 Grok Bot secrets (Verified)
- A secure secret card stores a value without showing it to the chat.
  - Values are masked, write-only, and replaced with `[REDACTED]` in command
    output, file reads and plugin calls.
- Team Bots: up to 25 secrets of 8–4,096 bytes each. Every teammate's
  conversation uses them.
- Enterprise **Team Secrets** (up to 100 per team) serve setup scripts only.
- Sources: <https://cursor.com/help/grok-bot/secrets.md>,
  <https://docs.x.ai/grok-bot/team-bots>,
  <https://cursor.com/docs/grok-bot/teams.md>

### 3.3 Grok Bot cloud computer: shell, HTTP and browser (Verified)
- Each Bot can use a terminal, a filesystem (`/workspace`) and a full browser
  with persistent logins. It can therefore run `curl`, scripts and CLIs, and
  drive any website ("computer use"). The Bot hands passwords, 2FA and
  CAPTCHAs to the human through **Take over**.
- Network egress is **allow-all** by default. Enterprise **Network Controls**
  can restrict destinations, optionally including MCP hosts.
- Members can route egress through their own desktop. Private networks can be
  reached through Tailscale, Cloudflare and similar, per
  <https://docs.x.ai/grok-bot/private-networks>.
- Public X search and read is built in and needs no connection.
- Sources: <https://docs.x.ai/grok-bot/computer-and-apps>,
  <https://docs.x.ai/grok-bot/security>

### 3.4 Grok Bot: other outbound paths (Verified)
- **Cloud Agents.** A Bot can delegate coding work to Cursor Cloud Agents.
  This is on by default and an admin can turn it off.
- **Local computer.** A Bot can run commands on the user's own desktop, with
  approval for each command by default.
- **Email.** Sending from `mail.grokbot.com` always needs an approved draft.
- **Slack.** Posting as the user goes through the Slack plugin. A Team Bot
  posts as its own Slack app.
- **Enterprise inference hooks.** These are an outbound, signed webhook from
  Cursor to an admin's DLP endpoint. They use Standard Webhooks HMAC headers
  plus up to 16 custom headers. This is a compliance feature, not a messaging
  path. (<https://cursor.com/docs/grok-bot/inference-hooks.md>)

### 3.5 Grok consumer app connectors (Verified)
- **Built-in OAuth connectors:**
  - Gmail & Google Calendar
  - Google Drive
  - OneDrive
  - Outlook Mail & Calendar
  - Microsoft Teams (read, send, reply, create chats)
  - SharePoint
  - Salesforce
- There is also a catalog of pre-configured OAuth connectors at
  `grok.com/connectors`, and **custom MCP** (New Connector → Custom → URL,
  then "complete any required authentication").
- The server must be on the public internet. Private and localhost addresses
  are rejected, and the docs give tunnelling instructions.
- The tunnelling page says "If your MCP server requires OAuth or API keys, you
  will still complete that flow in Grok". **Whether that means a static
  `Authorization` header field is not shown**, so this is single-source and
  implied only.
- On Grok Business and Enterprise, an admin must first provision each
  connector in `console.x.ai` → Grok Business → Connectors.
- **No Slack connector is listed for grok.com.**
- Sources: <https://docs.x.ai/grok/connectors>,
  <https://docs.x.ai/grok/connectors/custom-mcp-tunneling>,
  <https://docs.x.ai/grok/connector-management>,
  <https://docs.x.ai/grok/connectors/microsoft-teams>

### 3.6 xAI API tools (Verified)
- **Server-side tools:** `web_search` (includes browsing pages), `x_search`,
  `code_interpreter` / `code_execution` (Python sandbox), image generation,
  and collections search (RAG).
- **Function calling:** client-side tools, which can be mixed with
  server-side tools.
- **Remote MCP** (`"type": "mcp"`):
  - `server_url`: Streamable HTTP or SSE only.
  - `server_label`
  - `server_description`
  - `allowed_tools`
  - **`authorization`**: "A token that will be set in the Authorization header".
  - **`headers`**: additional headers. The native SDK names this `extra_headers`.
  - `require_approval` and `connector_id` (from the OpenAI spec) are **not
    supported**, so MCP calls run without an approval step.
  - Multiple servers can be used in one request.
  - Remote MCP is also supported in the Speech-to-Speech (voice) API.
  - `server_side_tool_usage` counts `mcp_calls`.
- **Pricing:** tokens plus a fee per server-side tool invocation. A 2025
  release note gives "no more than $5 per 1000 successful calls".
- Sources: <https://docs.x.ai/developers/tools/remote-mcp>,
  <https://docs.x.ai/developers/tools/overview>,
  <https://docs.x.ai/developers/pricing>,
  <https://docs.x.ai/developers/release-notes>

### 3.7 Grok Build MCP client (Verified)
- Examples:
  - `grok mcp add --transport http api https://mcp.example.com/mcp --header "Authorization: Bearer ${API_TOKEN}"`
    (`--header` is repeatable)
  - `grok mcp add --transport http linear https://mcp.linear.app/mcp`
    (OAuth through a browser; tokens are stored in `~/.grok/mcp_credentials.json`)
- In `config.toml`:
  - `[mcp_servers.<name>]` takes `url`, `headers`, and
    **`bearer_token_env_var`** ("Inject `Authorization: Bearer` from an
    environment variable").
  - `${VAR}` and `{{session_id}}` expand.
- Grok Build also reads MCP configs from `~/.claude.json`,
  `.cursor/mcp.json` and `.mcp.json`.
- **Hooks** can be `type: "http"` (POST the event to a URL) on these
  lifecycle events: `SessionStart`/`SessionEnd`, `UserPromptSubmit`,
  `Pre`/`PostToolUse`, `Stop`, `Notification`, `Subagent*`, and `*Compact`.
- Sources: <https://docs.x.ai/build/features/mcp-servers>,
  <https://docs.x.ai/build/settings/reference>,
  <https://docs.x.ai/build/features/hooks>

---

## 4. Slack in detail (Grok Bot)

There are **three distinct Slack mechanisms**. They are often confused.
(Verified: <https://cursor.com/help/grok-bot/connect-apps.md>,
<https://cursor.com/help/grok-bot/routines.md>,
<https://docs.x.ai/grok-bot/team-bots>)

1. **Slack plugin (outbound, personal).**
   - The Bot reads and posts **as the connected Slack user**.
   - It is not a bot user and needs no invite.
   - It is limited to channels that the user can post in.
   - It connects one workspace at a time.
2. **Slack routine trigger (inbound, personal).**
   - Trigger types: a mention of the Bot, a phrase, a reaction by you, or any
     message. Scope: one channel or all of Slack.
   - It runs the owner's routine on the owner's usage.
3. **Team Bot Slack app (inbound and outbound, shared).**
   - Each published Team Bot can get **its own Slack app**, named after the
     Bot and using its avatar.
   - **Install:**
     - The owner or a manager chooses Bot details → **Bring to your team's
       Slack** → **Connect Slack workspace**.
     - They approve Cursor and pick a workspace.
     - Grok Bot then creates and installs the app and sends a welcome DM.
     - Teammates add the app to channels with `/invite @BotName`.
     - Slack app-approval workflows are supported ("Awaiting admin approval").
   - **Behaviour:**
     - **DMs:** the Bot answers every message. A DM is one conversation.
     - **Channels and group DMs:** the Bot answers an @mention, then follows
       that thread without further mentions. Each thread is its own
       conversation.
     - **Top-level messages with no mention** are ignored, unless a "Slack
       channel listener routine" is set up.
   - **Who it answers:**
     - It answers only people who have **linked their Slack account to
       Cursor** (Settings → General → Team Bots) and who are on the Bot's
       Cursor team.
     - **Exception: "Posts from Slack workflows and other apps."** It does
       answer these, and their usage is billed to the **Bot owner**.
       (Verified, same page and
       <https://cursor.com/help/grok-bot/team-bots.md>)
     - Secondary coverage of the changelog says Team Bots "answer workflow
       mentions" (single-source).
   - **Which computer the work runs on:**
     - A DM runs on the asking teammate's own computer.
     - Channels, group DMs and threads run on one shared computer for that
       Bot.
   - **Approvals:** in a Slack thread the Bot posts a **Review an action**
     card. Only the person who asked can answer it. Where nobody can answer,
     the Bot runs without Auto-review, unless the team enforces Auto-review.
   - **Handoffs:** from a Slack thread, a Team Bot can hand work to the asking
     person's own Bots, and their replies post back into the thread.
     A personal Bot cannot do this from a channel thread.
     (<https://cursor.com/help/grok-bot/group-chats.md>)
   - **Personal Bots in Slack:** a personal Bot can be reached from "your own
     Slack DM with it" (Verified table on the group-chats help page; the setup
     path for this is not documented separately).
   - **Inference hooks:** when an enterprise hook blocks a message, the Bot
     replies in Slack. (<https://cursor.com/docs/grok-bot/inference-hooks.md>)

**How a third party could post to a Grok Bot through Slack:** a third-party
Slack app or a Slack workflow that @mentions a Team Bot in a channel where the
Bot is a member. Per the docs above, the Bot answers it and bills the owner.
This is inferred from the "other apps" exception. No doc shows a bot-to-bot
example.

**Other chat platforms:**
- **Microsoft Teams:** exists only as a grok.com connector. It is outbound,
  acts as the user, and is not a trigger.
- **Discord and Telegram:** not found in any xAI or Cursor doc for Grok or
  Grok Bot.
- **X:** works through `@bot` (section 2.7).
- **Grok for Slack:** a "Grok for Slack" app, separate from Team Bots, is
  claimed only by a competitor's blog. There is no official page for it
  (single-source, likely outdated).

---

## 5. Authentication when Grok calls a third-party MCP server or API

| Surface | OAuth | DCR required? | Static `Authorization: Bearer` | Custom headers | Notes |
|---|---|---|---|---|---|
| **xAI API remote MCP** | No built-in OAuth flow: you obtain a token yourself | n/a | **Yes**: `authorization` field (Verified) | **Yes**: `headers` / `extra_headers` (Verified) | Streamable HTTP or SSE only. Public URL. No approval step. |
| **Grok Build (CLI)** | Yes, through a browser on first use | Not stated | **Yes**: `--header` or `bearer_token_env_var` (Verified) | **Yes**: `headers` map with `${VAR}` (Verified) | Also reads `.mcp.json` and `.cursor/mcp.json` |
| **Grok Bot custom MCP (Remote HTTPS)** | Yes: "each person's sign-in if the server uses OAuth" (Verified). The flow runs from Cursor's cloud, and `/.well-known/` must be public (single-source). | Not stated for Grok Bot. Cursor's IDE MCP docs support **static OAuth client credentials** (`CLIENT_ID`/`CLIENT_SECRET`) "when the provider does not support DCR", with fixed redirect URI `https://www.cursor.com/agents/mcp/oauth/callback`. Whether Grok Bot's UI exposes that is **not found**. (<https://cursor.com/docs/mcp.md>) | "The Bot's own credential" is listed (Verified). A **header holding an API key** on the server entry is described only by Composio (single-source). | Single-source (header name plus value) | MCP calls go through Cursor's connector backend. Enterprise MCP allowlist and network policy apply. |
| **grok.com custom connector** | Yes. A community template uses a fixed client id "grok" with PKCE (single-source). | Not stated | "OAuth or API keys" is mentioned in the tunnelling doc. **No header field is shown** (implied only). | Not found | Business and Enterprise admins provision connectors in console.x.ai |

---

## 6. The xAI API side

- **API shape.** The xAI API has **no hosted "Agents API"** that runs
  unattended. The Responses API (`POST https://api.x.ai/v1/responses`,
  OpenAI-compatible, plus the native `xai_sdk`) runs the agentic tool loop
  server-side within one request.
  - Supported fields: `previous_response_id` and `store` (conversation state),
    streaming, WebSocket mode (a long-lived socket for tool-heavy loops),
    Batch, and Deferred/Async requests.
  - The `background` request flag is marked "(Unsupported)".
  - Source: <https://docs.x.ai/developers/rest-api-reference/inference/responses>
- **Remote MCP and other tools:** see 3.6.
- **Multi-agent:** `grok-4.20-multi-agent` (beta) is a model for
  multi-agent research, not a bot host.
  (<https://docs.x.ai/developers/model-capabilities/text/multi-agent>)
- **Current model:** `grok-4.7` (500k context).
  (<https://docs.x.ai/developers/release-notes>)
- **Self-hosting a "Grok Bot" with the Lightbulb MCP attached.**
  - This is documented to work at the protocol level. A sample request:
    `tools: [{"type":"mcp","server_url":"https://api.thelightbulb.company/mcp","server_label":"lightbulb","authorization":"lbk_…"}]`.
  - That request lets Grok call Lightbulb's tools.
  - Receiving still needs a listener that you host (for example, Lightbulb's
    webhook agent calling the xAI API).
  - Note: per the docs, `authorization` "will be set in the Authorization
    header". The docs do not say whether xAI adds the `Bearer ` prefix. This
    is not verified, so a test call is needed.
- **Rate limits:**
  - Limits are per model, in RPS and TPM, and rise with tiers based on
    cumulative spend: T0 $0, T1 $50, T2 $250, T3 $1k, T4 $5k, and an
    Enterprise tier on request.
  - `grok-4.7` at T0 allows 150 RPS and 50M TPM.
  - The API returns `429` when a limit is exceeded.
  - Source: <https://docs.x.ai/developers/rate-limits>
- **Other features:** mTLS, regional endpoints, and a Management API with
  audit logs are documented.

---

## 7. Limits, plans and admin controls (Grok Bot)

- **Plans** (Verified):
  - Grok Bot is included on The X Subscription Premium, Super and Ultra
    (individual or team seats at $30, $100 and $200 per seat).
  - Also included on legacy Cursor Pro, Pro+, Ultra and Teams, and through a
    SuperGrok / Plus / Heavy or X Premium+ link.
  - Also included through a Grok Business seat ("Sign in with Grok").
  - Usage comes from a **weekly** shared pool, plus top-ups or on-demand
    usage. There is no Grok Bot-specific spend cap.
  - Sources: <https://cursor.com/help/grok-bot/plans.md>,
    <https://docs.x.ai/grok-bot/teams-and-enterprises>
- **Team Bots:** available to members of a team. The public beta is on Teams
  and Enterprise (launch coverage, single-source). A Team Bot's usage bills
  the person it is answering. Routines bill whoever set them up.
  Unlinked Slack posts bill the owner. (Verified)
- **Routine limits:** 50 per Bot, at least 5 minutes between runs, a shared
  "automation run rate limit" (known issue), and an auto-pause after a long
  absence. (Verified)
- **Agent email limits:**
  - 50 recipients per email.
  - 10 attachments per message, up to 10 MB each and 25 MB in total.
  - Caps on how many new recipients a Bot can email.
  - Newsletters and first-contact emails go to one recipient each.
  - (Verified)
- **Admin controls on Teams and Enterprise** (Cursor dashboard → Grok Bot):
  Team Rules, Cloud Agent delegation, Agent Email, public template sharing,
  Execution on Local Computer, connector policy (Team Marketplace), and
  **Manage Team Bots**.
- **Enterprise-only admin controls:**
  - the organization-wide enable switch and group access
  - Network Controls (destination allowlist, optionally covering MCP)
  - the MCP allowlist
  - Team Setup scripts and Team Secrets
  - Allow Local Egress Routing and Allow Sudo
  - Action Recording and OTel export (with MCP tool I/O as an opt-in)
  - Enforce Auto-review and Auto-review rules
  - SCIM, audit logs and inference hooks
  - An Admin API and Organization API are documented.
  - (Verified: <https://docs.x.ai/grok-bot/teams-and-enterprises>,
    <https://cursor.com/docs/grok-bot/teams.md>)
- **Isolation:** one microVM per user, shared by all of that user's Bots.
  "Do not use separate Bots as a security boundary." (Verified)

---

## 8. Neutral table

| # | Integration path | Direction | How it works | Auth | Availability / plan | Confidence |
|---|---|---|---|---|---|---|
| 1 | Grok Bot routine **webhook trigger** | In | HTTP POST to the routine's URL with an optional JSON body. 200 means a run started. The result goes to the Bot's chat. | `Authorization: Bearer <routine key>` | Any Grok Bot plan | Verified (URL format not published) |
| 2 | Grok Bot routine **schedule** | In (time) | Plain-language schedule, at least 5 min apart | n/a | Any Grok Bot plan | Verified |
| 3 | Grok Bot routine **Slack trigger** | In | Mention, phrase, reaction or any message, in one channel or all of Slack. Runs the owner's routine. | Owner's Slack connection on the Cursor account | Any Grok Bot plan | Verified (bot-authored messages not addressed) |
| 4 | Grok Bot routine **GitHub / Linear / Sentry / PagerDuty event** | In | Cursor account integrations start a routine | The integration's own connection | Any Grok Bot plan | Verified (list only; setup undocumented) |
| 5 | Grok Bot **agent email** (`@mail.grokbot.com`) | In / Out | Incoming mail starts a routine when a sender filter matches. Sending needs an approved draft. | Sender auth checks; human Send | On by default for individuals; Teams need the admin switch | Verified |
| 6 | **Team Bot Slack app**: DM or @mention | In / Out | Own Slack app. Answers DMs and @mentions, then follows the thread. | Teammates must link Slack to Cursor. Slack workflows and other apps are answered and billed to the owner. | Team members (Team Bots beta) | Verified |
| 7 | Slack **plugin** | Out | Read and post as the connected Slack user | Slack OAuth (user) | Any Grok Bot plan | Verified |
| 8 | **@bot on X** | In | Tagged post becomes a task for the main Bot. @bot confirms on X. | Connected X and Grok accounts | Not in AU, UK, EU, IS, LI, NO | Verified |
| 9 | Grok Bot **catalog plugins** (Gmail, Google apps, Notion, X, SharePoint, GitHub, …) | Out | MCP plugins through Connect apps | Mostly OAuth per person; some by key | Subject to team connector policy | Verified (names); full catalog unlisted |
| 10 | Grok Bot **custom MCP, Remote HTTPS** | Out | Add a public HTTPS MCP server | OAuth per person, or "the Bot's own credential". An API-key header is described only by a third party. | Subject to team connector policy and the Enterprise MCP allowlist | Verified (existence); single-source (header mechanics) |
| 11 | Grok Bot **cloud computer** shell, browser, `curl` | Out | Bot runs commands or drives websites | Whatever the Bot holds: secrets, browser logins | Egress allow-all unless Enterprise Network Controls | Verified |
| 12 | Grok Bot → **Cursor Cloud Agents** | Out | Delegates coding tasks | Cursor team | On by default; an admin can disable it | Verified |
| 13 | Grok Bot **public run API** | In | None found | n/a | n/a | Not found |
| 14 | **grok.com Connectors** (built-in, catalog, custom MCP) | Out | Grok calls tools during a human chat | OAuth. "API keys" mentioned for custom connectors without detail. | All users. Business and Enterprise need admin provisioning. | Verified (OAuth); single-source (keys) |
| 15 | grok.com **scheduled Tasks** | In (time) | Not in current docs | n/a | n/a | Single-source (2025 third-party) |
| 16 | **xAI Responses API + remote MCP** | Out (and In through your own host) | Your service calls `/v1/responses` with `tools:[{type:"mcp", server_url, authorization, headers}]` | `XAI_API_KEY` to xAI. Static token in the `authorization` field plus custom `headers` to the MCP server. | Pay-as-you-go API, rate tiers T0–T4 | Verified (Bearer prefix behaviour untested) |
| 17 | xAI API **function calling** | Out | Model returns tool calls and your code executes them | Yours | API | Verified |
| 18 | xAI API server-side **web_search, x_search, code_execution, collections** | Out | xAI runs the tools | API key | API (per-call fee) | Verified |
| 19 | **Grok Build headless / ACP** | In | `grok -p`, or `grok agent stdio` (ACP JSON-RPC) on a host you run | `XAI_API_KEY` or `grok login` | Grok Build | Verified |
| 20 | **Grok Build MCP client** | Out | `grok mcp add --transport http … --header "Authorization: Bearer …"` | Static header, `bearer_token_env_var`, or OAuth | Grok Build | Verified |
| 21 | Grok Build **HTTP hooks** | Out | POST lifecycle events (for example `Stop`, `Notification`) to a URL | Not stated | Grok Build | Verified |
| 22 | Grok Build **`/loop`** | In (time) | Repeats a prompt (60 s minimum, 7-day expiry, up to 50 active) inside a running session | n/a | Grok Build | Verified |
| 23 | Discord / Telegram / MS Teams **triggers** | In | None found (MS Teams exists only as an outbound grok.com connector) | n/a | n/a | Not found |

---

## 9. Verified integration paths (as of 2026-10-21)

1. **Grok Bot routine webhook trigger.** POST with `Authorization: Bearer <routine key>` and an optional JSON body.
2. **Grok Bot routine schedules.**
3. **Grok Bot routine Slack triggers.**
4. **Grok Bot routine GitHub / Linear / Sentry / PagerDuty event triggers.** The list is verified; the setup steps are not documented.
5. **Grok Bot agent email.** `@mail.grokbot.com` mail can start routines. Every send needs approval.
6. **Team Bot Slack app.** Answers DMs and @mentions. Slack workflows and other apps can trigger it, and their usage is billed to the owner.
7. **Grok Bot Slack plugin.** Posts as the connected user.
8. **@bot on X.** Region-limited.
9. **Grok Bot plugins, including custom Remote HTTPS MCP servers.** OAuth, or a Bot-held credential.
10. **Grok Bot cloud computer.** Shell, browser and HTTP; egress is allow-all by default.
11. **grok.com Connectors, including custom MCP.** OAuth; used only during a human chat.
12. **xAI Responses API remote MCP.** A static `authorization` token and custom `headers` to any public Streamable-HTTP or SSE MCP server, and also function calling and server-side tools.
13. **Grok Build CLI.** A headless or ACP entry point, plus an MCP client with static `Authorization: Bearer` headers or OAuth.
