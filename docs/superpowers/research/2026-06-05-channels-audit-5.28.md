# Isol8 Channels Audit — OpenClaw 5.28 (2026-06-05)

> Method: 10-agent dynamic workflow. Map (backend / container-config / frontend-UX / upstream-5.28 schema) → Diagnose (Telegram, Slack, WhatsApp) → adversarial Verify (refute the two root-causes) → Synthesize. All claims grounded in file:line read during the run; upstream = local `~/Desktop/openclaw` checkout pinned at 2026.5.28.
>
> **⚠️ CORRECTED 2026-06-06 against live prod CloudWatch (`/isol8/prod/openclaw`, image `2026.5.28-1ad2085`, org `org_3DBS8G83oPrMm49yN2b70EjoFpN`). The static diagnoses in §3.1 (Telegram "config reload skip on a stale field") and §3.2 (Slack "stale manifest is the log error") were BOTH FALSIFIED by the logs. See §0 for the evidence-backed root causes; §3.1/§3.2 are retained only as superseded hypotheses.**

---

## 0. LIVE PROD EVIDENCE — the real root causes (supersedes §3.1 and §3.2)

Pulled from `/isol8/prod/openclaw` + `/ecs/isol8-prod` + the container's on-disk `openclaw.json`. The container was on 5.28 the whole time, so every "legacy/pre-bump" framing in §3 is moot.

**Container config right now** (tokens redacted):
```json
"telegram": { "enabled": true, "dmPolicy": "pairing", "accounts": {} },
"discord":  { "enabled": true, "dmPolicy": "pairing" },
"slack":    { "enabled": true, "dmPolicy": "pairing", "mode": "socket",
              "appToken": "xapp-1… (len 97)", "botToken": "xoxb-8… (len 57)", "accounts": {} }
```

### 0.1 Telegram — bad token (`getMe` 401) + a reload-defer/timeout trap

The co-founder (`user_3DBS55…`, an org admin) made **3 attempts** on 2026-06-04 12:39–12:42. Backend: each `PATCH /api/v1/config → 200` (account `7a466ef7-1445-4627-b4bd-5c69e6cdc6bb` written successfully), followed by a flood of `channels.status` polls, then `DELETE /channels/telegram/7a466ef7…` (the wizard's 60s-timeout auto-rollback). Container:
```
[telegram] [7a466ef7…] Telegram bot token unauthorized (getMe returned 401 from Telegram; source: config token)
[telegram] [7a466ef7…] auto-restart attempt 1/10 … 6/10
[reload] config change requires channel reload (telegram) — deferring until 2 operation(s), 1 embedded run(s) complete
[reload] channel reload still deferred after 30088ms … 60391ms … 273493ms with 2 operation(s), 1 embedded run(s) active
[bundle-mcp] failed to start server "perplexity": MCP server connection timed out after 30000ms
```
**Root cause:** the pasted bot token is **invalid (Telegram `getMe` 401)** — the user needs a valid BotFather token. **Compounding UX bug:** the channel reload is **deferred >60s** because the 0.5-vCPU box is busy (MCP connect-storm + an in-flight embedded run), so the account doesn't even attempt `getMe` until **after** the wizard's 60s timeout has fired and rolled it back. The wizard therefore shows the generic *"Bot started but never reported ready. Check the token"* (`BotSetupWizard.tsx:429`) and **never surfaces the actual 401** — even though the backend learns it ~80s later. A *valid* token can also fail this way whenever the container is busy at setup time.

### 0.2 Slack — orphaned provider-level tokens crash-looping on `invalid_auth`

```
[slack] [default] slack auth.test failed at boot (invalid_auth); … until restart with a valid bot token
[slack] socket mode failed to start due to non-recoverable auth error — skipping channel (invalid_auth)
[slack] [default] auto-restart attempt 7/10 in 300s … 8/10 …
```
**Root cause:** there are **orphaned `channels.slack.botToken`/`appToken` at the provider level** (an old, now-revoked Slack app) with `accounts: {}`. OpenClaw reads provider-level tokens as the **`[default]` account** and crash-loops `auth.test` → `invalid_auth` → auto-restart 1→10 every ~5 min, forever. This is the "Slack logs we never set up." The wizard's `DELETE` only removes `accounts.<id>` — it **never clears provider-level tokens** (`routers/channels.py:251-279`), so a stale provider-level credential lingers indefinitely. The §3.2 "stale manifest" finding is **latent only** — it never engages because the app is dead; it would matter only once a *valid* Slack app is connected.

### 0.3 The systemic bug (this is the fix to build)

All three channels are provisioned `enabled: true` with no accounts (`config.py:793-816`). For Telegram/Discord (no provider-level token) that's harmless-idle. For Slack, leftover provider-level tokens make `enabled:true` an **active crash-loop** that burns CPU on the 0.5-vCPU box — which **worsens the reload-defer that breaks Telegram setup**. Fix: **only enable a channel that has a valid configured account, and strip provider-level tokens on delete.** Plus: surface `getMe`/`auth.test` failures in the wizard instead of a blind 60s timeout, and reconcile the 60s client timeout against OpenClaw's deferred-reload.

## 1. Executive summary

- **Bottom line:** The chat-channel rail is structurally sound under 5.28 — Isol8's config writes are schema-valid and Socket Mode connects — but **two real defects make Slack and Telegram unreliable in the field**, and **WhatsApp is a DEFER**. Neither live bug is a schema crash-loop; both are surfaced-error / stale-asset problems.
- **What works:** The token-channel architecture (token → `PATCH /api/v1/config` → plaintext into `openclaw.json` on EFS → chokidar hot-reload → `channels.status` poll) is correct. Every field Isol8 emits — Telegram `{botToken, dmPolicy:"pairing"}`, Slack `{mode:"socket", appToken, botToken, dmPolicy:"pairing"}`, the route bindings, `session.dmScope`, and the whole `agents.defaults` block — **validates against the 5.28 strict schema**. The DM→pairing-code happy path the wizard actually drives works with the scopes Isol8 grants.
- **Telegram — confirmed mechanism, root field unproven:** The Telegram *write itself* is 100% valid in 5.28 (verified, CONFIRMED). The real risk is **systemic**: Isol8's write path has zero pre-write schema validation, and 5.28 **skips the entire whole-config hot-reload if any field anywhere on the existing EFS config is invalid** (`config-reload.ts:178`). On a legacy/stale container, the Telegram PATCH is merely the trigger that surfaces a *pre-existing* invalid field → wizard hangs 60s → false "never reported ready." The exact stale field needs a live CloudWatch line to name.
- **Slack — confirmed stale manifest, corrected error story:** Isol8's hardcoded `SLACK_APP_MANIFEST` grants **6 bot scopes / 2 events**; 5.28 requires **23 scopes / 15 events** plus `app_home` + `assistant_view` features (the original diagnosis undercounted — corrected). This is a genuine latent defect. **But the verification refuted the "logged error" claim:** the two assistant-thread `missing_scope` failures the diagnosis leaned on hardest are written via `logVerbose` and are **silent by default**; only App Home `views.publish` (`home.ts:66`) logs via `runtime.error`. All three are **user-action-triggered, not connect-triggered**. Whether *this* container's log actually shows `missing_scope` is unverified — pulling the live line is mandatory.
- **WhatsApp — DEFER:** 5.28 WhatsApp is QR/Baileys (unofficial WhatsApp Web), not a token Cloud API. No token means the entire existing wizard rail can't reach it; the auth is a churning multi-file Baileys session on EFS (the exact NFS small-files hazard that already crash-loops the founders' box); and it carries real ToS/ban risk on an RC-pinned library. ~1.5–2.5 weeks of net-new work vs ~1 day for a 4th token channel.
- **Do first:** (1) Ship the corrected Slack manifest (frontend-only, S). (2) Add a fail-fast post-PATCH validation/probe so a reload-skip surfaces as an actionable 422 instead of a 60s blind hang. Both before touching WhatsApp.

---

## 2. How channels work today (current-state map)

### 2.1 User UX (per provider)

All three providers share one wizard — `apps/frontend/src/components/channels/BotSetupWizard.tsx:218`, driven by `PROVIDER_STEPS` (`:212-216`). It mounts on three surfaces (control-panel Agents→Channels tab `AgentChannelsSection.tsx:40`; Settings→Channels `MyChannelsSection.tsx:37` — **link-only**, never create; onboarding `ProvisioningStepper.tsx:613`) plus a read-only `ChannelsStatusPanel.tsx:53`.

- **Telegram** (`PROVIDER_STEPS.telegram`, `:213` — `intro→token→connecting→pair→done`): user creates a bot via `@BotFather` `/newbot`, pastes the HTTP API token (one input, validated `^\d+:[A-Za-z0-9_-]{30,}$` at `:81-97`), then DMs the bot for an 8-char pairing code and enters it. Shortest path.
- **Discord** (`:214` — adds a `discord-intents` step): create app in the Developer Portal, reset+copy the bot token (no real format validation — `:94-96`), **manually enable Message Content Intent** (`:557-561` — without it pairing silently never works), generate an OAuth2 invite URL (`bot` scope), invite to a server, then pair.
- **Slack** (`:215` — `slack-manifest→token→connecting→pair→done`, **no intro**): the hardest. User pastes Isol8's `SLACK_APP_MANIFEST` (`:55-75`) into "Create app from manifest," installs to workspace, generates an **app-level `xapp-` token** with `connections:write` added manually (`:618-626`), copies the **bot `xoxb-` token** (`:627-631`). Two inputs, both prefix-validated only (`xoxb-` at `:89-93`, `xapp-` at `:99-106`), then pair.

**Inputs by provider** are the only per-provider difference the user sees in the token step; field-name asymmetry (below) is invisible to them.

### 2.2 Backend control-plane (token-submit path)

The token does **not** go through `routers/channels.py`, any gateway `setSecret` RPC, or the integrations registry. The wizard's `submitTokenAndConnect` (`BotSetupWizard.tsx:296-441`) builds an `openclaw.json` patch and ships it via **`PATCH /api/v1/config`** (`routers/config.py:135-165`, mount `main.py:650`):

1. `require_org_admin(auth)` (`config.py:140`) — orgs must be admin; personal passes.
2. `_patch_touches_channels` (`:149`) → **subscription gate**: 403 `channels_require_subscription` if billing not active/trialing (`:150-155`).
3. `_check_token_collision` (`:50-96`) → 409 `token_already_assigned_to_other_agent` — **only checks `botToken`**, so Discord's `token` and Slack's `appToken` are not collision-checked (`:70`).
4. `patch_openclaw_config(owner_id, body.patch)` (`:161`) → deep-merge onto EFS; `ConfigPatchError` → 404.

`routers/channels.py` (mount `main.py:647`) handles only **member↔bot identity links** — `/links/me`, `link/{provider}/complete` (pairing code → `allowFrom` append + DDB row), self-unlink, and admin delete-bot. It never touches the token.

The only gateway RPC the wizard calls is **`channels.status`** (read-only poll over WS) to detect when the hot-reloaded plugin reports the new account `running`/`connected` (`BotSetupWizard.tsx:399-428`).

**DDB vs secret-store split:** the `channel-links` table (`channel_link_repo.py`, PK `owner_id`+`sk`, GSI `by-member`) stores **member↔bot identity links only — no tokens**. Tokens live nowhere but EFS (see 2.3).

### 2.3 Container config (the `openclaw.json` channels block + where secrets live)

**At provision** (`core/containers/config.py:793-816`), Isol8 writes an idle, account-less scaffold — identical for all three: `{"enabled": true, "dmPolicy": "pairing"}` — plus the adjacent `session.dmScope: "per-account-channel-peer"` (`:817-819`). No accounts, no tokens, no `allowFrom`. The comment block (`:783-792`) explains why all three ship `enabled:true`: it loads the plugins at boot so later token adds are a fast per-channel hot reload (seconds), not a ~6-min gateway restart. `webchat` is deliberately omitted (`:794-803`) because upstream rejects unknown channel ids.

**At setup** the per-account object is PATCHed in (`BotSetupWizard.tsx:362-378`), with **per-provider field-name asymmetry**:
- Telegram: `{ botToken, dmPolicy:"pairing" }`
- Discord: `{ token, dmPolicy:"pairing" }` — `token`, **not** `botToken`
- Slack: `{ mode:"socket", appToken, botToken, dmPolicy:"pairing" }`

**Where secrets actually live (definitive):** channel bot tokens are stored as **plaintext JSON inside `openclaw.json` on EFS** at `channels.<provider>.accounts.<agent_id>.{botToken|token|appToken}`. There is **no** `${OPENCLAW_...}` placeholder, **no** ECS task-def secret, **no** Secrets Manager entry, **no** env var — unlike LLM keys and the hook token, which use ECS-secret indirection (`config.py:519-528`, `ecs_manager.py:1169-1170`). The "plaintext MUST NEVER land" invariant is **not enforced for channel tokens**; their only protection is the `require_org_admin` read-gate on `GET /config` (`config.py:119`). The write is atomic + NFS-safe (`config_patcher.py:37-100`: `fcntl.lockf`, `.bak`, `mkstemp`+`os.rename`, `chown 1000` if root), and auto-adds a top-level `bindings[]` route entry (`:103-138`). **Reprovision data-loss gap:** a full reprovision rewrites `openclaw.json` wholesale (`config.py:1007`) and re-injects channels account-less — there is no channel re-injection (unlike `integration_mcp_servers` at `:893-936`), so any live channel account/token/`allowFrom` is wiped.

### Provider matrix

| Provider | Tokens required | Setup steps | Secret storage | Live-status surface |
|---|---|---|---|---|
| **Telegram** | 1 — `botToken` (HTTP API token) | BotFather `/newbot` → paste token → DM-pair | Plaintext `channels.telegram.accounts.<id>.botToken` in `openclaw.json` on EFS | `channels.status` → `channelAccounts.telegram[].{running,connected,lastError}` |
| **Discord** | 1 — `token` (field named `token`, not `botToken`) | Create app → reset token → **enable Message Content Intent** → OAuth2 invite → DM-pair | Plaintext `channels.discord.accounts.<id>.token` on EFS | same |
| **Slack** | 2 — `botToken` (`xoxb-`) + `appToken` (`xapp-`, needs `connections:write`) | Paste manifest → install → 2 separate token locations → DM-pair | Plaintext `channels.slack.accounts.<id>.{botToken,appToken,mode:"socket"}` on EFS | same |

---

## 3. Root-cause diagnoses

### 3.1 Telegram "can't set up" — final root cause (post-verification)

**Resolved answer (corrected from the initial "Telegram-block schema drift" premise):** There is **no schema drift in the Telegram write**. The verification CONFIRMED that every field Isol8 emits — the account `{botToken, dmPolicy:"pairing"}`, the `{type:"route", agentId, match:{channel, accountId}}` binding, `session.dmScope`, and the full `agents.defaults` block — validates against 5.28's strict schemas (`zod-schema.providers-core.ts:265-266`; `zod-schema.agents.ts:51-59`; `zod-schema.agent-defaults.ts:116,123,219,221,236,272`). On a **freshly reprovisioned 5.28 container, the Telegram path works.** That hypothesis is falsified by code.

**The real root cause is systemic, not Telegram-specific:**
- **Symptom:** wizard enters the token, advances to "Connecting…", spins a full **60 seconds**, then dies with *"Bot started but never reported ready. Check the token and try again."* (`BotSetupWizard.tsx:429`) + auto-`rollbackAccount()`. Retrying reproduces the identical hang. The token is never the problem; the user re-checks it in vain.
- **The exact drift (Isol8 emits X / 5.28 expects Y):** Isol8's write path has **no pre-write schema validation** (`config_patcher.py:217-238`). 5.28 **re-validates the entire config on every reload and skips the reload wholesale if *any* field is invalid** — `config-reload.ts:173-180`: `config reload skipped (invalid config): <issue>` → keeps old in-memory config, never applies the Telegram patch. So a **stale field that predates the 5.28 bump** (sitting latent on a legacy container's EFS config) is surfaced by the Telegram PATCH, which is simply the next reload. Highest-probability stale fields are 5.28's retired Telegram keys per `extensions/telegram/src/doctor-contract.ts:102-132` — `channels.telegram.dm`, `direct.*.threadReplies`, `groupMentionsOnly`, or scalar legacy `streaming` aliases — but the field could also be non-Telegram (a retired `browser.profiles.*`/`tools.*` key).
- **Minimal fix:**
  - *Immediate (operational, no deploy):* reprovision the co-founder's container so EFS `openclaw.json` is rewritten clean (current `config.py` output is 5.28-valid). Re-run the wizard → it reaches the `pair` step in seconds.
  - *Permanent (code):* in `apps/backend/routers/config.py` (the `PATCH /config` handler, ~`:158-161`), after `patch_openclaw_config`, do one server-side `channels.status` probe (pattern already at `channels.py:90-97`) and, if the new `accounts.<agent_id>` is absent or `lastError` indicates a reload skip, raise `HTTPException(422, detail={"error":"config_reload_rejected","field":<issue>})`. This converts the 60s blind hang into "Telegram setup failed: `<field>` is no longer valid — your container needs reprovisioning."
- **Confidence:** HIGH that the Telegram write is valid and the mechanism (reload-skip on a stale field) is real and field-agnostic. MEDIUM that a stale field is actually present on *this* container — that is unproven statically and needs the live log (§6).

**Ranked alternatives** if the live log doesn't show a reload-skip: (S1) bad/expired token → `getMe returned 401`, wizard shows *"Bot failed to start: getMe returned 401"* within **seconds**, not a 60s timeout — the most common *mundane* cause. (S3) subscription/admin gate → immediate 403 toast, no spinner. (S4) container crash-looping → every poll throws, 60s timeout, health dot red and chat also broken.

### 3.2 Slack "log issue" — final root cause (post-verification)

**Resolved answer (core mechanism CONFIRMED; the diagnosis's headline "what's in the log" was REFUTED and is corrected here):**

- **Confirmed defect — stale manifest:** Isol8's hardcoded `SLACK_APP_MANIFEST` (`BotSetupWizard.tsx:55-75`) grants **6 bot scopes** (`app_mentions:read, chat:write, im:history, im:read, im:write, users:read`) and **2 bot events** (`app_mention, message.im`). 5.28's `buildSlackManifest` (`extensions/slack/src/setup-shared.ts:10-105`) declares **23 bot scopes and 15 bot events** plus the `app_home` and `assistant_view` features. (The original diagnosis said "19 scopes / 13 missing" — **corrected to 23 scopes / 17 missing** per the verification re-count.) Isol8's set is **below even the doc "Minimal" variant**. This is a genuine latent defect: any Slack operation needing an ungranted scope (App Home publish, assistant threads, channel/reaction/pin/file ops) returns `missing_scope`.
- **Confirmed falsifications (schema theories ruled out):** The config Isol8 writes is **schema-valid** — `SlackAccountSchema` is `.strict()` but accepts `mode`(954)/`botToken`(973)/`appToken`(974)/`dmPolicy`(1020) (`zod-schema.providers-core.ts:951-1031`); `"pairing"` triggers no `allowFrom` requirement. **Signing-secret theory falsified** — `signingSecret` is required only in `mode:"http"` (`zod-schema.secret-input-validation.ts:79-101`); Isol8 writes `mode:"socket"`. **Field names match** the secret registry exactly (`secret-contract.ts:13-99`). Socket Mode connects (the wizard correctly adds `connections:write` separately, `:623`, and sets `socket_mode_enabled:true`, `:75`), so the wizard's success-poll passes while runtime operations error.
- **Likely log error string + the verification correction:** The diagnosis claimed the headline log error is `missing_scope` "logged as an error" via `runtime.error?.(...)` on assistant `setStatus`/`setSuggestedPrompts`. **The verification REFUTED this.** Those two paths write via `logVerbose` (`context.ts:459,489`), which returns early and emits **nothing** unless verbose/debug logging is on (`globals.ts:10-23`) — silent by default. Of the three "primary" emitters, **only App Home `views.publish` logs via `runtime.error?.(danger(...))`** (`home.ts:66`). Furthermore all three are **user-action-triggered** (`app_home_opened` when a member opens the App Home tab; `assistant_thread_started` on assistant-thread use) — **not** connect-triggered. So the corrected statement is: *the stale manifest will produce `missing_scope` on App Home / assistant / channel / reaction / pin / file operations, but the only one that appears in default (non-verbose) logs is App Home `views.publish`, and it only fires if someone actually opens App Home.* The DM→pairing happy path the wizard drives is **fully scoped and does not fail** (`chat:write` + `im:*` are granted).
- **Must we pull the real CloudWatch line?** **Yes — mandatory.** Whether any `missing_scope` line is in *this* container's log is unverified and depends entirely on whether a member exercised App Home / assistant threads. An equally plausible, far more log-visible alternative the diagnosis under-weighted is a **token-pairing error** (`slack token mismatch` / `auth.test failed` at `provider.ts:316,325-327`) — these *do* use `runtime.error`/`runtime.log`. Grep `/isol8/<env>/openclaw` for `missing_scope` (confirms manifest gap, and `needed:` names the first scope hit), `views.publish` (confirms App Home), and `slack token mismatch`/`invalid_auth` (redirects to a token problem).
- **Minimal fix (frontend-only, no schema change):** replace `SLACK_APP_MANIFEST` at `BotSetupWizard.tsx:55-75` with the 5.28 canonical manifest from `extensions/slack/src/setup-shared.ts:10-105` — adding the `app_home` + `assistant_view` features, the `/openclaw` slash command, the 17 missing scopes (incl. `assistant:write, channels:history, channels:read, commands, groups:*, mpim:*, pins:*, reactions:*, files:*, emoji:read, usergroups:read`), and the 13 missing events. Keep `socket_mode_enabled:true` and the separate `connections:write` instruction. Confirm by re-running the wizard with the new manifest (user re-creates/updates the app, reinstalls, re-pastes tokens) and verifying `missing_scope` lines stop and App Home / assistant threads render.
- **Confidence:** HIGH that the manifest is stale and the schema/signing-secret/field-name falsifications are correct. HIGH that the diagnosis mis-stated the logging path. The exact in-log error remains **unproven without the live line.**

### 3.3 Config/secret schema-drift table

| Field / Key | Old shape (pre-bump) | 5.28 shape | Isol8 currently emits | OK? |
|---|---|---|---|---|
| `channels.<p>.accounts.<id>.botToken` (TG/Slack) | `botToken` | `botToken` (SecretInput) | `botToken` | ✅ valid |
| `channels.discord.accounts.<id>.token` | `token` | `token` (NOT `botToken`) | `token` | ✅ valid (correct asymmetry) |
| `channels.slack.accounts.<id>.appToken` | `appToken` | `appToken` (Socket Mode) | `appToken` | ✅ valid |
| `channels.slack.…mode` | `"socket"` | `"socket"`\|`"http"`, default `"socket"` | `"socket"` | ✅ valid |
| `channels.slack.…signingSecret` | optional | required **only** if `mode:"http"` | not emitted | ✅ N/A in socket mode |
| `dmPolicy` (top-level + per-account) | `"pairing"` | enum `pairing\|allowlist\|open\|disabled`, default `pairing` | `"pairing"` | ✅ valid (no `allowFrom` needed) |
| `session.dmScope` | `"per-account-channel-peer"` | valid scope (`zod-schema.session.ts:36`) | same | ✅ valid |
| Binding `{type:"route",agentId,match:{channel,accountId}}` | route shape | matches `RouteBindingSchema` exactly | same | ✅ valid |
| `agents.defaults.{contextLimits,memorySearch,heartbeat,verboseDefault}` | — | accepted by strict `AgentDefaultsSchema` (`:272`) | same | ✅ valid (CONFIRMED) |
| **Slack manifest scopes/events** | 6 scopes / 2 events sufficed | **23 scopes / 15 events** + `app_home` + `assistant_view` | **6 scopes / 2 events** | ❌ **STALE — undergranted (live bug 3.2)** |
| **Retired Telegram keys** `dm`, `direct.*.threadReplies`, `groupMentionsOnly`, scalar `streaming` aliases | valid | **REMOVED**; strict schema rejects as unknown keys | not emitted by current `config.py` | ⚠️ not emitted, but **may persist on legacy EFS configs → reload-skip (live bug 3.1)** |
| `embeddedHarness` | valid (≤5.12) | removed (#85341), strict-rejected | not emitted (grep-clean) | ✅ N/A for current builder |

---

## 4. WhatsApp feasibility

**Auth model in 5.28:** **QR-multidevice via Baileys (unofficial WhatsApp Web)** — NOT a Cloud API token. `extensions/whatsapp/package.json:12` pins `baileys@7.0.0-rc13` (a *release candidate*); `session.ts:34,158,172` uses `useMultiFileAuthState` + `makeWASocket`. There is **no token**: `secret-contract-api.ts:2` is an empty registry (*"WhatsApp does not expose secret-contract surfaces"*), and the generated metadata explicitly lists `channels.whatsapp.…creds.json` as an *unsupported* secret surface. Linking is a **live, ~30s-expiring, auto-refreshing QR** surfaced **only through the `whatsapp_login` agent tool** (`agent-tools-login.ts:15-111`, returns `![whatsapp-qr](${qrDataUrl})` as a markdown image in a chat reply) — there is no REST/gateway-RPC the existing wizard can call.

**Session-persistence / EFS concerns:** Baileys writes `creds.json` **plus a directory of many small signal-key files** (pre-keys, sessions, sender-keys) that churn on every message, at `…/credentials/whatsapp/<account>/` on the per-user EFS workspace (`auth-presence.ts:35`, `paths.ts:285-294`). This is the **exact small-files-on-NFS hazard** that already crash-loops the founders' 0.5-vCPU container. OpenClaw's atomic writer hardens only `creds.json` (`creds-persistence.ts:16-42`); the churning key files written by Baileys' own `useMultiFileAuthState` are not. **Reprovision wipes the link** with no re-PATCH recovery (unlike token channels), and a 401 logout forces a fresh QR re-scan — a recurring support burden.

**Stability / ToS risk:** RC-pinned reverse-engineered library; docs warn of reconnect loops, `status=408` timeouts, and a Node-only constraint. **Ban risk is the big one:** automated use of an unofficial WhatsApp Web link can get the user's **phone number banned** — OpenClaw's own docs recommend a separate eSIM number (`openclaw.plugin.json:43`). For a self-serve SaaS where users link their primary number, that is a product-liability + support surface the official-API token channels do not carry.

**Eng cost vs token channels:** ~1 day for a 4th token channel; **~1.5–2.5 weeks** for WhatsApp — the config block is cheap (~0.5 day, `whatsapp` is a valid channel id and maps onto the existing `dmPolicy`/`allowFrom` machinery), but a from-scratch live-QR wizard, a new backend route + gateway-RPC/agent-tool bridge (none exists today), the EFS session-state + reprovision-survival lifecycle, and mandatory EFS-under-load testing are the bulk of the work.

**Recommendation: DEFER.** Three deciding reasons: (1) **No token = the existing wizard rail can't reach it** — everything Isol8 built assumes a token → `PATCH /config`; WhatsApp needs a from-scratch live-QR flow wired to an *agent tool*. (2) **EFS/NFS churning-session hazard + reprovision data-loss** — the exact pattern that already crash-loops the founders' container, with no re-PATCH recovery. (3) **ToS/ban risk on an RC library** — can ban the user's number; docs recommend a separate eSIM. If WhatsApp is strategically required, the lower-risk path is to **wait for an official Meta Cloud API channel** in OpenClaw (token-based, slots into the existing rail).

---

## 5. Prioritized remediation plan

Ordered by impact × how-broken. The two live bugs first.

1. **[LIVE BUG — Slack] Replace the stale `SLACK_APP_MANIFEST` with the 5.28 canonical manifest.** Files: `apps/frontend/src/components/channels/BotSetupWizard.tsx:55-75` (source the new manifest from `extensions/slack/src/setup-shared.ts:10-105`). Add `app_home` + `assistant_view` features, the `/openclaw` slash command, the 17 missing scopes, the 13 missing events; keep `socket_mode_enabled:true` and the `connections:write` instruction. **Effort: S** (frontend string constant + a re-create-app instruction note). **Risk if skipped:** every Slack workspace silently under-provisions — App Home is broken, assistant threads 500, channel/reaction/pin/file features fail; users blame Isol8. **Schema note:** does NOT touch `openclaw.json`; no boot-crash risk.

2. **[LIVE BUG — Telegram/all] Fail-fast on a post-PATCH reload-skip.** Files: `apps/backend/routers/config.py` (`PATCH /config` handler, ~`:158-161`); reuse the server-side `channels.status` probe pattern at `apps/backend/routers/channels.py:90-97`. After `patch_openclaw_config`, probe; if the new `accounts.<agent_id>` is absent or `lastError` indicates a reload skip, raise `HTTPException(422, detail={"error":"config_reload_rejected","field":<issue>})`. **Effort: M.** **Risk if skipped:** every channel setup on any container carrying a 5.28-invalid stale field hangs 60s and false-fails with a token-blaming message; users are permanently blocked with no diagnostic. **Schema note:** read-only probe, no `openclaw.json` write — safe.

3. **[OPERATIONAL] Reprovision legacy/stale containers onto the pinned 5.28 image.** No code; use `DELETE`+`POST /api/v1/debug/provision` (dev) or the prod org-reprovision runbook (preserves billing/sub/marketplace). **Effort: S per container.** **Risk if skipped:** any container with a pre-bump stale field stays in the reload-skip trap (bug 1 above) until reprovisioned; this is the immediate unblock for the co-founder. Gate on the live-log finding in §6 first.

4. **[GAP] Add pre-write schema validation to the config-write path.** Files: `apps/backend/core/services/config_patcher.py` (validate the merged result before `os.rename`) — validate against the 5.28 channel schema before the write lands. **Effort: M–L** (needs a vendored/synced copy of the 5.28 channel schema or a gateway-side validate RPC). **Risk if skipped:** the systemic root cause persists — any future field drift bricks hot-reload silently again. **Schema note:** this is the *defense* against schema-drift boot-loops; it must itself be validated against the 5.28 schema.

5. **[GAP] Re-inject channel accounts on reprovision (or warn).** Files: `apps/backend/core/containers/config.py` (mirror the `integration_mcp_servers` re-injection at `:893-936` for the `channels` block; today the wholesale rewrite at `:1007` wipes live channel accounts/tokens/`allowFrom`). **Effort: M.** **Risk if skipped:** every reprovision silently drops all configured channels — users must re-run the wizard with no warning. **Schema note:** writes `channels.<provider>.accounts.*` into `openclaw.json` — **validate the re-injected shape against the 5.28 schema before deploy** to avoid a boot crash-loop.

6. **[POLISH] Close the wizard UX gaps.** Files: `BotSetupWizard.tsx` (add a Cancel/Back on the 60s `connecting` step, `:841-843`; pass `botUsername` in the Settings link path, `MyChannelsSection.tsx:181-190`); extend `_check_token_collision` to also check Discord `token` and Slack `appToken` (`config.py:70`); fix the misleading `ChannelsStatusPanel` empty-state pointer (`ChannelsStatusPanel.tsx:114`). **Effort: S each.** **Risk if skipped:** persistent low-grade UX friction and a duplicate-token foot-gun on Discord/Slack; not blocking.

---

## 6. Open questions / things to confirm live

1. **[Telegram — blocking] Pull the co-founder's container reload log.** CloudWatch group `/isol8/<env>/openclaw`, grep `config reload skipped (invalid config)`. **Action:** if present, the `: <path>: <issue>` suffix names the exact stale field → confirms bug 3.1 and the reprovision fix; if instead `getMe returned 401` appears, it's a bad token (S1), not a config bug. Needs AWS SSO (`aws sso login --profile isol8-admin`).

2. **[Slack — blocking] Pull the real Slack error line (the diagnosis never did).** Same log group, grep `missing_scope` (confirms manifest gap + `needed:` names the first scope), `views.publish` (confirms App Home is the surfacing path), and `slack token mismatch`/`invalid_auth`/`auth.test failed` (would redirect to a token problem, which is *more* log-visible than the manifest gap). **Action:** this is mandatory — the manifest staleness is proven from code, but attribution of *this* container's logged error is unproven. Note the assistant-thread `missing_scope` failures are silent by default (`logVerbose`), so absence of those lines does NOT clear the manifest.

3. **[Slack — confirm] Was anyone exercising App Home / assistant threads?** The only default-logged `missing_scope` (`home.ts:66`) fires only on `app_home_opened`. **Action:** if the founders never opened the bot's App Home tab, expect zero `missing_scope` lines even with the stale manifest — in which case the "log issue" is something else (token pairing) and bug 3.2 is latent-only.

4. **[Validation — confirm before deploy] Re-validate any `openclaw.json`-touching change against the live 5.28 schema.** Applies to remediation items 4 and 5. **Action:** validate the merged/re-injected config against the genuine 2026.5.28 schema (local checkout confirmed at that version) on a test container before fleet deploy — a wrong value bricks the box at boot.

5. **[Telegram — confirm the fix end-to-end] After reprovision, re-run the wizard on a live dev account.** **Action:** confirm it reaches the `pair` step within seconds (not a 60s hang); use a personal dev Clerk account, never the E2E `isol8-e2e-testing@mailsac.com` account.
