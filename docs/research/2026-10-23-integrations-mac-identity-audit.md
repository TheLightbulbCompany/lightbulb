# Integrations, agent tooling, Mac app, AgentCore Identity, Google: audit (2026-10-23)

Read-only audit. Parts 1-3 are the detailed reports, with file:line evidence. This page holds the decisions.

## Decisions for the owner

1. **Google.** Recommended mix:
   - Verify ONE Lightbulb Google client for sensitive scopes only: `gmail.send`, `calendar`, and `drive.file` with the Picker. This needs no CASA.
   - Offer the restricted scopes (`gmail.modify`, `drive`) to Workspace customers whose admin marks our client id as Trusted. For that domain, this skips verification and the 100-user cap.
   - Keep bring-your-own-app for power users.
   - Pay for CASA (about $0.5-5k a year plus 1-2 engineer-weeks) only when demand appears.
   - Do not use a shared-app OAuth proxy. Nylas, Composio and Pipedream would put their name on the consent screen, put a third-party company (a subprocessor) in the path of customer mail, and replace gog and our watch channels.
   - Google's official Workspace MCP servers are still a developer preview, and they still need our own client.
   - Today Google is bring-your-own-app only (`providers.yaml:607 byo_required: true`). Owners' apps sit in Testing mode, so refresh tokens die after 7 days.
2. **AgentCore Identity: skip for now.**
   - It does not provide a shared Google client, so it changes nothing about Google verification.
   - A credential provider holds one OAuth client, with a default quota of 50 per account per region. That does not fit one bring-your-own Google client per workspace.
   - It returns access tokens only, never refresh tokens. That breaks gog and xurl over an 8-hour turn.
   - Tokens can't be imported or exported, so moving in or out later means every user reconnects.
   - It could be revisited later as a vault for platform-owned OAuth apps that our code calls directly.
3. **Mac app: opening it beyond one owner.**
   - Signing, notarization and the updater all work (v0.4.910).
   - The real gate is `DESKTOP_EXECUTION_OWNER_PROD` holding one org (`service-stack.ts:1471,1517`). There is no "all" mode, so opening it needs code plus your decision.
   - Still outstanding: every row in the staged-acceptance runbook is pending, the WKWebView RAM growth hasn't been measured, the openclaw-host sidecar needs a Rust port of Shell/Files/MCP, and the build is arm64 only.

## Fix list (code; no decision needed)

1. **Copy CLI credentials into new agents.** An agent created after a connect never gets gh/aws/gog/xurl credentials. `hermes_cli.project` runs once, at connect, over the roster at that moment (`hermes_cli.py:264`). Also, the copy starts from `agent_ids[0]` (the first agent in the roster), which can wipe an earlier account.
2. **Make `reconnect_required` visible.** Show it in the UI, create a Needs-you item, reconcile on the status change, and pass the bridge the list of unavailable providers. Today an expired connection reads "Not connected" and the dead MCP entry stays in the profile.
3. **Delete Slack, Telegram and Discord from the catalog and the skill.** They are dead but still offered to agents. Also delete the Slack code, the orphan `oauth-clients/google` secret, and the attio_api/apollo_api rows that nothing reads.
4. **Fix the Google BYO setup copy.** Tell owners to choose Internal or publish to production, and to update redirect URIs registered before the domain move.
5. **Stop the shared `GOG_KEYRING_PASSWORD` reaching tenant shells.** It is one value for the whole fleet.

## Owner or external actions

- Microsoft: get Entra publisher verification.
- Meta: App Review for both apps, with an org-based reviewer account (the current one is a `user_` id, which the workspace check blocks).
- GitHub: rename the OAuth App from "Isol8" (not verified).
- Re-run the agent-network end-to-end matrix after #4294.
- Clerk DCR is still off.

---

# Part 1: Integrations and agent tools
# Lightbulb integrations audit — how a Hermes agent gets tools and credentials

Read-only audit, 2026-10-23, repo at `96f6e9f6a` (branch `codex/lantern-font`, same code as `main` for these paths).
Paths are relative to `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync`. "UNVERIFIED" = inferred, not proven in code or AWS.
Secrets Manager was read with `list-secrets` (names only). No secret values were read, and no DynamoDB rows.

**Out of scope (another agent owns these), listed only:** `stripe` (remote MCP via DCR), `company_stripe` (pasted restricted key, backend-only), Stripe Projects rail (`projects/{provider}`), `quickbooks` (platform Intuit app → stdio MCP), BYO LLM keys / ChatGPT OAuth / openrouter-gate, Metronome, Apple IAP.

---

## 0. The delivery rails (how anything reaches a runtime)

| Rail | Where it is | What the agent sees |
|---|---|---|
| **A. CLI projection** | `credentials/hermes_cli.py` `project()` (`:264`). It runs the real materializer into a temp dir and uploads the files into **every roster profile** under `home/` (`OWNED_PATHS`/`ROOT_DIR`, `:74-89`). Shell values go into `cli-env.json` (`:101`, `:104-112`) | A logged-in binary in the shell (`gog`, `gh`, `aws`, `xurl`) or a seeded skill dir (`skills/instagram|shopify|vercel`). No tools in the tool list |
| **B. Brokered remote MCP** | `registry.McpRemote.server_block(brokered=True)` writes `Authorization: Bearer ${MCP_<KEY>_ACCESS_TOKEN}` into `config.yaml` (`registry.py:87-101`). On every turn the bridge calls `POST /api/v1/runtime/credentials/u/{owner}` (`bridge/turn.py:1375-1399`, `routers/runtime_rpc.py:30-77`). The backend refreshes the token from Secrets Manager (`credentials/access_token.py:23-64`) | MCP tools in the tool list. Also, **every** brokered bearer goes into the shell env (`turn.py:2655`) |
| **C. Native Hermes MCP OAuth** | `server_block(brokered=False)` → `auth: oauth`, plus a one-time seeded `mcp-tokens/<server>.json` (`hermes_profiles.py:1471-1510`). Hermes refreshes the token in place | MCP tools. Used only where `refresh_owner_for()` != backend: today `meta_ads`, and every DCR provider in `local` |
| **D. Stdio MCP with a file credential** | `mcp_stdio` + materializer (`mcp_server.py:98-103`). Only QuickBooks | MCP tools |
| **E. Profile `.env` + config patch** | `apply_connection.HERMES_OPENCLAW_PATHS_PROVIDERS` (`:45-52`). Only `parallel` → `PARALLEL_API_KEY` + `web.backend: parallel` | Hermes native `web` tool |
| **F. backend_only** | `Wiring.backend_only`. The credential is kept only in Secrets Manager (`apply_connection.py:189-192`) | Nothing. **Exception:** `microsoft365` is backend_only **and** broker-vended as `$MCP_MICROSOFT365_ACCESS_TOKEN` (`runtime_rpc.py:60`) |
| **G. Manual MCP servers** | `routers/integrations.py`. Stored as plain dict on the `manual_mcp` integrations row (`integrations_repo.py:305-323`), merged into every profile (`mcp_server.py:72`) | MCP tools |
| **H. Platform-held, not user-connected** | Agent mail (SES), website-login vault (`routers/website_logins.py`), AgentCore Browser v2, Mac agent Chrome, Recall/LiveKit meetings, Photon text, Paperclip (`PAPERCLIP_API_KEY` in profile `.env`, `hermes_profiles.py:439`), hosted Lightbulb MCP `/mcp` (Clerk OAuth, inbound to us) | Plugin tools / skill scripts |

The availability gate is `apply_connection.runtime_supported()` (`:74-90`) plus `registry.available_for()` (`registry.py:605-615`). The `GET /connections` list and the public catalog `GET /platform-info` (`routers/platform_info.py:52-62`) both read them.

---

## 1. Inventory

Columns: agent mechanism | how the credential reaches the runtime | user auth | where the user sets it up | refresh/expiry | status.
Every setup is in **Settings → Connections** (`/settings?panel=connections`, `components/control/panels/ConnectionsPanel.tsx` → `settings/ConnectionsList.tsx` / `ConnectButton.tsx`) unless the row says otherwise. Mobile has **no** Connections surface: `apps/mobile/src/app/(app)/control/integrations.tsx:91,270` is a read-only list of MCP servers.

| Provider (yaml line) | Agent mechanism | Credential → runtime | User auth | Refresh / expiry | Status |
|---|---|---|---|---|---|
| **Google Workspace** `google` (`providers.yaml:597`) | `gog` CLI baked in the image (`Dockerfile:28,94`). Also Calendar/Drive/Gmail push automations (`google_watch.py`) | Rail A: `home/.config/gogcli` + `home/.local/share/gogcli` keyring (JWE). `GOG_KEYRING_PASSWORD`/`BACKEND` in `cli-env.json` (`hermes_cli.py:105`). Per-agent `GOG_ACCOUNT` (`google_identity.py:31`) | OAuth through the **owner's own Google Cloud app** (`byo_required: true`, `:607`). The owner pastes client id/secret (`PUT /connections/google/byo-app`, `connections.py:986`) | `gog` refreshes in the VM (`refresh_owner: actor`). No secret is kept in SM. Google refresh tokens do not rotate, so there is no write-back (`bridge/sync.py:113-122`) | Live, BYO only. Per-agent account picker: `GoogleAccountsSection` (`ConnectionsPanel.tsx:59`), `routers/google_accounts.py` |
| **GitHub** `github` (`:890`) | `gh` CLI (`Dockerfile:109`). Also Paperclip `GITHUB_TOKEN` company secret for PR/issue lookups (`paperclip_github_token.py`) | Rail A: `home/.config/gh/hosts.yml`. **And** a copy in SM (`lightbulb/prod/integrations/<owner>/github`, 4 in prod), which is synced into Paperclip | OAuth through the platform GitHub OAuth App (`platform_app: github`, `platform-oauth/github`) | OAuth App tokens do not expire (`refresh: none`) | Live |
| **AWS** `aws` (`:1956`) | `aws` CLI v2 (`Dockerfile:97`) with `credential_process` → `lightbulb-creds.mjs` | Rail A: `home/.aws/{config,lightbulb-creds.mjs,token}`. The CLI fetches 1 h STS creds from `POST /connections/aws/credentials` (`connections.py:472-549`) | CloudFormation quick-create of the `lightbulb-connector` role in the owner's account (`aws_cfn.py`, `GET /{provider}/aws-setup`, `POST /aws-complete`). UI: `AwsConnectCard`/`AwsChangeAccessCard` | The helper re-fetches on demand. Tiers: ViewOnly/ReadOnly/PowerUser | Live (3 prod secrets) |
| **X** `x` (`:1917`) | `xurl` CLI (`Dockerfile:116`) | Rail A: `home/.xurl` (the `lightbulb-x` app entry) | OAuth through the **owner's own X developer app** (`byo_required: true`), confidential client, Basic auth (`:1944`) | `xurl` refreshes in the VM. The single-use refresh token is written back to every profile (`sync.py:123-127`) | Live, BYO only (1 prod `oauth-app/x`) |
| **Instagram** `instagram` (`:2160`) | Seeded skill `skills/instagram/` (curl + `token.sh`) | Rail A: `credentials.json` + `token.sh` (`instagram_cli.py`) | OAuth through the platform Meta app `meta-instagram` (`1049347507456011`) + long-lived exchange | `token.sh` extends the 60-day token in place, with write-back (`sync.py:126`) | **Flagged off in prod**: `environments: [local, dev]` + `owner_allowlist` (`:2165-2185`) |
| **Meta Ads** `meta_ads` (`:2063`) | Meta-hosted remote MCP `https://mcp.facebook.com/ads` | Rail C (native OAuth, token file seeded from SM) | OAuth through the platform Meta app `meta-ads` (`2080424579211211`), `fb_exchange_token` | **No refresh token at any tier.** About 60 days, then a silent 401. No expiry is tracked | **Flagged off in prod**: `environments: [local, dev]` + allowlist |
| **Shopify** `shopify` (`:2251`) | Seeded skill `skills/shopify/` (Admin GraphQL via curl+jq, `token.sh` mints a 24 h client-credentials token) | Rail A: `credentials.json` (shop, client_id, client_secret) | Paste 3 fields (store, Dev Dashboard client id, secret). Validated by a live grant (`connections.py:401-425`) | Minted per session. No stored long-lived token | Live (shipped 2026-09-23, `d625436ce`). **The memory `shopify-integration-dead-on-new-stores-2026-09-16.md` is stale**: its container-login/#2714 path was replaced by this |
| **Vercel** `vercel_cli` (`:1574`) | Seeded skill `skills/vercel/` (`npx vercel@54.18.2`) | Rail A: `skills/vercel/SKILL.md` + `VERCEL_TOKEN` in `cli-env.json` (`hermes_cli.py:107-111`) | Pasted account token, validated on `/v2/user` | None (token lives until revoked) | Live. `skill_cli.py:7-16` still says "NOT DISPATCHED TODAY", which is **false** now (`OWNED_PATHS` has `skill_cli`, `hermes_cli.py:80`) |
| **Parallel** `parallel` (`:1239`) | Hermes native `web` search tool | Rail E: profile `.env` `PARALLEL_API_KEY` + `web.backend: parallel` | Pasted API key | None | Live |
| **Notion** `notion` (`:706`) | Notion-hosted remote MCP | Rail B (brokered in dev/prod) | OAuth through provider DCR (per-owner client `oauth-app/notion`) | Backend refreshes in `access_token.get` | Live |
| **Linear** `linear` (`:800`) | Remote MCP | Rail B | DCR | Backend | Live |
| **Sentry** `sentry` (`:731`) | Remote MCP | Rail B (`refresh_owner: backend` is explicit) | DCR | Backend | Live (`environments: [dev, prod]`) |
| **Attio (for your agents)** `attio` (`:1101`) | Remote MCP | Rail B | DCR | Backend | Live |
| **Attio (dashboard access)** `attio_api` (`:2301`) | None | Rail F (SM only) | Pasted API key | None | **Hidden in the UI** (`ConnectionsList.tsx:42`) but **listed** in `/platform-info`. No reader exists (the skill says so, `integrations.md:72-78`) |
| **Apollo (for your agents)** `apollo` (`:2013`) | Remote MCP | Rail B | DCR | Backend | Live |
| **Apollo (dashboard access)** `apollo_api` (`:2321`) | None | Rail F | Pasted key | None | Hidden in the UI, listed in the catalog, no reader |
| **Intercom** `intercom` (`:1214`) | Remote MCP | Rail B | DCR. Intercom's DCR has a redirect-URI allowlist (`:1211-1213`) | Backend | Live. UNVERIFIED whether the post-2026-10-02 callback `api.thelightbulb.company/...` is on Intercom's allowlist |
| **PostHog** `posthog` (`:1258`) | Remote MCP (`?mode=cli`) | Rail B. The secret is also kept for backend HogQL (`retain_secret`) | DCR, **~250 read+write scopes** (`:1280`) | Backend | Live |
| **Cloudflare** `cloudflare` (`:2043`) | Remote MCP | Rail B | DCR, `scopes: []` | Backend | Live |
| **Microsoft 365** `microsoft365` (`:2412`) | **No tools.** The agent runs `curl` against Graph with `$MCP_MICROSOFT365_ACCESS_TOKEN` (skill `integrations.md:42-63`) | Rail F + broker vend (`runtime_rpc.py:60`). The access token lives about 1 h | OAuth through the platform multi-tenant Entra app (`platform-oauth/microsoft365`), read-only | Backend refreshes. Microsoft rotates refresh tokens, so the backend is the only holder | Live in prod (memory `m365-graph-integration-2026-09-22.md`) |
| **Slack** (not in `providers.yaml`; `routers/slack_app.py`, `slack_install_service.py`) | **Nothing.** Events are forwarded to `/slack/events/{team}` on the runtime (`slack_app.py:213-220`), and the Hermes bridge has no such route | Bot token in SM (`integrations/<owner>/slack/<team>`, 1 in prod) | Platform Slack app OAuth install (`/api/v1/slack/oauth/start`) | n/a | **Dead.** The runtime half was removed (`slack_install_service.py:1-14`). No frontend entry point exists: `lib/slackManifest.ts` is referenced only by its test, and the return URL targets the removed `agent-inspector` Channels tab (`slack_app.py:251-254`). The app's URLs are still on the old domain (memory, domain move 10-02) |
| **Telegram / Discord** | Nothing | Nothing | Nothing | n/a | **Dead.** They exist only as names in `CHANNEL_PROVIDER_IDS` (`core/constants.py:18`), which `/platform-info` still advertises as connectable channels (`platform_info.py:62`) |
| **Manual MCP servers** | Any MCP server (stdio or remote) | Rail G. Each `env`/`headers` value is moved into the profile `.env` (`hermes_profiles.py:549-596`) | Org admin types the command/URL + secrets | None | Live. UI: Settings → MCP (`control/panels/McpServersTab.tsx`). Secrets are stored in the DynamoDB row, not in SM, and `GET /integrations/mcp/servers` returns them to admins (`integrations.py:226-256`) |
| **`m365` CLI** (`@pnp/cli-microsoft365`, `Dockerfile:121`) | A baked binary with its own device-code login | None | Device code, run by the agent | n/a | Baked but deliberately unused. The skill tells the agent not to use it (`integrations.md:61-63`) |

Agent-side capabilities that need no user connection: plugin tools `send_room_message`, `request_approval`, `suggest_idea`, `hire_suggestion`, `lightbulb_local_sessions`, `lightbulb_read_rooms`, `lightbulb_voice_profile`, `lightbulb_desktop` (`plugins/lightbulb/__init__.py:710-726`); skill scripts `lightbulb-api.sh`, `-connect.sh`, `-mail.sh`, `-post.sh`; agent-browser (AgentCore browser), terminal and web. Disabled toolsets: `cronjob, tts, video_gen, clarify` (`hermes_profiles.py:168`). Small drift: `plugin.yaml` `provides_tools` does not list `lightbulb_voice_profile`.

---

## 2. Overlaps and inconsistencies

**Dead or advertised-but-broken surfaces**
1. **Slack, Telegram and Discord are advertised to agents but do not work.** `/platform-info` lists all three as channels (`platform_info.py:62`). The skill says "Slack: one click from the agent's Channels tab (Add to Slack); Telegram/Discord: the bot wizard" (`skills-src/lightbulb/references/integrations.md:6-7`). No Channels tab exists, there is no Telegram/Discord code, and Slack events are forwarded to a bridge route that does not exist. Dead code still in place: `routers/slack_app.py` (374 lines), `slack_install_service.py`, `slack_signing.py`, `channel_link_repo`, `frontend/src/lib/slackManifest.ts` + test, `platform-oauth/slack` secrets, and 1 prod bot token.
2. **`attio_api` / `apollo_api`**: kept as backend-only "dashboard access" keys that no reader uses (the yaml comment, `:2274-2299`, admits this). They are hidden in the UI but present in the public catalog, so the skill has to explain the duplicate.
3. **Orphan Secrets Manager entries**: `lightbulb/{dev,prod}/oauth-clients/google` is not referenced anywhere in the repo (`git grep oauth-clients` finds nothing; only purged docs used it). The domain-move memory calls it the "Isol8 Integrations (prod)" connector client, but `google` is BYO-only in code, so that client is unused. The dev per-owner `oauth-app/{attio,notion,stripe}` entries are DCR caches (fine).

**Same provider on two rails / credentials in two places**
4. **GitHub token in two places**: profile `hosts.yml` (rail A) and the SM secret mirrored into Paperclip's `GITHUB_TOKEN` (`paperclip_github_token.py`). This is intended and kept in sync, but it is a second custodian.
5. **Microsoft: Graph broker token vs the baked `m365` CLI.** The skill has to forbid the CLI. Removing the CLI from the image would remove that rule.
6. **Google: managed `gog` vs Hermes's bundled generic Google Workspace skill** (its `setup.py --check` gives a false "disconnected", `integrations.md:30-35`). There are two Google paths in the image. UNVERIFIED which upstream skills the base image ships.
7. **Attio and Apollo each have two rows** (MCP OAuth + pasted REST key). See item 2.
8. **Manual MCP secrets are in DynamoDB; provider secrets are in Secrets Manager.** That is two custodians for the same kind of credential (Rail G).

**OAuth on one surface, API key on another**
9. Attio/Apollo: OAuth (agent) + key (backend). Stripe: MCP OAuth + pasted restricted key (out of scope). Shopify: pasted client credentials, and Shopify has no OAuth row.
10. **Microsoft 365 reads `actor: skill` but has no skill.** The facts manifest reports its rail as `skill` (`owner_facts.py:286-287`), and the UI badges it as a skill. The agent actually uses curl + an env var that is documented only in `integrations.md`.

**UI vs runtime projection gaps**
11. **Agents added after a CLI connect get no CLI credentials.** `hermes_cli.project` fans out only to the roster that exists at connect time (`hermes_cli.py:258-273`). No code copies `home/.config/gh`, `.aws`, `gogcli`, `.xurl`, `skills/instagram|shopify|vercel` or `cli-env.json` into a profile created later. I searched every `write_profile_subtree`/`fetch_profile_subtree` caller. So a marketplace hire or new agent has no `gh`/`aws`/`gog`/`xurl` until the owner reconnects, while Connections shows "Connected". MCP rails do not have this gap, because `write_profiles` merges `build_connected_integration_servers` (`hermes_profiles.py:783`). Verified by absence in code, not tested at runtime.
12. **Related data-loss risk:** `_run_and_sync` seeds the read-modify-write from `agent_ids[0]` (`hermes_cli.py:198-200`) and assumes all profiles are identical. If that profile was created after the first connect (item 11), connecting a second Google account (or another X app) seeds from an empty keyring and overwrites every profile, dropping the earlier accounts. UNVERIFIED: it depends on roster order.
13. **`reconnect_required` is not visible to people.** `access_token._mark_reconnect` (`access_token.py:16-20`) changes the row and rewrites facts, but: the UI has no state for it (it renders as "Not connected", `ConnectionsList.tsx:115-162`); no Activity/Needs-you item is created; there is no email; the bridge drops the `unavailable` list (`turn.py:1391-1399` reads only `tokens`); and `config.yaml` still holds the MCP block with an empty bearer until something triggers `reconcile_integrations`, so the server fails to start and its tools disappear silently.
14. **Actor-refresh grants that die in the VM never update the row.** gog (Google Testing-mode 7-day expiry, see §3), xurl, QuickBooks and Meta Ads (60 days, no refresh) keep showing "Connected".
15. **Vercel skill on a warm VM:** `skills/vercel/` is not in the bridge's `CLI_CREDENTIAL_PREFIXES` (`bridge/sync.py:98-111`, though `skills/instagram/` and `skills/shopify/` are). A connect reaches a warm microVM's `cli-env.json` (`VERCEL_TOKEN`), but probably not the SKILL.md until the next cold restore. UNVERIFIED.
16. **Stale paths in skill text and docstrings (harmless today).** The skill source says `~/.openclaw/lightbulb-facts.json` (`SKILL.md:54,120`, 8 references). It is safe only because `build-skills.sh:36` rewrites it. It also says `$ISOL8_API_URL` (still set, per CLAUDE.md). Many materializer docstrings still describe EFS (`gog_cli.py:1-16`, `gh_cli.py`, `xurl_cli.py`, `instagram_cli.py:153-159` in the yaml). `owner_posthog.py:12` describes a container-rotated `mcp-tokens` copy, but PostHog is brokered in dev/prod.

**Security-shaped items** (credential handling, so flagged here)
17. **One fleet-wide secret in every tenant's shell.** `GOG_KEYRING_PASSWORD` is a single value for the whole fleet (`config.py:482-488`, SM `lightbulb/{env}/gog-keyring-password`), copied into every Google-connected owner's agent shell via `cli-env.json`. A prompt-injected agent can read it.
18. **The QuickBooks platform `client_secret` is written into every connected owner's profile** (`qbo_dotenv.py` docstring, "narrow Intuit-specific exception"). Out of scope for detail; noted only.
19. **Every brokered MCP bearer is exported to shell children** (`turn.py:2655` → `_allow_child_env_names`). This is required for Microsoft 365 curl, but it also gives the shell the Notion/Linear/Attio/PostHog/… access tokens. One prompt injection could exfiltrate all of them.

---

## 3. Testing mode / unverified apps

Platform OAuth apps in Secrets Manager (both envs): `integrations/platform-oauth/{github, meta-ads, meta-instagram, microsoft365, quickbooks, slack}`. Plus `gog-keyring-password`, `oauth-clients/google` (orphan), `recall_*`, `photon_*`.
Per-owner prod secrets: `aws`×3, `github`×4, `instagram`, `meta_ads`, `microsoft365`, `posthog`, `apollo`, `stripe`×2, `company_stripe`, `slack/<team>`, and DCR/BYO app caches `oauth-app/{apollo×4, attio×4, cloudflare×2, google×2, linear, notion×2, posthog×5, sentry, stripe×6, x}`. Note: no prod SM secret exists for any brokered `notion/linear/attio/intercom/cloudflare/sentry` grant. Either nobody has connected them under the broker yet, or older rows are stuck. UNVERIFIED (I did not read DynamoDB).

| Provider | Whose app | Requested scopes | Verification / mode (evidence) |
|---|---|---|---|
| **Google** | **The owner's own** GCP app (`byo_required`, `providers.yaml:604-607`, UI `ConnectButton.tsx:228-300`) | `gmail.modify` — **restricted**; `drive` (full) — **restricted**; `calendar` — **sensitive** (`:613`), plus `access_type=offline, prompt=consent` | Lightbulb's own scopes are never verified (so no CASA). But the UI tells External owners to "add your own Google account under Test users" (`ConnectButton.tsx:279-283`). That leaves the owner's app in **Testing**: 100 test users, the unverified-app screen, and (Google policy, not in the repo) **refresh tokens that expire after 7 days**. The UI and the skill never mention this, so most non-Workspace owners will see `gog` stop working every week while the row reads Connected. Internal (Workspace) apps avoid it. To ship a shared Google app, both restricted scopes would need verification plus an annual CASA assessment. The GCP project `isol8-485204` branding was "verified" on 2026-10-02 (memory), but that is brand verification for the Clerk sign-in client, not scope verification. **Redirect break:** owners whose BYO client registered `api[-dev].flickmeon.ai/.../oauth/google/callback` before the 10-02 domain move cannot reconnect until they edit their own client (the memory fixed only the founder org's app; 2 prod `oauth-app/google` exist) |
| **GitHub** | Platform OAuth Apps "Isol8" / "Isol8 (Dev)" (memory) | `repo, read:org, gist, workflow` (`:902`): full private-repo write | No verification process. Consent screen still shows the **"Isol8"** app name (rebrand residue, UNVERIFIED that it was renamed). Customer orgs with OAuth App access restrictions must approve the app. One callback per app (switched 10-02) |
| **Microsoft 365** | Platform multi-tenant Entra apps (prod `17e4c1b5…`, dev `6dcd8346…`) | `offline_access openid profile email User.Read Mail.Read Calendars.Read Files.Read.All` (`:2421`) | **Publisher not verified** (memory 09-22: "prod still needs publisher verification"; 10-02: "Entra publisher domain unverified"). Since Nov 2020, users in most work/school tenants **cannot self-consent to an unverified multi-tenant app**, so they need an admin. Personal MSA accounts work |
| **Meta (Ads + Instagram)** | Platform Meta app `2080424579211211` (Ads), IG app `1049347507456011` | Ads: `ads_mcp_management, ads_read, ads_management, business_management, catalog_management, pages_show_list, pages_manage_metadata, instagram_basic` (`:2083`). IG: `instagram_business_basic, _content_publish, _manage_comments, _manage_messages` (`:2193`) | **Development mode, no Advanced Access / App Review** (`:1995-1999`, `:2165-2174`). Only accounts with an app role can connect. Env-gated to dev plus `owner_allowlist` (founder org + one `user_` "App Review login"). **The `user_3HmnX…` allowlist entry is now unusable**: personal workspaces are fenced (`resolve_owner_id` 409 `workspace_required`), so a Meta reviewer signed in with it cannot reach Connections. Re-submitting for App Review needs an org-based reviewer account first |
| **X** | **The owner's own** X developer app (`byo_required`, `:1922-1923`) | `tweet.read/write, users.read, offline.access, dm.read/write, follows.read/write, like.read/write, media.write` (`:1928`) | No Lightbulb app. What works depends on the owner's X API plan (UI admits this). The same redirect-URI break applies to BYO apps registered before 10-02 (1 prod) |
| **Slack** | Platform app (`platform-oauth/slack`) | 23 bot scopes (`slack_install_service.py:37-60`) | Distribution / App Directory status UNVERIFIED. Moot: the runtime is dead and its URLs are on the old domain (memory 10-02: "leave until asked") |
| **DCR remote MCPs** (Notion, Linear, Sentry, Attio, Intercom, PostHog, Apollo, Cloudflare) | Per-owner public PKCE client registered by DCR (`oauth_dance.py:77-112`). No app to verify | Notion `offline_access`; Linear `read`; Sentry `org:read`; Attio `openid offline_access mcp`; Intercom `offline_access`; Apollo 9 read/search scopes (`:2024`); Cloudflare none; PostHog ~250 incl. `*:write` (`:1280`) | No verification exists. The redirect is re-registered automatically on mismatch (`oauth_dance.py:181-187`). Intercom's allowlist is the one manual exception (UNVERIFIED after the domain move). The provider's consent screen shows the DCR `client_name`. UNVERIFIED that it says "Lightbulb" and not "Isol8" |
| **ChatGPT** (inference, mostly out of scope) | **OpenAI's own Codex CLI public client** `app_EMoamEEZ73f0CkXaXp7hrann` (`oauth_service.py:3-4`) | Codex device auth | Not our app. OpenAI can change or block it (it already moved endpoints once, ~2026-04) |
| **AWS** | CFN quick-create; our account is the trust principal + per-connection ExternalId | ViewOnly / ReadOnly / PowerUser managed policies (`:1956`) | No verification concept |
| **Shopify / Vercel / Parallel** | Owner's own key / Dev Dashboard app | n/a | No platform app |

---

## 4. Agent experience

**How the agent learns what it has**
- `$HERMES_HOME/lightbulb-facts.json` `connected_integrations: [{provider, rail}]`, names only (`owner_facts.py:263-292`). It is rewritten on every connect/disconnect (`connections.py:606,759,881,949,1232`) and on `reconnect_required`.
- The skill `references/integrations.md` explains the three rails (`mcp` = tools, `cli` = shell binary, `skill` = bundled skill), names specific Google and Microsoft quirks, and tells the agent to read the live catalog `GET $ISOL8_API_URL/api/v1/platform-info` and never recite from memory.
- MCP tools appear in the tool list after the `.revision` re-pull. CLI and skill rails never appear as tools (by design; the skill says so).
- The seeded `skills/instagram|shopify|vercel/SKILL.md` describe themselves.

**When a connection is missing**
- The skill tells the agent to send the owner to Connections, or to post `<app_url>/chat?panel=connections&provider=<slug>`, which the room renders as a Connect button (`messaging/appLinks.ts:52`). On request the agent can mint the link with `lightbulb-connect.sh link <provider>` (documented exit codes 409/400/404/503/502/401). BYO and requestable providers are described from the catalog flags.
- **Gaps:** (a) The catalog still offers Slack/Telegram/Discord (§2.1), so the agent may pitch them. (b) No first-class "connect X" Needs-you item exists. The marketplace deploy gate returns `missing_credentials` (`marketplace_listings.py:1062`), but an agent mid-task can only write prose.

**When a connection is expired or broken**
- Brokered MCP and Microsoft 365: the backend marks `reconnect_required` and drops the provider from the facts. But the bridge ignores `unavailable`, the MCP server just fails to start, and the skill tells the agent that a missing tool means "check Connections". There, the row reads "Not connected" with no reconnect cue (§2.13). Microsoft 365: an empty `$MCP_MICROSOFT365_ACCESS_TOKEN`, and the skill says what that means.
- Actor-refresh CLIs (gog, xurl, Instagram, QuickBooks) and Meta Ads: the failure appears only as a CLI or MCP error. The row stays Connected and nobody is told. For Google the skill tells the agent to quote API-enablement errors, but it says nothing about Testing-mode token expiry.
- New agents (§2.11): `gh`/`aws`/`gog` are simply unauthenticated. The skill tells the agent to check `gh auth status`, which reports "not logged in", and the facts file says Connected. The agent cannot settle this contradiction.

---

## 5. Top reconciliation recommendations

1. **Delete or hide the dead channels.** Remove `telegram`/`discord`/`slack` from `CHANNEL_PROVIDER_IDS`/`/platform-info` and the skill's Channels line. Then delete (or explicitly park) `slack_app.py`, `slack_install_service.py`, `slack_signing.py`, `lib/slackManifest.ts`, the orphan `oauth-clients/google` secret and the `attio_api`/`apollo_api` rows. Deletion is smaller than keeping explanations.
2. **Project CLI credentials into every new profile.** When a profile is created (provision, marketplace deploy, agent create), copy the owned `hermes_cli` subtrees + `cli-env.json` from an existing profile, or re-run `project()` from SM for backend-custodian providers. Seed `_run_and_sync` from the profile that actually holds the credential, not `agent_ids[0]`. This closes §2.11 and §2.12 together.
3. **Make `reconnect_required` visible.** Give it a UI state ("Reconnect"), create one Activity/Needs-you item, run `reconcile_integrations` on the status flip so the dead MCP block is removed, and have the bridge pass `unavailable` into the turn context so the agent can name it. Add the same signal for in-VM refresh failures (gog/xurl/Meta) through a small bridge → backend report.
4. **Fix the Google BYO path or replace it.** At minimum, tell owners to set the consent screen to **Internal** (Workspace) or to **publish to production** (unverified is OK for their own use), so refresh tokens do not expire in 7 days. Tell BYO Google/X owners to update their redirect URI to `api.thelightbulb.company`. Long term, decide whether to fund restricted-scope verification + CASA for one shared app (and drop the unused `oauth-clients/google` either way).
5. **Finish the platform-app verification queue.** Order: Entra **publisher verification** (unblocks work/school self-consent; Microsoft 365 is already live in prod), then Meta App Review with an **org-based** reviewer account (the `user_` allowlist entry can no longer pass the workspace fence), then rename the GitHub OAuth App from "Isol8". Also consider removing the fleet-shared `GOG_KEYRING_PASSWORD` from tenant shells (a per-owner keyring password) and limiting which brokered bearers reach shell children.

---
# Part 2: Mac app blockers
# Mac (Tauri) desktop blockers, 2026-10-23

## A. Verified in code / GitHub / AWS vars
| blocker | evidence | blocked on | next step |
|---|---|---|---|
| Selected-Mac execution is single-owner, not GA | `gh variable list`: only DESKTOP_EXECUTION_OWNER_PROD=org_3H3viGAfq2SXc9dIdcatmGXg8Td (set 2026-09-21); no _DEV var. service-stack.ts:1471,1517-1518 (one id => enabled+allowlist; empty => off); app.ts:197-198 | owner decision + code (allowlist is one string, no "all" mode) | Add a GA mode (flag or wildcard) in service-stack.ts; owner OKs; run staged-acceptance matrix first |
| Staged acceptance evidence table is all "pending" | docs/runbooks/desktop-execution-staged-acceptance.md (release identity table; 2/4-task routing, >30s shell, cancel/unknown, file/MCP, sleep/wake) | code + owner (real-Mac runs) | Run dev then prod matrix, fill table; stop-expansion rule if any gate unmeasured |
| RAM/WKWebView growth (+466 MB/101 s) unresolved | runbook "Desktop RAM gate": #2821/#2820 did not measure savings; 0.4.150 baseline | code (measurement) | Do the same-Mac before/after (ps rss + footprint at 0/30/60/101 s) on v0.4.910 |
| openclaw-host sidecar still required | docs/research/2026-10-22-openclaw-host-retirement.md: it is the `node-host` node serving Shell/Files/local MCP; prod has 2 connected; vendor-sidecars.sh runs `npm install openclaw@2026.8.1`. #4290 (merged) pruned ~545 MB | code (Rust port of system.run/file/dir/mcp as 2nd NodeClient, clientId node-host) | Port per its removal plan, contract tests vs backend `_shell`/desktop_resources, then delete subprocess.rs/proxy.rs |
| Chrome MCP existing-session attach unavailable | runbook (use_browser section) says stays unavailable; Mac browser = own "Lightbulb agent" Chrome only | by design / owner | No action unless owner wants it |
| arm64-only DMG, no Intel/Windows | desktop-build.yml: `targets: aarch64-apple-darwin`, `bundle.targets=["app"]`, "CI builds aarch64 only" (CLAUDE.md); p2p design spec: Windows "later", Linux after core split step 8 | owner decision + code | Decide if Intel needed; add x86_64 target + vendor-sidecars x86_64 |
| Self-hosted single Mac runner, 150-min job, triple retry on notarize/upload | desktop-build.yml (runs-on macos-desktop, timeout 150, tries 1-3, notarytool) | external (Apple notary flakiness / one laptop) | Monitor; no code blocker found. Last release desktop-v0.4.910 (2026-10-22) signed+notarized OK |
| Pre-cutover installs cannot self-update | desktop-build.yml:126-130 comment | accepted | none |
| Open GitHub issues/PRs for desktop | none found (issue search for desktop/mac/tauri: only unrelated #2059; PRs: #3791 sessions-terminal docs, not desktop-specific) | n/a | n/a |
| Core crate split in progress | commits #4289 (3/9), #4290, #4294 on main; plan docs/superpowers/plans/2026-10-21-lightbulb-core-crate-split.md (untracked) | code | finish remaining steps (Linux = step 8) |

## B. Only from memory/docs (not re-verified)
| item | source | blocked on | next step |
|---|---|---|---|
| Codex delivery FAIL on v0.4.876 | agent-network-shipped-2026-10-20 | FIXED in #4269 (v0.4.884), verified applied | none; still open: Codex 0.163 rejects plugin hooks.json (`unknown field modules`) |
| Open agent-network bugs 10-22 (v0.4.905): Grok hooks need trusted folder; Cursor per-tool asks (pre-allow `Mcp(lightbulb:*)`); hook receipt "applied" while still in inbox; Codex session view blank; RING-5 label mismatch; `local-session-hook grok` under Claude breaks MCP (check ancestor exe) | agent-network-cross-harness-2026-10-21 | code (#4294 claims fixes for Cursor allow, hooked receipts, hook identity, resume, Codex blank pane; re-run e2e pending per goal plan line 12-13) | Re-run 10-22 e2e matrix on latest build |
| Clerk DCR off (hosted MCP uses CIMD) | cross-harness memory | owner/external (AWS SSO expired; Clerk dashboard) | Owner enables DCR |
| Real Grok Bot test | same | owner | owner sets up routine/webhook |
| Mac browser sign-in sharing gaps (wipe on unpair, offline revocation, pair key-possession DoS) | mac-browser-signin-sharing-2026-09-28 (largely superseded by raw-CDP train #3412+; wipe now documented in runbook) | code | verify wipe path in agent_chrome.rs |
| Anthropic terms on driving user's claude headless | coding-in-desktop-app-research | external (Anthropic answer) | do not reopen managed launch |
| Lightbulb Connect (Rust engine) replaces/subsumes app CLI; "delete the Lightbulb app" test; `lightbulb` CLI takeover (v0.2.2) | lightbulb-network-p2p-rulings | owner decision/in flight; Track C reverted, design parked | await owner approval of AgentCore-hosted member design |
| macOS Local Network privacy blocks mDNS for ad-hoc-signed binaries | p2p rulings F2 | Apple (signing) | sign/notarize connect binary stably |
| Desktop not tested on dev (owner policy) | desktop-app-not-tested-on-dev | owner ruling | staged runbook's "dev" leg conflicts; owner to reconcile |

---
# Part 3: AgentCore Identity and Google without CASA
# AgentCore Identity and Google-without-CASA: research report

Date: 2026-10-23. Sources were read on that date. Each claim has a citation, and the uncertain ones are marked **[unverified]** or **[vendor claim]**.

---

## 0. What we run today (read from the code)

- **Google is already BYO-only.** `apps/backend/core/services/credentials/providers.yaml` (`google:`) sets `byo_required: true` with the comment "no Isol8-managed Google app. Every owner connects with their own OAuth app and owns its Limited-Use compliance." The scopes are `gmail.modify`, `calendar` and `drive`. `gmail.modify` and `drive` are **restricted** and `calendar` is **sensitive** (see §B0). So today the CASA burden falls on each owner. Lightbulb does not carry it.
- **Token custody for Google is the gog keyring inside the profile.** The provider has `refresh_owner: actor`. `routers/connections.py::_retains_secret` skips Secrets Manager for actor-refreshed providers. `materializers/gog_cli.py` runs the real `gog` binary to write a password-derived JWE keyring that holds the **refresh token**. Inside the runtime, gog refreshes the token itself. One keyring holds every Google account the owner has connected, selected with `-a <account>`.
- **Backend-refreshed providers** go through `credentials/access_token.py`. It reads Secrets Manager under an owner-lifecycle lease, refreshes the token and marks the connection `reconnect_required` when the refresh fails.
- **Grants are workspace-keyed, not human-keyed.** The owner is the Clerk org, and `hermes_cli.project` writes the credential into every roster profile.
- **Workload identity today.** We do not use AgentCore Identity at all. `service-stack.ts` (`AgentCoreWorkloadIdentityManage`) grants Create/Get/Delete/Tag on `workload-identity-directory/default/*` only because `CreateAgentRuntime` creates a workload identity for each runtime under the caller's identity, and the create fails with AccessDenied without that grant. No credential provider, token vault or `GetResourceOauth2Token` call exists anywhere in the repo.

---

## A. Amazon Bedrock AgentCore Identity

### A1. What it offers

| Capability | Facts | Source |
|---|---|---|
| Workload / agent identity | An agent identity is a workload identity in a directory. AgentCore Runtime and Gateway create one automatically, named after the runtime or gateway id. | [terminology](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.md), [session binding](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/oauth2-authorization-url-session-binding.html) |
| OAuth2 credential providers (outbound) | Built-in vendors: Cognito, Auth0, Atlassian, CyberArk, Dropbox, Facebook, FusionAuth, GitHub, **Google**, HubSpot, LinkedIn, **Microsoft**, Notion, Okta, OneLogin, Ping, Reddit, Salesforce, **Slack**, Spotify, Twitch, **X**, Yandex, Zoom. Anything else uses `CustomOauth2` with a discovery URL. | [identity-idps](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idps.html), [gateway outbound](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html) |
| **You supply the client id and secret** | The Google provider setup has you create *your own* Google OAuth client and paste its client id and secret into `CreateOauth2CredentialProvider`. You must add `bedrock-agentcore.<region>.amazonaws.com` to the app's Authorized domains. AWS has **no shared or AWS-verified Google client**. | [identity-idp-google](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-google.md) |
| Callback URL | Each credential provider gets its **own** callback URL (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>`). It is returned by Create, and you must then register it with the IdP. | same |
| API key credential providers | `CreateApiKeyCredentialProvider` stores **one key per provider**, read back with `GetResourceApiKey`. The key is stored in Secrets Manager under the hood (the secret ARN is returned). | [gateway outbound](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html) |
| Token vault | "credentials can only be accessed by the specific agent and user combination that originally obtained them", which means the vault is keyed on (workload identity, user id). | [terminology](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.md) |
| Flows | `GetResourceOauth2Token.oauth2Flow` takes `USER_FEDERATION` (3LO), `M2M` (2LO) or `ON_BEHALF_OF_TOKEN_EXCHANGE`. The request also takes `scopes`, `customParameters`, `forceAuthentication`, `resourceOauth2ReturnUrl`, `customState` and `sessionUri`. The response returns `accessToken` **or** `authorizationUrl` + `sessionUri`, and `sessionStatus`. **The API never returns a refresh token.** | [API_GetResourceOauth2Token](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_GetResourceOauth2Token.html) |
| `@requires_access_token` | A Python SDK decorator (`bedrock_agentcore.identity.auth`) that wraps the call above and injects `access_token=`. `on_auth_url` takes a callback that receives the consent URL, so you can stream it to the user, store it, or poll for it. | [identity-authentication](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html) |
| Session binding | After consent, AgentCore redirects the browser to **your** HTTPS return URL. That URL must be listed in `allowedResourceOauth2ReturnUrls` on the workload identity. Your page checks that the person logged in is the person who started the flow, then calls `CompleteResourceTokenAuth(sessionUri, userIdentifier={userId or JWT})`. The authorization URL is valid for 10 minutes. AWS recommends `customState` against CSRF. | [session binding](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/oauth2-authorization-url-session-binding.html) |
| Per-user scoping | Inside Runtime, the caller passes `runtimeUserId` (header `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`). That needs the extra IAM action `bedrock-agentcore:InvokeAgentRuntimeForUser`, and AgentCore uses `GetWorkloadAccessTokenForUserId` internally. AWS does **not** verify the header value, so the caller must derive it from an authenticated principal. The other path is JWT inbound auth (`GetWorkloadAccessTokenForJWT`). One AWS page says OAuth integration requires a raw HTTPS invoke instead of the SDK, which conflicts with the botocore reference **[unverified]**. | [runtime-oauth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.md), [botocore invoke_agent_runtime](https://docs.aws.amazon.com/botocore/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime.html) |
| Refresh | Refresh is automatic when the IdP returns a refresh token. For Google, pass `customParameters: {access_type: offline}`. AWS says "Access tokens returned by AgentCore are not guaranteed to be valid": a token revoked at the IdP is not detected, and the fix is `forceAuthentication=true`, which also **clears the stored refresh token**. | [identity-authentication](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html) |
| Gateway | MCP-server and OpenAPI targets support outbound OAuth client-credentials, **authorization-code (3LO)**, token exchange and API key. Lambda targets support IAM only. The gateway's own workload identity reads the vault. A hosted **consent portal** exists for gateways that use JWT inbound auth. | [gateway outbound](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html), [terminology](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-terminology.md) |

### A2. Pricing, regions, limits

- **Price:** $0.010 per 1,000 token or API-key requests for non-AWS resources, and "**No additional charges** … when customers use AgentCore Identity through AgentCore Runtime or AgentCore Gateway". Gateway costs $0.005 per 1k invocations. ([pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)) Cost does not decide this question either way.
- **Regions:** Identity is available in every AgentCore region, us-east-1 included. ([regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html))
- **Quotas** ([limits](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)):
  - workload identities: 11,000
  - **OAuth2 credential providers: 50 per account per region (adjustable)**
  - **API key credential providers: 50 (adjustable)**
  - `GetResourceOauth2Token`: 200 TPS
  - `CompleteResourceTokenAuth`: 100 TPS
  - control-plane create/update: 20 TPS

### A3. Honest fit against our design

**Problem 1: the provider quota does not fit our tenancy.** A credential provider holds **one** OAuth client. Google is BYO per owner, so every owner's Google app would be its own provider, and each would get its own callback URL that the owner must register in their GCP console. One account also hosts all owners. At 50 providers per region, the default quota is spent after roughly 50 Google-connected workspaces, and API keys hit the same ceiling (one key per provider). The quotas are adjustable, but a ticket asking for tens of thousands of providers is a bet on AWS goodwill. Providers where *we* own the client (Instagram, X, GitHub) fit fine, because there the vault's per-user key carries the tenancy.

**Problem 2: CLI tools need a refresh token, and the vault never hands one out.** gog holds a refresh token in a JWE keyring and refreshes itself. `xurl` and `gh` read token files in `$HOME`. The vault returns only short-lived access tokens (about 1 h for Google). The bridge could call `GetResourceOauth2Token` at turn start and write the access token into the profile or env, but:
- a turn can run up to 8 h, longer than the token lives, so long turns fail halfway;
- gog would have to accept a bare access token instead of its keyring. Whether gog can do that is **[unverified]**: I found no gog flag for it, and its keyring model points the other way;
- every connector that refreshes itself (gog, xurl, Instagram's `token.sh`) would have to become "ask the bridge for a fresh token", which means a token-broker helper on loopback in the image. This is buildable, but it is new code, not deletion.

**Problem 3: the user-identity model.** The vault keys on (workload identity, user id), and the runtime's workload identity is per runtime, which for us means per workspace. Workspace-wide grants (what we do today) would use a constant user id such as `owner_id`. Per-human grants would mean passing each human's Clerk id as `runtimeUserId`. But agent turns triggered by heartbeats, routines or room events have no single human behind them, so per-human tokens do not fit "an AI employee working for the company" without a policy for "whose Gmail does Friday use at 3 a.m.". We would also need `InvokeAgentRuntimeForUser` and must never forward a client-supplied value, because AWS does not verify it.

**Problem 4: lock-in and portability.** Refresh tokens cannot be exported, so leaving the vault later means every user reconnects. Today our Secrets Manager blobs and gog keyrings are ours to move.

**What we would delete:** `oauth_dance.py` (authorization URL, code exchange and refresh) and the refresh half of `access_token.py`, for backend-refreshed providers only. We would also drop the token blobs in `integration_secret_store` and possibly the `owner_lifecycle` lease around refresh.

**What we would keep or add:**
- the OAuth app stores (we still own the client ids; providers wrap them)
- `registry`/`providers.yaml`
- a new `/oauth/agentcore/return` page that calls `CompleteResourceTokenAuth` (our callback becomes AWS's callback plus our session-binding page)
- every CLI materializer, rewritten into a fetch-at-turn-start or token-broker shape
- provider lifecycle code (create/update/delete one provider per BYO app)
- `reconnect_required` UX driven by `authorizationUrl` responses

**Google verification:** this changes nothing. AWS provides no shared Google client, so the OAuth app is still ours or the owner's, and the consent screen, Limited Use and CASA obligations stay with whoever owns that client. The vault only moves where the refresh token sits.

### A4. Recommendation for A: **skip for now; consider a narrow "vault only" adoption later**

- Do not migrate Google, or anything else that is BYO per owner. The 50-provider quota and the missing refresh token break it.
- If we want it at all, the only candidate is **platform-owned OAuth apps used by API-calling code, not by CLIs**: one provider per vendor, `user_id = owner_id`. Even there the gain is small. We already have working refresh with a lease, and we would add an AWS-specific session-binding page and lose token portability.
- Re-evaluate if AWS (a) adds a per-tenant client override on a single provider, or (b) lets a workload fetch a refresh token. Neither exists today.

Migration sketch, if ever done for one platform-app provider (for example GitHub):
1. `CreateOauth2CredentialProvider(GithubOauth2, our client)` in CDK, and register its callback URL in the GitHub app.
2. Add `https://api.thelightbulb.company/api/v1/oauth/agentcore/return` to `allowedResourceOauth2ReturnUrls` on each runtime's workload identity, set in `provision_hermes_runtime`.
3. Connect: the backend calls `GetResourceOauth2Token(USER_FEDERATION, user=owner_id, customState=nonce)` and shows `authorizationUrl`. The return page checks the Clerk session and nonce, then calls `CompleteResourceTokenAuth`.
4. Runtime: invoke with `runtimeUserId=owner_id` (plus the IAM action). The bridge fetches the token at turn start and writes `gh`'s hosts file, or serves it from a loopback helper for long turns.
5. Dual-run with the old path, then have users reconnect, because tokens cannot be imported into the vault.

---

## B. Google without CASA

### B0. Scope classification (Google's tables)

- **Gmail** ([Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)):
  - restricted: `mail.google.com`, `gmail.readonly`, `gmail.compose`, `gmail.insert`, `gmail.modify`, `gmail.metadata`, `gmail.settings.*`
  - sensitive: `gmail.send`, `gmail.addons.current.message.readonly/metadata`
  - non-sensitive: `gmail.labels`
- **Drive:** `drive` and `drive.readonly` are restricted. `drive.file` is **non-sensitive** and works with the Google Picker. ([Drive auth](https://developers.google.com/drive/about-auth))
- **Calendar:** `calendar` is sensitive. It needs verification but no CASA.
- Restricted scopes need verification **plus** an annual CASA assessment at assurance level AL1 or AL2, chosen by Google from user count and scopes. "All applications must be revalidated every year", and an app at AL2 stays at AL2. ([Security assessment](https://support.google.com/cloud/answer/13465431))

Our current scope set (`gmail.modify` + `drive`) is therefore fully restricted.

### B1. Vendors that hold their own verified Google app

| Vendor | Shared verified app in production? | Branding | You still do CASA? | Data and token path | MCP | Price signal |
|---|---|---|---|---|---|---|
| **Nylas** | **Yes.** The "Shared GCP App" is "fully verified for all Google scopes that Nylas supports" and passed **Tier 3 CASA**. Nylas handles the yearly re-assessment. | Consent shows **"Nylas"** and cannot be customised. No custom hosted-auth hostname. | No | Nylas owns the client and stores and refreshes tokens. You call the **Nylas API** and get no raw Google token. | Nylas API, not gog | Annual Pro + Accelerator add-on, or Enterprise. Ask sales. ([shared app](https://developer.nylas.com/docs/provider-guides/google/shared-gcp-app/)) |
| **Truto** | **Yes.** CASA **Tier 2**. "customers do not need to undergo any Google verification themselves". | not stated | No | Mostly **read-only** scopes. Gmail is read-only plus modify for labels and read state, so sending mail is not covered by default. | unified API | not stated ([blog](https://truto.one/blog/our-google-oauth-app-is-live-and-casa-tier-2-certified.md)) **[vendor claim]** |
| **Unipile** | **Yes** **[vendor claim]**: a "certified OAuth key" and you "skip Google Cloud setup entirely". Moving to your own client later (BYOC) is supported. | Unipile | No (vendor claim) | Unipile API | — | €49/mo for ≤10 accounts, then about €5 per account ([billing](https://developer.unipile.com/v2.0/docs/billing)) |
| **Composio** | **Partly.** Composio-managed Google app; their blog says "No Google Verification on Your End". Their KB tells you to stay within "scopes already available on the selected Composio auth config" or bring your own verified app. | Composio's. Your own app is needed for your branding. | No for their scopes (vendor claim) | **Tokens are redacted.** The API masks `access_token`, and you call through Composio's proxy or tools. Tokens cannot be imported. | Yes, native MCP and tools | Repriced Aug 2026: Free 100k calls, Pro $29 plus usage **[unverified aggregator]** ([KB](https://docs.composio.dev/kb/guide/platform-google-oauth), [token custody](https://docs.composio.dev/docs/security/token-custody.md)) |
| **Pipedream Connect** | **Yes, with a fixed scope set.** The managed Gmail client requests `gmail.labels/send/modify/compose/settings.basic`; for other scopes you supply your own client. | Pipedream's | Not stated for the managed client | "you store no tokens". MCP or proxy calls carry the user's credentials on Pipedream's side. | Yes (Connect MCP) | Connect about $99/mo plus $2 per external user beyond 100 **[aggregator]** ([Gmail app](https://pipedream.com/apps/gmail)) |
| **Arcade.dev** | The default Google provider is "shared across all Arcade users" and has Gmail read, compose, send and modify. Docs say "in a production environment, you will most likely want to use your own". | Arcade's | Unclear | Arcade engine | Yes | — ([google provider](https://docs.arcade.dev/en/references/auth-providers/google.md)) |
| **Nango** | **No.** "Production use still needs your own credentials." Nango publishes a CASA prep guide. | yours | **Yes** | You can get the raw tokens | MCP via their tooling | — ([security review](https://nango.dev/docs/api-integrations/google-shared/google-security-review)) |
| **Klavis** | **No.** "only supports using your own custom Google OAuth application" | yours | Yes | Klavis-hosted MCP | Yes | — ([Klavis Gmail](https://docs.klavis.ai/knowledge-base/oauth_app/gmail)) |
| **Paragon** | No evidence of a shared app. The setup asks for your client id and secret. | yours | Yes | — | ActionKit | — ([Paragon Gmail](https://docs.useparagon.com/resources/integrations/gmail)) |
| **Merge, Unified.to** | Not confirmed in any source I could reach. | — | **[unverified]** | — | — | — |

Cross-cutting risks of a shared app:
1. **Concentration risk.** If Google suspends the vendor's project, every customer loses access at once (noted in the [Nylas forum](https://forums.nylas.com/t/google-verification-requirement-with-nylas-hosted-oauth-for-restricted-scopes/855) thread).
2. **Branding.** The consent screen names the vendor, not Lightbulb. Google requires brand information that "accurately represent[s] the identity of the application" ([OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)). The vendor is the app of record, which is why this model is accepted, but users will see "Nylas wants to access your Gmail".
3. **Data path.** Mail content and tokens pass through the vendor, which becomes a subprocessor for customer mail. That needs DPA and privacy-policy updates.
4. **No raw token.** None of the shared-app vendors hands out a Google refresh token, and Composio redacts even the access token. So **gog cannot be used**: the agent would call the vendor's tools or MCP instead of `gog`. That breaks our Gmail and Drive automations (`gog gmail watch`, Calendar `events.watch` with our `X-Goog-Channel-Token` flow) unless the vendor offers equivalent webhooks.

### B2. Sensitive and non-sensitive scopes only (no CASA, verification still required)

The no-CASA scope set is `gmail.send` + `calendar` + `drive.file` (with the Picker) + `gmail.labels`.

What we keep:
- sending mail as the user
- full calendar management
- creating or editing files the agent made, or files a human picked in the Picker

What we lose:
- **reading the inbox**: no triage, replies in thread, search or `gmail.poll` / `gmail.message` triggers. Reading needs `gmail.readonly`/`metadata`/`modify`, all restricted. `gmail.compose` (drafts) is also restricted.
- browsing or searching the whole Drive, and the `drive.changed` watch across the drive

Agents already have their own mailboxes (`{agent}@{slug}.thelightbulb.company`) for inbound mail, and humans can **forward** or CC mail to them. That covers much of the "read my mail" need without any Google scope. Sensitive-scope verification is a brand and privacy review only, with no paid assessor.

### B3. Workspace admin paths (verification not needed at all)

Google's "[When is verification not needed](https://support.google.com/cloud/answer/13464323)" lists:
- **Internal** apps (same Workspace org; does not fit us as a multi-tenant SaaS)
- **"An administrator can add a third-party app to the list of trusted apps in the Google Admin console"**
- **Admin-installed** unverified Marketplace apps, where access goes to every user the admin installed it for
- personal use under 100 users
- testing mode (100-user cap; the Nango guide says refresh tokens expire after 7 days)

The [Admin help page](https://knowledge.workspace.google.com/admin/apps/authorize-unverified-third-party-apps) confirms that trusting an app lets it reach otherwise restricted scopes for that domain. One point is still open: whether trusted-domain users are excluded from the 100-user cap. Only Nylas says so, which is **[vendor claim]**; Google's text says only that the restrictions do not apply to trusted apps.

Implication:
- **For Workspace customers (our construction-GC beachhead is mostly Workspace or M365), one Lightbulb Google client that stays unverified for restricted scopes, plus a "have your admin trust client id X" onboarding step, avoids CASA for those domains.**
- Consumer `@gmail.com` users and non-admin employees cannot use this path. They see the unverified-app screen and count toward the 100-user cap.
- Domain-wide delegation (a service account) is the other admin path. It is powerful but admin-only and impersonates users; Google notes DWD clients cannot use the "AI agent" client option for the MCP servers.
- Today's BYO model is effectively "internal app per owner", which also avoids CASA but costs each owner a GCP console setup.

### B4. Google's official Workspace MCP servers

- They are real: Gmail, Drive, Calendar, Chat and People remote MCP servers (for example `calendarmcp.googleapis.com/mcp/v1`, `drivemcp.googleapis.com/mcp/v1`). The **developer preview** started 2026-05-01 and still requires membership in the Workspace Developer Preview Program. ([Workspace blog](https://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html), [configure guide](https://developers.google.com/workspace/guides/configure-mcp-servers), [supported products](https://docs.cloud.google.com/mcp/supported-products))
- **Auth is your own OAuth client** ("Use this client for an AI-powered agent") with normal scopes such as `gmail.readonly` and `drive.readonly`. So the MCP servers **do not remove verification or CASA**. They only replace the API surface (they could replace gog), and they are preview-only. Google is also adding usage-quota tiers for new projects.

### B5. CASA baseline

- Google states the AL1/AL2 levels and annual revalidation. ([assessment](https://support.google.com/cloud/answer/13465431))
- Public prices are all vendor or reseller figures **[vendor claim]**:
  - TAC Security Tier 2 about $540–$900 (Basic/Premium), Enterprise about $3,600
  - Tier 3 about $4,500
  - TAC says assessment to Letter of Validation takes 1–3 weeks ([TAC](https://tacsecurity.com/google-casa-cloud-application-security-assessment/), [switchlabs](https://www.switchlabs.dev/post/casa-tier-2-tier-3-security-review-providers-pricing-and-the-cheapest-option))
- Google's own verification adds time: Nango says 5–20 days with preparation, and other sources say several weeks.
- A realistic budget is **$0.5k–$5k per year plus about 1–2 engineer-weeks the first year**: a privacy-policy and Limited Use page, a demo video, and SAST/DAST fixes. Ongoing cost is the annual re-scan.

### B6. Recommendation for B

1. **Keep BYO as the CASA-free default for power users**, as today.
2. **Add one Lightbulb-owned Google client, verified for sensitive scopes only** (`gmail.send`, `calendar`, `drive.file` + Picker). This needs no CASA, and it gives one-click connect to everyone, consumer Gmail included, for send, schedule and agent-created docs. Route "read my mail" through forwarding to the agent's own mailbox.
3. **On the same client, offer restricted scopes (`gmail.modify`, `drive`) to Workspace customers through "admin trusts our client id"**. This is the documented exemption ([support.google.com/cloud/answer/13464323](https://support.google.com/cloud/answer/13464323)), so no CASA is needed for those domains. Keep the restricted-scope request behind a separate incremental-consent connect so non-trusted users never hit the unverified screen.
4. **Do CASA (AL1/Tier 2, about $1k/yr) only when demand shows up** from non-admin or consumer users who need inbox read. It is cheap relative to engineering time, so do not over-engineer around it.
5. **Do not adopt a shared-app vendor** unless we drop gog for Gmail and Drive. Nylas is the most credible: Tier 3 CASA, all scopes. But it brings Nylas branding, a contract add-on, mail transiting a subprocessor, and the loss of our raw-token, gog and watch-channel design. Composio and Pipedream are viable for an MCP-tool-only Gmail path, with the same caveats and opaque scope approval.
6. **Google's Workspace MCP servers** are worth tracking as a gog replacement once they leave preview. They have no bearing on verification.
