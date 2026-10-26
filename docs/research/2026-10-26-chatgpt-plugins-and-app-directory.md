# ChatGPT plugins and the plugin directory: how Lightbulb gets listed

Date: 2026-10-26. Sources are official OpenAI docs unless marked. Several
help-center pages returned 403 to the fetcher; claims that rest only on
search snippets or third-party posts are marked **(unverified)**.

## 1. What the words mean today

- **Plugin** is the install unit. "Plugins bundle capabilities into reusable
  workflows in ChatGPT and Codex." A plugin can hold skills, one MCP server,
  an app mapping, and (desktop manual install only) hooks. [S1, S2]
- **App** is what used to be an "Apps SDK app": an MCP server connection,
  optionally with UI widgets. In July 2026 OpenAI renamed the App Directory to
  the Plugin Directory (chatgpt.com/plugins); apps now ship inside plugins.
  **(unverified date: third-party and Wikipedia say 2026-07-09)** [S10]
- **MCP** is the tool transport. Public plugins must use a remote HTTPS
  Streamable HTTP server. Local stdio servers are "desktop-only". [S1, S4]
- **Skill** is `skills/<name>/SKILL.md`, the same layout as Claude Code. [S1]
- **Surfaces.** ChatGPT Chat and Work on web, desktop and mobile; Codex in the
  ChatGPT desktop app and Codex CLI (the IDE extension has no plugins). Mobile
  can only use plugins already on the account. Desktop-only plugins can be
  found on web but install and run only in the desktop app. "ChatGPT and Codex
  use the same public plugin catalog." [S2]
- This matches what the owner saw: the ChatGPT macOS app (bundle id
  `com.openai.codex`) with Chat and Work modes and a Plugins / Apps / MCPs /
  Skills / Marketplace settings page.

## 2. Getting into the public directory

- **Where:** the plugin submission portal, platform.openai.com/plugins →
  Create plugin → "With MCP" (remote server) or "Skills only". [S3, S5]
- **Who:** org owners, or members with Apps Management Write
  (`api.apps.write`). Identity verification (individual or business) is
  required; the directory shows the verified name. Unverified names are
  rejected. EU data-residency projects cannot submit MCP plugins. [S3, S6]
- **Listing:** `displayName` and `shortDescription` ≤ 30 chars,
  `longDescription` ≤ 4,000, `developerName`, `category`, square icon ≥ 48 px;
  HTTPS `websiteURL`, `supportURL`, `privacyPolicyURL`, `termsOfServiceURL`. [S3]
- **Review kit (MCP plugins):** 5 positive + 3 negative test cases, a demo
  recording URL, and reviewer credentials entered in the dashboard. The test
  account must work "without MFA approval, email or SMS codes, magic links, or
  private-network access", must hold sample data, and must not need a fresh
  sign-up. [S3, S4, S6]
- **Domain proof:** serve the portal token as plain text at
  `/.well-known/openai-apps-challenge` on the MCP host. [S3]
- **Policy:** clear privacy policy (categories, purposes, recipients,
  retention, controls); minimum data; no full chat history; no credentials or
  secrets in tool I/O; no ads; general audience 13+; no "unofficial connector"
  pass-throughs; no trial/demo plugins; each tool its own tool, not a generic
  executor. [S4]
- **Money:** no listing fee or revenue share is documented. Commerce is
  physical goods only. A plugin may explain plan requirements but may not link
  to checkout/upgrade flows or show subscription plans. [S3, S4]
- **Countries:** `publication.countries` allowlist (e.g. `["US","GB"]`); `[]`
  means no restriction. [S3]
- **Process:** fix scan findings → Submit for review → attestations → email
  feedback (reply to appeal). One review active per plugin. After approval you
  choose when to Publish. MCP server changes are rescanned daily; new tools
  stay hidden until approved. Changing the MCP origin (host/port) needs a new
  plugin. [S3, S6]
- **Timeline:** none given; "expedited review requests won't be
  accommodated". [S6] Community reports (April–Sept 2026): 14 days, "30 days or
  more is not uncommon", 60–120 days, one approved app stuck unpublishable for
  months. [S11] Plan for weeks to months.
- **Plans and workspaces:** the directory is visible on all plans; use depends
  on plan, workspace settings and role. Business: plugins on by default.
  Enterprise/Edu: new plugins disabled by default; admins enable them and set
  roles (`Use plugins`). **(unverified: from help-center search snippets, page
  403)** [S7, S8]

## 3. Plugin format vs Claude Code

- Portable format: root `plugin.json` (`name`, `version`, `description`,
  `author`, …), `skills/`, root `mcp.json` (`mcpServers`, `type:
  "streamable-http"`, `url`), OpenAI settings under
  `extensions["com.openai"]` (`interface`, `auth`, `hooks`,
  `onboardingSkill`, `apps`). `.codex-plugin/plugin.json` still works as a
  fallback. [S1]
- Claude compatibility: OpenAI "accepts legacy and Claude-compatible
  manifests"; local marketplaces read `.claude-plugin/marketplace.json`;
  Codex sets `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA` for hooks. That is why
  our Claude Code plugins already show up in the app. [S1]
- Submitting a Claude plugin: upload the archive with
  `.claude-plugin/plugin.json`; the portal converts it. `commands` and
  `agents` are dropped (turn them into skills). Hooks must be removed for the
  public directory. `userConfig`, live artifacts, `outputStyles`, `channels`,
  `.mcpb` are not supported. [S5]
- **Remote MCP:** yes, one server per plugin. **Local stdio MCP:** only as a
  "Desktop only" plugin, and local-MCP submissions are limited to partners
  whose org ID an OpenAI rep has allowlisted. Format:
  `{"type":"stdio","command":…,"args":["${PLUGIN_ROOT}/…"]}`. [S3, S9]
- **Marketplaces:** `codex plugin marketplace add owner/repo[@ref]` (or a git
  URL / local dir, `--sparse PATH`); repo file
  `.agents/plugins/marketplace.json`, personal
  `~/.agents/plugins/marketplace.json`; entry sources `local`, `url`,
  `git-subdir`, `npm`. Workspace admins can import and sync a GitHub
  marketplace for the team; workspace-published plugins stay out of the
  public directory. [S1, S2]

## 4. What our hosted MCP needs

Checked against live metadata on 2026-10-26.

- **OAuth: mostly ready.** Clerk (`clerk.thelightbulb.company`) advertises
  `client_id_metadata_document_supported: true`, a DCR
  `registration_endpoint`, `S256`, `authorization_response_iss_parameter_supported:
  true`, auth method `none`, and `/oauth/userinfo`. With CIMD + iss, ChatGPT
  uses client id `https://chatgpt.com/oauth/client.json` and redirect
  `https://chatgpt.com/connector_platform_oauth_redirect`. [S12]
  To verify: (a) the token `aud` equals `https://api.thelightbulb.company/mcp`
  when ChatGPT sends `resource=` (our verifier checks audience); (b)
  userinfo returns `email` and `email_verified: true`; (c) Clerk honours the
  `openid email profile` scopes ChatGPT asks for by default.
- **Tool annotations: missing.** `core/services/hosted_mcp.py` registers 9
  tools with bare `@mcp.tool()`. Add explicit `readOnlyHint`,
  `destructiveHint`, `openWorldHint` booleans. Suggested: find_rooms,
  find_sessions, read_messages, find_tasks, read_task, message_receipt →
  read-only; send_message, update_task → `readOnlyHint:false,
  destructiveHint:false, openWorldHint:false`; use_browser →
  `readOnlyHint:false`, review destructive. [S4, S6]
- **Per-tool `securitySchemes`** (`oauth2` + scopes), and an auth error
  carrying `_meta["mcp/www_authenticate"]` to trigger account linking. [S12]
- **Domain challenge route:** `/.well-known/openai-apps-challenge` on
  `api.thelightbulb.company`, declared before `app.mount("/", hosted_mcp.mount)`
  in `main.py`.
- **Responses:** strip internal ids/timestamps not needed by the question
  (reviewers flag them); keep `request_id` since it is the retry key. [S4]
- **Widgets/CSP:** not needed. Only required if we return UI. Skip.
- **Rate limits:** no documented number; just do not 5xx under reviewer
  load.
- **Review account:** a Clerk password user (no email code, no MFA) in a
  workspace with seeded rooms, sessions and tasks. Today Clerk sign-in may
  default to email codes: check.
- **Copy rules:** listing and tool text must not link to Stripe checkout or
  show plan prices.

## 5. Precedents

- Submissions opened 2025-12-17 (OpenAI blog); first approvals rolled out in
  early 2026. Early named partners: Expedia, Spotify, Zillow, Canva. [S13, S14]
- The Codex plugin directory launched in March 2026 and added 90+ plugins
  (Atlassian Rovo, CircleCI, CodeRabbit, GitLab, Render, …) by April 2026.
  **(third-party)** [S15]
- No public data on approval time for Claude-converted plugins. General
  reports: two weeks to four months. [S11]

## 6. Recommendation

**(a) Owner-only testing this week, no review:**
1. On chatgpt.com/plugins → "+" → "Add custom MCP server" with
   `https://api.thelightbulb.company/mcp`, OAuth. Then `@Lightbulb` in a chat
   and in Work. [S16] (Plan gating of custom servers is unclear; the
   owner's plan must allow it.)
2. In the desktop app the Claude Code plugin already shows. It already points
   at the hosted HTTP MCP, so it is the same server.
3. For the stdio engine (`lightbulb` CLI): add it under the desktop app's MCPs
   tab, or a personal marketplace
   (`codex plugin marketplace add TheLightbulbCompany/cli`). Local only.

**(b) Public listing, smallest path:** one "With MCP" plugin on the hosted
server plus our skills, no hooks. Work: tool annotations, challenge route,
verify aud/email_verified, review account, listing copy and icon, privacy and
terms URLs (exist on thelightbulb.company), business verification of the
OpenAI org as "Isol8 Inc., doing business as Lightbulb AI" (legal name per
CLAUDE.md). Countries: start `["US"]`. Defer the stdio engine: ask an OpenAI
rep to allowlist our org for a desktop-only plugin later.

**Risks**
- Review time is weeks to months and cannot be expedited.
- The MCP origin is locked at approval; `api.thelightbulb.company/mcp` must be
  permanent.
- Reviewer may read `send_message` to other people's sessions or
  `use_browser` as broad or open-world; justify scope clearly.
- "Duplicates built-in capabilities" or "unofficial connector" rejections:
  frame Lightbulb as our own service, not a bridge to Claude.
- No upgrade or pricing links inside the plugin.
- Enterprise users need their admin to enable the plugin.
- Help-center plan details were not readable directly; confirm in the portal.

## Sources

- [S1] Package your plugin — https://developers.openai.com/plugins/build/plugins (no date on page; read 2026-10-26)
- [S2] Plugins (ChatGPT/Codex docs) — https://learn.chatgpt.com/docs/plugins (redirect from developers.openai.com/codex/plugins)
- [S3] Submit plugins — https://developers.openai.com/apps-sdk/deploy/submission (also /plugins/deploy/submission)
- [S4] App submission guidelines — https://developers.openai.com/apps-sdk/app-submission-guidelines
- [S5] Submit your Claude Code plugin to OpenAI — https://developers.openai.com/plugins/guides/submit-claude-plugin.md
- [S6] MCP server review requirements — https://developers.openai.com/plugins/deploy/app-review.md
- [S7] Admin controls for plugins and apps — https://help.openai.com/en/articles/11509118 (403; via search snippet)
- [S8] Plugin controls (Enterprise) — https://learn.chatgpt.com/docs/enterprise/apps-and-connectors
- [S9] Desktop-only plugins — https://developers.openai.com/plugins/deploy/desktop-only
- [S10] App Directory → Plugin Directory rename, July 2026 (third-party): https://www.taskade.com/blog/chatgpt-plugins , https://en.wikipedia.org/wiki/ChatGPT
- [S11] Community review-time threads (Apr–Sep 2026): https://community.openai.com/t/app-review-process-timelines-for-chatgpt-app-store/1378947 , https://community.openai.com/t/i-submitted-an-app-how-long-should-the-review-process-take/1369797 , https://community.openai.com/t/chatgpt-app-review-pending-for-6-weeks-is-this-normal/1388077
- [S12] Apps SDK authentication — https://developers.openai.com/apps-sdk/build/auth
- [S13] Developers can now submit apps to ChatGPT (2025-12-17) — https://openai.com/index/developers-can-now-submit-apps-to-chatgpt/
- [S14] TechCrunch, 2025-12-18 — https://techcrunch.com/2025/12/18/chatgpt-launches-an-app-store-lets-developers-know-its-open-for-business
- [S15] Codex plugins coverage (third-party): https://thenewstack.io/openais-codex-gets-plugins/ , https://zuplo.com/blog/openai-codex-mcp-plugins-api-teams
- [S16] Connect from ChatGPT — https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- Live checks 2026-10-26: https://clerk.thelightbulb.company/.well-known/oauth-authorization-server , https://api.thelightbulb.company/.well-known/oauth-protected-resource/mcp
