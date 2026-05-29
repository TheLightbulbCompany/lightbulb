# Credential Linking Layer Design

**Date:** 2026-05-21
**Status:** ⚠️ SUPERSEDED (framing) by **`docs/superpowers/specs/2026-05-29-credential-integration-registry-design.md` (Part 2)**. Kept for its verified facts (OpenClaw reload rules, Nango API/catalog audit) and the deferred milestones (Jarvis M6, AWS STS M7), which are now used as *reference*, not the v1 plan.

> **ERRATA (verified 2026-05-29 — read Part 2 for the current decision):**
> - **v1 does NOT use Nango.** Bucketing the actual shipped integration set found 0 live providers that need a true-OAuth broker (1 deferred: Outlook). v1 = build our own integrations panel (`direct_oauth` + materialize) reusing the channels/ChatGPT rails. Adopt Nango only at ~5–8 OAuth providers.
> - **D3 is wrong.** Nango sends **no webhook on successful refresh** (`auth_refresh` fires only on failure — `packages/webhooks/lib/utils.ts`). Refresh is lazy refresh-on-read via reconcile, not webhook-pushed.
> - **§500 reversed.** Agent-native CLIs (`gog`, Printing Press) are a first-class actor — they self-refresh; we no longer "prefer `gcloud` over `gog`."
> - **The parked `credentials-stack.ts` has a vault-fatal bug** (`NANGO_ENCRYPTION_KEY` non-base64-32-byte + no `RETAIN`). Do not deploy as-is.
> - **Nango's open-source catalog (`packages/providers/providers.yaml`) is reference data** for our own `providers.yaml` (the OAuth endpoints/scopes are facts); **Nango Proxy stays reserved** for any future tokens-never-on-disk provider.
**Source-of-truth refs:**
- `docs/architecture/credentials/problem-discovery.md` — the rubric (acceptance criteria, personas, problem matrix)
- `docs/architecture/credentials/research-notes.md` — verified facts + runtime-delivery analysis
- `docs/architecture/credentials/solution-brief-stale.md` — earlier brief (partially superseded; Nango pick + Connect UI sections still load-bearing)

**Nango facts verified 2026-05-21** against:
- License: `github.com/NangoHQ/nango/blob/master/LICENSE` — ELv2, "may not provide … as a hosted or managed service to third parties" (Isol8 internal use is in scope)
- Self-host: `nango.dev/docs/guides/platform/self-hosting.md` + `github.com/NangoHQ/nango/blob/master/docker-compose.yaml`
- Webhooks: `nango.dev/docs/guides/platform/webhooks-from-nango.md`
- Connect Session API: `nango.dev/docs/reference/api/connect/sessions/create` — `POST /connect/sessions` with `tags`; returns `{token, connect_link, expires_at}` (30-min expiry)
- Connection get API: `nango.dev/docs/reference/api/connection/get` — `GET /connection/{id}?provider_config_key=...`
- Connection list API: `nango.dev/docs/reference/api/connection/list` — `GET /connection` with `tags[end_user_id]=...` filter (the `endUserId` query param is deprecated; tag-based filtering is the supported path)
- Tenancy: `nango.dev/docs/guides/platform/migrations/migrate-from-end-user.md` — `end_user` is deprecated; use `tags`
- Frontend SDK: `nango.dev/docs/reference/frontend/frontend-sdk.md`
- BYO OAuth: `nango.dev/docs/guides/auth/auth-guide` — first-class, recommended for production
- Connect UI customization: `nango.dev/docs/guides/auth/customize-connect-ui.md` — color + theme only; no logo
- Direction: active dev, v0.70.4 released 2026-05-18 (three days before this doc)

**OpenClaw facts verified 2026-05-21, two key facts re-verified against current upstream HEAD 2026-05-24** (latest release v2026.5.22-beta.1 on 2026-05-23):
- Chokidar reload + `dispose-mcp-runtimes` on `mcp.*` config changes: `src/gateway/config-reload-plan.ts` `BASE_RELOAD_RULES` (re-verified 2026-05-24 — rule unchanged)
- **Bonus reload-trigger paths verified 2026-05-24**: `plugins.*` and `plugins.entries.${plugin.id}` ALSO trigger `dispose-mcp-runtimes` (in addition to `reload-plugins`). Means writing credentials to plugin-config paths in openclaw.json also respawns MCPs that depend on that plugin — strictly more flexibility than the doc previously claimed.
- SecretRef `file` / `exec` sources resolve at use-time (not config-load-time): `src/secrets/resolve.ts:898`; consumer at `agents/tools/message-tool.ts:768` (file unchanged upstream since clone — same git SHA)
- Skills declare `requires.bins` and `requires.config` in YAML frontmatter: e.g., `skills/github/SKILL.md` declares `requires.bins: [gh]`; `skills/slack/SKILL.md` declares `requires.config: [channels.slack]`
- Container layout: `USER node, WORKDIR /home/node` per `apps/infra/openclaw/Dockerfile`
- **Bash-tool inherits config-pointer env vars from parent process** (re-verified 2026-05-24): `src/infra/host-env-security-policy.json` `allowedInheritedOverrideOnlyKeys` still contains all of `XDG_CONFIG_HOME`, `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `KUBECONFIG`, `GOOGLE_APPLICATION_CREDENTIALS`, `DOCKER_HOST`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY` — keystone fact for env-var-pointer design.
- chatgpt_oauth precedent: pre-stages files at OpenClaw's expected EFS paths (`codex/auth.json` + `<agentDir>/agent/auth-profiles.json`) — backend writes; OpenClaw reads via its own resolution. The earlier env-var-pointer attempt (`CODEX_HOME`) was REMOVED because OpenClaw's internal openai-codex provider doesn't read it — see test at `apps/backend/tests/unit/containers/test_ecs_manager.py:2165-2178` which hard-asserts CODEX_HOME absence. Implication: env-var-pointers work for CLIs that honor their documented config-path env vars, NOT for OpenClaw-internal credential consumption.
- **Upstream PR status (re-verified 2026-05-24)**: PR #72548 (SecretRef plugin SDK seam) still OPEN, last updated 2026-05-22, NOT merged. Issue #76493 (SecretRef in mcp.servers[].env) still OPEN with no PR attached. The migration path described in this doc (~2-3 weeks once both land) remains a future option, not a current one.

**Gotcha — reload behavior we are NOT relying on:**
`BASE_RELOAD_RULES_TAIL` lists `secrets` and `identity` config paths with `kind: "none"`. Changes to `secrets.providers.*` in openclaw.json do NOT trigger a hot-reload; they take effect only on OpenClaw restart. **Our design does not write to `secrets.providers.*` at runtime** — credentials land in `mcp.servers[].env`, `skills.entries.*.apiKey` (as SecretRef→file), `channels.*` paths, or EFS files. All those paths DO hot-reload (or resolve at use-time). But future engineers should NOT add a "just update `secrets.providers.*` at runtime" shortcut — it won't work without restart.

**Not yet verified** — would require integration smoke during M1/M2:
- End-to-end token rotation (file rewrite → CLI reads new value on next invocation; logically sound, not tested)
- EFS write latency under concurrent reconciles at scale
- Nango's white-label customization gating on self-host vs cloud (Open Question 3)
- Whether the bundled `:hosted` Nango image scales to projected load (we find out around 1k owners)

## Overview

Build the credential layer that lets Isol8 agents act in third-party services on behalf of whoever owns the agent — the end customer for marketplace agents (their Slack, their Greenhouse), the Isol8 platform team for Jarvis (its `admin@isol8.co` Google, its GitHub PAT). For OAuth providers: customer clicks "Connect Slack," sees an Isol8-branded OAuth consent screen, returns to a green checkmark; the agent finds the token at use time. For AWS cross-account: customer deploys an Isol8-provided CFN template, pastes the resulting Role ARN; agent uses temporary STS credentials. For Jarvis: platform team runs a one-time admin CLI; identity tokens are vaulted in AWS Secrets Manager. No tokens are ever typed, pasted, or visible to end customers. Refresh is invisible; rotation is invisible; revocation is one click.

This is **one cohesive plan executed across 9 sequential milestones**. Each milestone is independently shippable (passes its own exit criterion), and each milestone unblocks subsequent work. The plan covers all three credential subjects in the discovery doc taxonomy:

- **Tier 3 customer-delegated OAuth** (Slack, Google Workspace, Greenhouse, etc.) — M1-M5
- **Tier 2 agent identity** (Jarvis's Google refresh token, GitHub PAT) — M6
- **Tier 3 AWS cross-account** (the customer's AWS via STS AssumeRole) — M7
- Marketplace seller manifest declarations + safety scanner — M8
- Production hardening + first-provider end-to-end smoke — M9

Nothing is deferred to a separate work stream. What's truly out of scope (multi-account per provider, BYO OAuth clients per customer, per-call audit, custom-built Connect UI) is listed at the end and is v2+ work.

## Key Discovery: OpenClaw upstream anticipated this and ships solutions for most of it

The stale brief proposed building a wrapper-shim (`isol8-tool`) and an init binary (`isol8-credinit`) to deliver credentials to tools. Both turn out to be unnecessary because OpenClaw upstream **already provides** the primitives this design needs. Verified by reading source at HEAD `510628ca6f`:

### 1. Skill manifests declare what they need

Every skill in `~/Desktop/openclaw/skills/*/SKILL.md` has YAML frontmatter with `metadata.openclaw.requires` declaring:
- `bins: [<binary>]` — which CLI binaries the agent will invoke through this skill
- `config: [<path>]` — which openclaw.json config paths must be populated

```yaml
# skills/github/SKILL.md
metadata:
  openclaw:
    requires: { bins: [gh] }

# skills/slack/SKILL.md
metadata:
  openclaw:
    requires: { config: [channels.slack] }
```

**This is the declarative surface that lets us validate at deploy time** that every required binary is installed and every required config path is populated — without intercepting anything.

### 2. A path-aware config-reload pipeline (the keystone)

OpenClaw runs a chokidar watcher on `openclaw.json` at `src/gateway/config-reload.ts` and applies path-aware hot-reload via `src/gateway/config-reload-plan.ts`. The relevant rules (verified against upstream HEAD 2026-05-24):

```ts
{ prefix: "mcp", kind: "hot", actions: ["dispose-mcp-runtimes"] }
{ prefix: "plugins", kind: "hot", actions: ["reload-plugins", "dispose-mcp-runtimes"] }
{ prefix: "plugins.entries.${plugin.id}", kind: "hot", actions: [..., "dispose-mcp-runtimes"] }
```

means any change to an `mcp.*` OR `plugins.*` path **hot-disposes MCP runtimes**. The next agent call respawns the MCP server picking up whatever the current `mcp.servers[].env` says. **Token rotation for MCP servers isn't a separate engineering problem — it falls out of the file watcher.** Combined with Isol8's existing `config_patcher.py` (file-locked, atomic deep-merge of openclaw.json on EFS, already production-tested for Track 1 silent updates), the delivery side for MCP env values is essentially free.

**Important non-rule** (verified 2026-05-24): `secrets.providers.*` and `identity.*` paths are listed in `BASE_RELOAD_RULES_TAIL` with `kind: "none"` — i.e., they do NOT hot-reload. Future maintainers: do not put runtime-rotating credentials into `secrets.providers.*` thinking it'll auto-pick-up. It won't, until the next OpenClaw restart. We deliberately route around this by writing to the hot-reloading paths (`mcp.servers[].env`, `skills.entries.*.apiKey` as SecretRef-with-file-source, `channels.*` paths) instead.

### 3. A SecretRef resolver with use-time resolution

`src/secrets/resolve.ts` exposes `resolveSecretRefValue` / `resolveSecretRefString` with three built-in sources (`env` / `file` / `exec`). The `file` source reads a path on disk at *resolution time*, not at config-load time — and consumers like `agents/tools/message-tool.ts:768` (`resolveSecretRefsForTool`) call it per-tool-invocation. For any `SecretInput`-typed config field, **a SecretRef pointing at an EFS file means refresh is "rewrite the file" — no openclaw.json change, no reload, no MCP respawn, no anything.**

### 4. A bash-tool env-inheritance whitelist for exactly this use case

`src/infra/host-env-security-policy.json` lists in `allowedInheritedOverrideOnlyKeys`:
- `XDG_CONFIG_HOME`, `XDG_DATA_HOME`
- `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_WEB_IDENTITY_TOKEN_FILE`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `KUBECONFIG`
- `DOCKER_HOST`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`
- `AZURE_AUTH_LOCATION`
- `SSH_AUTH_SOCK`
- ...and others

These are **explicitly allowed to flow from the OpenClaw parent process to bash-tool-spawned children**. This isn't a side effect — it's a curated list. OpenClaw's authors anticipated that operators would point CLIs at specific config files via these env vars, and built the security policy to permit it while blocking dangerous overrides (`AWS_SECRET_ACCESS_KEY` as a direct value is in `blockedOverrideOnlyKeys`, but `AWS_SHARED_CREDENTIALS_FILE` as a pointer is in `allowedInheritedOverrideOnlyKeys`). **Our entire CLI delivery story stands on this list.**

### 5. Partial Isol8 precedent — proven for file pre-staging; the env-var-pointer half is new

Two things to be precise about here, because my earlier draft of this doc overclaimed:

**What IS already an Isol8 production pattern (the file pre-staging half):**
The chatgpt_oauth flow pre-stages **files at OpenClaw's expected paths** on EFS — specifically `codex/auth.json` AND `<agentDir>/agent/auth-profiles.json` (see memory `feedback_openclaw_chatgpt_auth_two_files`). OpenClaw discovers these via its own internal path resolution. The backend writes; OpenClaw reads; no env-var indirection.

This is the precedent for the file-based delivery half of our design (materializers writing `~/.aws/credentials`, the SecretRef-target file under `$ISOL8_SECRETS_DIR/<provider>.json`). Same primitive: backend writes file at a path; container picks it up.

Isol8 also already sets per-user env vars at task-def time — `OPENCLAW_HOOK_TOKEN` is set per-container with a per-user value. So "per-user task-def env vars" is a proven pattern.

**What is NEW in this design (the env-var-pointer half):**
Setting per-user task-def env vars that *point at* EFS config files (like `AWS_SHARED_CREDENTIALS_FILE=/mnt/efs/users/<owner>/credentials/aws/credentials`) is not currently done in Isol8.

Importantly: an earlier Isol8 attempt at this exact shape was REMOVED. `CODEX_HOME` used to be set per-task pointing at EFS, but the chatgpt_oauth flow now hard-asserts its absence (`apps/backend/tests/unit/containers/test_ecs_manager.py:2165-2178`):

> *"The per-user task def MUST NOT carry CODEX_HOME. We used to inject it pointing at codex/auth.json — that approach was wrong: OpenClaw's openai-codex provider reads from <agentDir>/auth-profiles.json, not from $CODEX_HOME/auth.json."*

The reason it failed: **OpenClaw's internal openai-codex provider doesn't read the `CODEX_HOME` env var** — it has its own auth-profile path resolution. So pointing an env var at our chosen path didn't change what OpenClaw looked at.

This is why upstream's `allowedInheritedOverrideOnlyKeys` whitelist (signal #4 above) is the actual load-bearing evidence for the env-var-pointer pattern, not CODEX_HOME. **The pattern works specifically for CLIs that DO honor the relevant env vars as documented config paths** — `aws` honors `AWS_SHARED_CREDENTIALS_FILE`, `gh` honors `XDG_CONFIG_HOME`, `gcloud` honors `GOOGLE_APPLICATION_CREDENTIALS`. It does NOT work for tools that ignore env vars and have their own path-resolution logic (like OpenClaw's openai-codex provider did).

**Implication for this design**: we use the env-var-pointer pattern for CLIs only (where docs guarantee the tool honors the var). For OpenClaw-internal credential consumption (skills, channel plugins, MCP env), we use the paths OpenClaw expects — same approach as chatgpt_oauth's proven pre-stage pattern. No env-var indirection where the consumer is OpenClaw itself.

### 6. Upstream is heading further in this direction

Three OPEN upstream items continue the trajectory:
- **PR #72548** (`feat(secrets): pluggable SecretRef sources via plugin SDK seam`) — APPROVED companion #82326 — extends the SecretRef resolver with a plugin seam, ships reference `extensions/secrets-gcp/` and `extensions/secrets-keyring/` plugins. If/when this lands, we could write a Nango-backed SecretRef plugin (~1 week of work) and remove our backend's `config_patcher` step for SecretRef-shaped fields. Migration is bounded.
- **Issue #76493** — proposes opening `mcp.servers[].env` to accept SecretRef objects. No PR yet. If/when this lands, MCP env can stop being literal-value-injected and become SecretRef-resolved like skills are today.
- **Issue #71593** — the parent feature issue tracking broader SecretRef field coverage. The trajectory is clear: more fields will accept SecretRef over time. Our design uses SecretRefs where the schema accepts them today (skills.entries.*.apiKey) and literal values where it doesn't (mcp.servers[].env); the literal-value paths will become SecretRef-shaped over time without changing the rest of the design.

### What we still have to build

Upstream gives us the runtime substrate. Isol8 still owns:
- **The vault** (Nango deployment for tier 3, AWS Secrets Manager wiring for tier 2)
- **The fetch + project pipeline** (`apply_connection`, materializers, providers.yaml)
- **The customer-facing Connect UI** (via Nango's hosted Connect UI for v1)
- **The webhook handler** (`/webhooks/nango`)
- **The provision-gate reconciliation** (extend `ensure_user_ready`)
- **The marketplace validator** (cross-reference manifest ↔ providers.yaml ↔ skill requires)

But the runtime delivery — the part that's typically hardest because it has to work without restarts, has to support all four tool primitives, and has to play nice with the platform's existing reload pipeline — that's substantially solved upstream. We don't need to fork OpenClaw, intercept anything, or build wrappers. We write files to EFS and openclaw.json paths; OpenClaw's existing machinery does the rest.

## The Problem, Restated Precisely

Three observations frame what we're actually building:

**1. Isol8 agents act through tools.** An agent is an LLM session inside OpenClaw. It doesn't make API calls directly; it invokes tools and the tools make the calls. The four tool primitives are MCP servers, CLIs (via OpenClaw's bash tool), plugins (channel adapters like Telegram/Slack), and skills (OpenClaw's first-class verb modules). Each of those tools reads credentials in its own way.

**2. There is no universal env var.** A Slack MCP server reads `SLACK_BOT_TOKEN`. The `gh` CLI reads `GH_TOKEN`. The `gog` CLI reads its own internal credential storage. The `aws` CLI reads `AWS_ACCESS_KEY_ID` plus `AWS_SECRET_ACCESS_KEY` (or `~/.aws/credentials`). Each tool's contract is its own.

**3. Credentials come from different stores depending on whose identity they represent.** Customer-delegated OAuth tokens live in Nango. Platform-internal credentials (Jarvis's GitHub PAT, Isol8's Stripe webhook key) live in AWS Secrets Manager. Some agent identities (Jarvis's AWS) need no static credential at all — IAM task role + STS.

**The "linking" problem** is: given (a) the right vault for each credential, (b) the per-tool convention for how that credential is consumed, deliver the credential to the running process in the shape it expects, refresh it when the source rotates, and surface failures usefully.

### What lives where

Two stores, one delivery pipeline. No single vault holds everything; that would be password-manager drift and a category error for Nango specifically.

| Credential | Owner | Lives in | Why |
|---|---|---|---|
| Customer's Slack OAuth tokens | End customer | **Nango** | Standard OAuth, refresh-able, provider-issued |
| Customer's Greenhouse Harvest API key (no OAuth) | End customer | **Nango** (API_KEY auth mode) | Customer pastes once via Connect UI; Nango stores; central revoke; no auto-refresh but tracked |
| Customer's Google Workspace OAuth | End customer | **Nango** | OAuth |
| Jarvis's `admin@isol8.co` Google refresh token | Isol8 platform | **AWS Secrets Manager** | One-time human bootstrap; not a "customer" relationship; not a user-driven OAuth flow |
| Jarvis's GitHub PAT | Isol8 platform | **AWS Secrets Manager** | Static token, no OAuth flow, manually rotated |
| Jarvis's AWS access | Isol8 platform | **IAM task role** | Zero static credentials; ECS task role + STS |
| Isol8's per-provider OAuth client_id/client_secret (the Slack app we registered) | Isol8 platform | **AWS Secrets Manager** | Mounted into Nango Fargate task at startup; isn't a credential we deliver to agents |
| Isol8 platform infra secrets (Stripe webhook signing key, KMS keys, etc.) | Isol8 platform | **AWS Secrets Manager** (already in use) | Existing pattern; unchanged |

**Rule of thumb**: Nango holds credentials that came from a provider integration (OAuth, API_KEY-the-user-pasted, BASIC, JWT). Everything else lives in AWS Secrets Manager. If you're tempted to put a static internal token in Nango "for consistency," you're using Nango wrong.

```
Nango (token vault) ────┐
                        ├──▶ apply_connection ──▶ config_patcher ──▶ openclaw.json paths
AWS Secrets Manager ────┘                          + materializers ──▶ files on EFS
   (+ IAM task role)                                                    + sourced env file
```

`apply_connection` is **source-agnostic**. It takes a "connection-shaped dict" — `{provider, credentials, connection_config, tags}` — and writes the appropriate paths. The dict can come from `nango_client.get_connection()` for tier 3, or from `agent_identity_client.get_jarvis_credentials()` for tier 2. The delivery pipeline does not care.

## Where credential conventions come from

`providers.yaml` is the bridge between Nango's typed `credentials` and the per-tool delivery conventions. It's the load-bearing piece of curated knowledge in the whole design. This section makes precise what is and isn't discoverable.

### What we get from OpenClaw skill manifests (partial discovery)

Verified by inspecting `~/Desktop/openclaw/skills/*/SKILL.md`. Every skill has YAML frontmatter:

```yaml
# ~/Desktop/openclaw/skills/github/SKILL.md
metadata:
  openclaw:
    requires:
      bins: [gh]        # declares: needs the `gh` binary installed
    install: [...]      # how to install on brew/apt

# ~/Desktop/openclaw/skills/slack/SKILL.md
metadata:
  openclaw:
    requires:
      config: [channels.slack]   # declares: needs channels.slack in openclaw.json
```

So skills tell us:
- **Which CLI binaries the agent will invoke** (`requires.bins`)
- **Which openclaw.json config paths must be populated** (`requires.config`)

What skills do **NOT** tell us:
- The env var name the binary reads (gh: `GH_TOKEN`; aws: `AWS_ACCESS_KEY_ID`; stripe: `STRIPE_API_KEY`)
- The config file path the binary reads (gh: `~/.config/gh/hosts.yml`; aws: `~/.aws/credentials`; gcloud: ADC JSON at `$GOOGLE_APPLICATION_CREDENTIALS`)
- For proprietary CLIs (e.g., `gog`), the internal credential storage format the CLI uses
- For MCP servers, the env var names the MCP expects

These conventions live in the tool's own documentation. We can't auto-discover them. They have to be encoded somewhere — that "somewhere" is `providers.yaml`.

### The delivery shapes, ranked by refresh-friendliness

Every credential lands in EFS one of these ways. Listed best-first; the system uses whichever the provider's `providers.yaml` entry declares:

| Shape | Example | How `apply_connection` delivers | Refresh cost |
|---|---|---|---|
| **SecretRef file source on a `SecretInput` field** | `skills.entries.slack.apiKey = {source: "file", provider: "isol8", id: "/mnt/efs/users/<owner>/credentials/secrets/slack.json#/access_token"}` | openclaw.json written ONCE at provision with the SecretRef; backend writes/rewrites the EFS file pointed at by the SecretRef on each refresh | **None.** SecretRef resolves at use-time; OpenClaw re-reads the file each time the value is needed |
| **Materialized config file at the CLI's conventional path** | `gh` reads `$XDG_CONFIG_HOME/gh/hosts.yml`; aws reads `$AWS_SHARED_CREDENTIALS_FILE`; gcloud reads `$GOOGLE_APPLICATION_CREDENTIALS` | Backend writes the file at the path the CLI reads (chown 1000:1000, perms 0640) | **None.** CLI re-reads file on next invocation |
| **Literal value in `mcp.servers[].env`** | Slack MCP server reads `SLACK_BOT_TOKEN` from spawn env | Write to `mcp.servers.<name>.env.SLACK_BOT_TOKEN` in openclaw.json | **Bounded.** chokidar fires; `dispose-mcp-runtimes` disposes the runtime; next call respawns the MCP child with new env. No OpenClaw restart. |
| **Inline value in an openclaw.json config path** (e.g., channel plugin tokens) | Telegram plugin reads `channels.telegram.accounts.<id>.botToken` (if it doesn't use the `tokenFile` variant); SDK fields without `SecretInput` typing | Write the literal value to the path; OpenClaw's reload pipeline picks up the change | Same as above — chokidar + plugin-specific reload |
| ❌ **Sourced parent-env file requiring OpenClaw restart on refresh** | *(removed)* | *(removed)* | *(would require restart — anti-pattern; do not use)* |

**The agent's bash-tool inherits a small fixed set of config-pointer env vars** from the OpenClaw parent process — verified at `src/infra/host-env-security-policy.json` `allowedInheritedOverrideOnlyKeys`. These pointers are set ONCE at ECS task-def time (alongside the existing CODEX_HOME pattern), point at EFS paths, and never change for the container's lifetime. The credentials AT those paths change freely.

The pointer set (set in ECS task def per owner, the same shape for every container):

```bash
XDG_CONFIG_HOME=/mnt/efs/users/<owner>/credentials/config       # gh, git, gcloud, many others
AWS_CONFIG_FILE=/mnt/efs/users/<owner>/credentials/aws/config
AWS_SHARED_CREDENTIALS_FILE=/mnt/efs/users/<owner>/credentials/aws/credentials
GOOGLE_APPLICATION_CREDENTIALS=/mnt/efs/users/<owner>/credentials/gcloud/adc.json
KUBECONFIG=/mnt/efs/users/<owner>/credentials/kube/config
DOCKER_CONFIG=/mnt/efs/users/<owner>/credentials/docker
ISOL8_SECRETS_DIR=/mnt/efs/users/<owner>/credentials/secrets    # for SecretRef file targets we own
```

That's the entire fixed container-side env surface. **Adding a new provider does not change any of these env vars.** New providers get new files under these existing roots.

### What `providers.yaml` actually declares

`providers.yaml` is an **Isol8-internal engineering artifact** — checked into this repo, edited only by Isol8 engineers, never visible to customers. It encodes "for provider X, which delivery shape, mapping which Nango payload fields to which destination."

```yaml
# providers.yaml — Isol8-internal, version-controlled, ~10-20 providers initially

github:
  shapes:
    - kind: file
      relative_to: XDG_CONFIG_HOME           # writes under $XDG_CONFIG_HOME/gh/hosts.yml
      relative_path: gh/hosts.yml
      format: yaml
      template:
        github.com:
          oauth_token: "{{ credentials.access_token }}"
          user: "{{ connection_config.login }}"
          git_protocol: https

slack:
  shapes:
    - kind: mcp_env                          # for MCP servers that need SLACK_BOT_TOKEN at spawn
      var_name: SLACK_BOT_TOKEN
      value_path: credentials.access_token
    - kind: secretref_file                   # for OpenClaw skills that read via SecretInput
      relative_to: ISOL8_SECRETS_DIR         # writes under $ISOL8_SECRETS_DIR/slack.json
      relative_path: slack.json
      format: json
      template:
        access_token: "{{ credentials.access_token }}"
        team_id: "{{ connection_config.team.id }}"

aws:
  shapes:
    - kind: file
      relative_to: AWS_SHARED_CREDENTIALS_FILE   # the env var IS the target file path
      format: ini
      template:
        default:
          aws_access_key_id: "{{ credentials.access_key_id }}"
          aws_secret_access_key: "{{ credentials.secret_access_key }}"

google_workspace:
  shapes:
    - kind: file
      relative_to: GOOGLE_APPLICATION_CREDENTIALS  # env var IS the target path
      format: json
      template:
        type: authorized_user
        client_id: "{{ connection_config.client_id }}"
        client_secret: "{{ connection_config.client_secret }}"
        refresh_token: "{{ credentials.refresh_token }}"
```

**Adding a provider is an Isol8 engineering PR** (1-2 hours of toil):
1. Engineer registers an OAuth app at the provider for "Isol8" production
2. Adds `client_id`/`client_secret` to AWS Secrets Manager
3. Configures the integration in our Nango deployment (BYO OAuth client)
4. Adds a `providers.yaml` entry
5. PR, review, ship

After that, every customer's `/settings/connections` page shows the new provider as a "Connect" option automatically. Customers never see `providers.yaml`.

**Out-of-band providers (long tail):** if a customer wants a CLI or provider we haven't catalogued, they reach out and we evaluate (same 1-2 hour Isol8-side workflow). We do NOT build customer-self-service "BYO CLI" in v1. Standard industry pattern (Zapier, Make, n8n all work this way).

### Validation: closing the loop

At deploy time we cross-reference three sources, fail closed if anything is missing:

```
        seller manifest                  OpenClaw skill manifests          providers.yaml
        requires.credentials             requires.bins, requires.config    (provider → shapes)
              │                                       │                            │
              └────────────────────┬──────────────────┴────────────────────────────┘
                                   ▼
                  Pre-deploy validator (marketplace_service.deploy)
                  - Every required provider has a providers.yaml entry
                  - Every skill's requires.bins is satisfied by the container image
                  - Every skill's requires.config is populated by some provider's openclaw_paths
                  - Every MCP server's env in the bundle's openclaw.json declares
                    standard env var names that providers.yaml knows about
```

If any of these fail, the deploy is rejected with a structured error that lists what's missing. This is the safety scanner's "no surprises at runtime" property — the agent doesn't discover at run-time that the gog binary isn't installed or that no provider populates `channels.slack`.

### What we're explicitly NOT using from Nango

Nango ships a lot more than the OAuth/vault/Connect-UI surface we're consuming. To prevent scope drift, this is the negative list:

- **Nango Syncs** (background data sync from provider → Nango DB) — not used
- **Nango Actions** (typed wrappers around provider endpoints we'd invoke via Nango) — not used; agents call providers directly using the tokens we deliver
- **Nango Scripts** (custom TypeScript Sync/Action implementations) — not used
- **Nango Persist** (the service that stores synced records) — not used; bundled image runs it but it's idle
- **Nango Orchestrator / Jobs / Runner** — only used as bundled by the `nangohq/nango-server:hosted` image; they're idle for vault-only usage. Future optimization: deploy Server + Postgres + Redis only via Helm, drop the others.
- **ClickHouse** — Nango uses it internally for analytics; we don't query it

If a future engineer reads this doc and wonders "should we use Nango Syncs for X" — the answer is no. We get tokens from Nango, full stop. Tools using those tokens is our problem.

## Container has zero credential coupling at start

A consequence worth stating directly: **the per-user OpenClaw container starts with no credentials and no knowledge of which providers will ever be connected.**

- ECS task-def env vars are pointers to EFS paths. The paths may not yet exist when the container starts.
- openclaw.json is provisioned with the agent shape (skills, MCP definitions, etc.) — credential-bearing fields are SecretRef-with-file-source pointing at EFS paths that don't yet exist.
- The container can run for weeks before any provider is connected. Tools that need a credential and find the file absent return "not authenticated"; the agent surfaces that to the user.
- When the customer eventually clicks "Connect Slack," the backend writes the credential file. The next time the tool is invoked, it works. **No container restart at any point.**

This decoupling is the design's most important property. It's what makes asynchronous OAuth completion natural rather than a timing race.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Isol8 backend (FastAPI)                          │
│                                                                         │
│   ┌───────────────────┐   ┌──────────────────┐   ┌─────────────────┐   │
│   │ /connections UI   │──▶│ POST /connect/   │──▶│ Nango self-host │   │
│   │ "Connect Slack"   │   │   {provider}     │   │  (token vault)  │   │
│   └───────────────────┘   └──────────────────┘   └────────┬────────┘   │
│                                                            │             │
│                              ┌─────────────────────────────┘             │
│                              │  Nango webhooks                           │
│                              │  - auth: token refreshed                  │
│                              │  - connection: created/updated/deleted    │
│                              ▼                                            │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ POST /webhooks/nango   (primary refresh path; verifies HMAC)     │  │
│   │   resolve event → owner_id (end_user_id on the connection)       │  │
│   │   fetch full connection from Nango (event payload is metadata)   │  │
│   │   patch = providers_yaml.render({provider: conn})                │  │
│   │   config_patcher.patch_openclaw_config(owner_id, patch)          │  │
│   │   materializers.write(owner_id, provider, conn)  # if file-based │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ ensure_user_ready (in user_provision.py) — fallback              │  │
│   │   on /container/status poll, if credential freshness > 1h:       │  │
│   │     same apply_connection path, all providers in one pass        │  │
│   │   (covers webhook delivery failures and providers.yaml deploys)  │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                                  │                                       │
└──────────────────────────────────┼───────────────────────────────────────┘
                                   │ EFS write via config_patcher
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│       Per-owner ECS task — OpenClaw container                            │
│                                                                         │
│   /mnt/efs/users/{owner}/                                               │
│     openclaw.json    ◀── chokidar watcher (CHOKIDAR_USEPOLLING=true)    │
│     ├─ mcp.servers.greenhouse.env.GREENHOUSE_API_KEY = "<live>"         │
│     ├─ skills.entries.slack.apiKey = "<live>"                           │
│     └─ channels.telegram.accounts.default.botToken = "<live>"           │
│     gcloud/adc.json  (materialized for file-based creds)                │
│                                                                         │
│   OpenClaw process:                                                     │
│     ├─ config-reload.ts: chokidar fires on openclaw.json change         │
│     ├─ config-reload-plan.ts: mcp.* path → dispose-mcp-runtimes         │
│     ├─ MCP runtimes disposed                                            │
│     └─ next chat message: respawn MCPs with new env from updated config │
└─────────────────────────────────────────────────────────────────────────┘
```

## Key Decisions (with rationale)

### D1 — OAuth broker: Nango self-hosted

Inherited from `solution-brief-stale.md` §1. Re-verified against the discovery doc's §7 acceptance criteria; nothing surfaced that changes the pick. Verified Nango facts:

- **License**: Elastic License 2.0. The only restriction is "may not provide … as a hosted or managed service to third parties." Isol8 using Nango internally as part of its own product is within scope.
- **BYO OAuth client per provider** is first-class and recommended for production. Configure each integration with `client_id`, `client_secret`, `scopes` — consent screens then read "Isol8 wants to access your Slack" (the §7.1 acceptance criterion).
- **Self-host story** is complete: official Helm charts + a public `nangohq/nango-server:hosted` Docker image. Healthy release cadence (v0.70.4 on 2026-05-18, three days before this doc).
- **Connect UI customization is limited**: theme (light/dark/system), primary color (light + dark), can remove the "Secured by Nango" footer mention. **No logo replacement.** These customizations require Nango's Growth plan on cloud (gating on self-hosted is unclear — see Open Questions). For full visual control, Nango offers a headless auth path where we build the UI ourselves; defer to v2 if the modal branding becomes a blocker.

Migration to Klavis or another vendor remains bounded (~4-6 weeks of glue): we use Nango only as token vault + Connect UI, not as a workflow/sync engine.

### D2 — Token delivery: literal injection via `config_patcher`

(Research notes §2 Design C.) Backend writes fetched tokens directly into `openclaw.json` paths (`mcp.servers[].env`, `skills.entries.*.apiKey`, `channels.*.accounts.*.botToken`) via the existing `config_patcher.patch_openclaw_config`. OpenClaw's chokidar watcher picks up the change, dispose-mcp-runtimes hot-reload fires, next call spawns MCPs with fresh env.

**Rejected alternatives:**
- **Wrapper shim** (`isol8-tool`): solves a problem we don't have (parent env doesn't inherit), adds ~5 new components, requires safety-scanner rule for every credentialed tool.
- **`${VAR}` interpolation + parent env preload**: cleaner authoring (tokens not in config) but refresh requires init-container coordination + file mtime touching to re-trigger config-reload. More moving parts for marginal benefit.
- **Wait for upstream PR #76493 (SecretRef in mcp.servers[].env)**: PR doesn't exist (only an issue), the SDK seam PR #72548 explicitly defers this field. No timeline. Migration cost from D2 if/when this lands is ~1 week.

**The single load-bearing concession**: literal tokens land in openclaw.json on EFS. Blast radius is one container per owner (per-tenant EFS, KMS-encrypted at rest, only that owner's task role can read it) — same as any in-container credential design. Accepted explicitly by the user.

### D3 — Refresh model: webhook-driven, reconcile-on-provision

Nango emits a single webhook event family — `type: "auth"` with `operation` ∈ {`creation`, `override`, `refresh`}. The backend subscribes once per env, handles events at `POST /webhooks/nango`. Tokens hit EFS within seconds of Nango refreshing them.

**Critical detail**: Nango's webhook retry budget is **modest** — verified at 2 retries with exponential backoff (100ms initial delay) and a 20-second timeout per attempt. That's roughly 600ms-1s of total retry runway before Nango gives up. Webhook delivery failures are common-ish at this budget (any backend restart, transient network blip, slow handler all consume it).

**This is what motivates the provision-gate reconciliation** — it's not "defense in depth," it's the actual primary fallback. `ensure_user_ready` runs on every `/container/status` poll (cadence ~3s while the chat UI is open). On each call, if credential freshness for this owner is older than a threshold (default 1h, configurable), it lists all the owner's Nango connections in one call and patches anything that's drifted. Missed webhooks are caught within seconds of the user trying to chat.

**No periodic safety poll.** Project preference is to avoid adding new periodic loops; `ensure_user_ready` is the natural event for "verify credentials are fresh." Reconciliation cost is bounded by the freshness timestamp (skip if last reconcile < threshold).

**Rejected:** an hourly cron-task safety poll. Provision-gate reconciliation covers the same failure mode with better latency-to-correctness (seconds when the user chats, vs up to an hour with a cron) and zero new periodic load.

### D4 — Manifest declaration: `requires.credentials` block

Marketplace seller manifests declare credential requirements explicitly. The platform handles provisioning; sellers write zero auth code. Bundles with hardcoded tokens or direct OAuth calls are rejected by the safety scanner at publish time.

```yaml
# Example manifest fragment
name: atlas-recruiter
requires:
  credentials:
    - provider: greenhouse
      reason: "Atlas posts jobs and tracks applicants in your Greenhouse account"
      scopes: [jobs.write, candidates.read]
      optional: false
    - provider: slack
      scopes: [chat:write, im:write]
      optional: true   # agent degrades gracefully if not connected
```

Schema lives in `apps/backend/schemas/marketplace.py`. Pre-deploy validator (in `marketplace_service.deploy`) checks which providers the buyer has already connected, blocks deploy + surfaces a Connect step if any required provider is missing.

### D5 — Provider catalog: `providers.yaml` source of truth

`providers.yaml` is the bridge documented in detail in the "Where credential conventions come from" section above. Recap:

- Each entry maps a Nango provider key (e.g., `slack`) to one or more delivery shapes (`mcp_env`, `agent_env`, `openclaw_paths`, `materializer`).
- Values are dotted paths into Nango's `credentials` / `connection_config` payload, or a sentinel like `materialized_path` for file-based credentials.
- CI enforces that every provider listed in any deployed marketplace manifest, AND every provider declared by any installed skill's `requires.config`, has an entry here.
- Adding a new provider is a PR: register the OAuth app at the provider (one-time, per-env), add the catalog entry, write a materializer if the credential is file-based.
- See full examples in "The four delivery shapes" above.

### D6 — Single-tenant blast radius, attribution via `tags`

One Nango deployment per env (dev/prod), application-layer multi-tenancy via the `tags` object on each connection. Matches the Paperclip pattern (one shared service per env, app-layer isolation). Per-tenant Nango is operationally untenable at projected scale.

**Tenancy model** (verified): the legacy `end_user` and `organization` fields on Nango connections were deprecated. Connections now carry a flexible `tags` object as the source of truth for attribution. We set `tags.end_user_id = <isol8 owner_id>` when creating Connect sessions; webhook payloads include the same `tags` object, which is how the handler resolves which Isol8 owner an event belongs to. Other useful tags we can set: `end_user_email`, `organization_id` (for org-scoped owners), and any custom keys.

Backend→Nango calls use a secret key from Secrets Manager (the `Authorization: Bearer <secret_key>` header). This key has full read access to all connections in the environment; we treat it as a control-plane credential, never expose it to containers, and rotate yearly. Per-owner scoping is enforced at the application layer by always passing `provider_config_key` + filtering returned connections by `tags.end_user_id`. An exfiltrated key would expose all owners' connections in that env — same risk class as exfiltrating our DDB IAM credential, mitigated the same way (VPC-only access, no public endpoint).

## What this doesn't cover (deferred to v2+)

These are real problems but they don't gate v1 shipping. Each is genuinely scoped out, not just deprioritized:

- **Multi-account per provider:** Gumloop nails this (one customer with multiple Slack workspaces). Manifest extension + UI for choosing-on-deploy. v2.
- **BYO OAuth clients per customer:** Pipedream pattern for enterprise (customer ships to *their* end users with their OAuth client_id). v1 ships platform-registered OAuth apps only.
- **Per-call audit at the API layer:** would require brokered-proxy posture, incompatible with §17 acceptance criterion ("works for all four primitives"). Architectural decision, not deferral.
- **Hard rebrand of Connect UI** (logo replacement, custom CSS): Nango Connect UI gives us theme + color + footer-removal. If branding becomes a constraint, build a custom Connect flow via Nango's headless auth path.
- **Proprietary-CLI auth flows:** tools like `gog` that have internal credential stores not exposed via env or files. v1 prefers CLIs with documented env/file conventions (`gcloud` over `gog`). Revisit if a critical provider has no alternative.
- **Customer-self-service "Add a provider":** if a customer needs a CLI/provider we haven't catalogued, they reach out and we add it (1-2 hour internal PR per provider). Standard industry pattern (Zapier, Make, n8n). v2 if demand justifies the self-service UX.
- **AWS finer-grained permissions UI:** v1 ships with broad customer-supplied policy (default `ReadOnlyAccess`). v2 adds a permission-scoping wizard.

**Everything else is in the plan** — tier 2 Jarvis (M6), AWS cross-account (M7), marketplace integration (M5), hardening (M8). No separate doc, no separate work stream.

## File structure

### CDK (new stack)

```
apps/infra/lib/stacks/
└── credentials-stack.ts          # IsolCredentialsStack: Nango infra
                                  # - Aurora Postgres Serverless v2 (Nango DB, KMS-encrypted, 7d PITR)
                                  # - ElastiCache Redis (Nango queue/cache)
                                  # - ECS Fargate service: nango-server (pinned image)
                                  # - Internal ALB (no public listener)
                                  # - DDB: isol8-{env}-credential-freshness (per-owner reconcile timestamps)
                                  # - Secrets Manager: NANGO_ENCRYPTION_KEY, NANGO_WEBHOOK_SECRET, ISOL8_CRED_SERVICE_TOKEN
                                  # - CloudWatch dashboard + alarms
                                  # - Public Lambda for OAuth callback (connect.isol8.com → internal ALB)

apps/infra/lib/
└── isol8-stage.ts                # Modify: instantiate IsolCredentialsStack per env
```

### Backend (new files)

```
apps/backend/core/services/credentials/
├── __init__.py
├── providers.yaml                # Nango provider key → openclaw.json path map
├── providers_loader.py           # Loads + validates providers.yaml at import time
├── nango_client.py               # Thin httpx wrapper for Nango REST API (list conns, get conn, revoke)
├── apply_connection.py           # Core: render providers.yaml + call config_patcher
├── materializers/
│   ├── __init__.py
│   ├── _base.py                  # Materializer protocol + path conventions
│   └── gcloud_adc.py             # First materializer (writes /run/isol8/gcloud/adc.json)
└── reconcile.py                  # ensure_user_ready hook: freshness check + one-shot reconcile

apps/backend/routers/
├── connections.py                # New: /api/v1/connections/* (list, connect, revoke)
└── webhooks.py                   # Modify: add /webhooks/nango (HMAC-verified)

apps/backend/schemas/
└── marketplace.py                # Modify: add CredentialRequirement, ProviderRef

apps/backend/core/services/
├── marketplace_service.py        # Modify: deploy() calls credentials pre-validator
└── marketplace_safety.py         # Modify: extend scanner with 3 credential rules

apps/backend/core/services/
└── user_provision.py             # Modify: ensure_user_ready calls reconcile.maybe_reconcile

apps/backend/core/repositories/
└── credential_freshness_repo.py  # New: per-owner reconcile-timestamp lookups (DDB)
```

### Frontend (new files)

```
apps/frontend/src/app/settings/connections/
└── page.tsx                      # Connections list + Connect/Disconnect buttons

apps/frontend/src/components/settings/
├── ConnectionsList.tsx           # Renders connected providers + freshness + status
├── ConnectButton.tsx             # Opens Nango Connect UI (popup or iframe)
└── ConnectionStatusBadge.tsx     # Green/yellow/red dot based on Nango connection state

apps/frontend/src/hooks/
└── useConnections.ts             # SWR-wrapped /api/v1/connections fetch + mutate

apps/frontend/src/lib/
└── nango.ts                      # Nango Connect UI SDK init (publicKey, hostUrl)
```

### Tests (new)

```
apps/backend/tests/credentials/
├── test_providers_loader.py      # YAML schema validation; CI gate
├── test_apply_connection.py      # Render-and-patch unit tests with mock Nango payloads
├── test_nango_webhook.py         # HMAC verify, owner resolution, idempotency
├── test_reconcile.py             # Freshness check + skip-when-fresh behavior
├── test_materializer_gcloud.py   # ADC JSON write + chown
└── test_marketplace_safety_credentials.py   # 3 new scanner rules
```

## Implementation Milestones

Each milestone produces working, testable software on its own.

### M1: Nango infra (CDK + first provider OAuth app)

**Goal:** Nango deployed in dev; one provider (Slack) registered with BYO OAuth app; manual `curl` from a one-off ECS task fetches a token.

**Verified Nango infrastructure requirements:**

Nango self-hosted is composed of five Node services in production (Server, Orchestrator, Jobs, Runner, Persist). For lower-scale deployments, the public `nangohq/nango-server:hosted` Docker image bundles them as one process — that's what the official `docker-compose.yaml` uses. For v1 we deploy the bundled image as a single Fargate task; we split when scaling demands it.

Required data stores:
- **Postgres** — Aurora Postgres Serverless v2 is fine. Stores connections, credentials, integration configs, schedules. Sizing per Nango: 2 CPU, 8 GB RAM, 128 GB storage at production scale.
- **Redis** — caches socket state, refresh locks, rate limit counters. ElastiCache Redis serverless or `cache.t4g.small` baseline.
- **S3** — holds compiled integration code (~500 MB max). One bucket, scoped to the Nango task role.
- **Elasticsearch** — OPTIONAL. Only needed if `NANGO_LOGS_ENABLED=true` for log search. Skip in v1.

Key env vars (NOT exhaustive — pull current list from `.env.example` in the Nango repo when implementing):
- `NANGO_DATABASE_URL` or `NANGO_DB_HOST` / `NANGO_DB_USER` / `NANGO_DB_PASSWORD` / `NANGO_DB_NAME`
- `NANGO_ENCRYPTION_KEY` — base64-encoded 256-bit key, at-rest encryption for credentials in Postgres; never rotates outside incident response
- `NANGO_SERVER_URL` — public URL of our Nango instance; the frontend SDK reads this to know where Connect UI lives
- Redis connection vars
- Webhook signing key (a separate secret per environment)
- BYO OAuth `client_id` / `client_secret` per integrated provider (one set per provider per env)