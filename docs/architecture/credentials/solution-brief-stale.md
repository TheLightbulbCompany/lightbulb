# Isol8 Managed Credentials Layer — Architecture Brief

**Author:** Claude (research synthesis)
**Date:** 2026-05-21
**Status:** Draft for engineering review

---

## Verification note (read first)

OpenClaw internals verified against the clone at `~/Desktop/openclaw` (`origin git@github.com:openclaw/openclaw.git`, HEAD `510628ca6f`, 2026-05-21). Five upstream refs confirmed with `gh`:

| Ref | State | Verified content |
|---|---|---|
| Issue [#71593](https://github.com/openclaw/openclaw/issues/71593) | OPEN, updated 2026-05-20 | "[Feature]: support to use plugin to implement secret ref provider and cover core schemas" — the parent feature issue |
| PR [#72548](https://github.com/openclaw/openclaw/pull/72548) | OPEN, no review decision, updated 2026-05-20, author `akoscz` | `feat(secrets): pluggable SecretRef sources via plugin SDK seam`. Ships `contracts.secretProviders: string[]` manifest field, new SDK subpath `openclaw/plugin-sdk/secret-provider`, lazy loader at `src/plugins/secret-provider-public-artifacts.ts`, dispatch in `src/secrets/resolve.ts`, and two reference plugins (`extensions/secrets-gcp/`, `extensions/secrets-keyring/`). **Critically: the "broader SecretRef field-coverage" half is explicitly deferred** — `skills.entries.*.env`, `plugins.entries.*.config`, and `mcp.servers[].env` are *not* opened to accept SecretRef objects in this PR. |
| PR [#82326](https://github.com/openclaw/openclaw/pull/82326) | OPEN, **APPROVED**, updated 2026-05-20 | "Add plugin manifest contract for SecretRef provider integrations" — companion/precursor work |
| PR [#23096](https://github.com/openclaw/openclaw/pull/23096) | OPEN, no review decision, updated 2026-05-21 | `feat(secrets): add Bitwarden/Vaultwarden secret provider` — a specific provider on the seam, not the seam itself |
| Issue [#76493](https://github.com/openclaw/openclaw/issues/76493) | OPEN, updated 2026-05-15 | "Allow SecretRef objects in `mcp.servers[].env` schema (currently `string\|number\|boolean` only)" — **this is the actual unblocker for retiring our env-file injection**, not #72548 |

MCP spawn surface verified at `src/agents/mcp-stdio-transport.ts:50-62`:

```ts
const baseEnv = {
  ...getDefaultEnvironment(),                    // MCP SDK curated env (PATH, USER, HOME, ...)
  ...this.serverParams.env,                      // explicit per-server env from config
};
const preparedSpawn = prepareOomScoreAdjustedSpawn(...);
const child = spawn(preparedSpawn.command, preparedSpawn.args, {
  env: preparedSpawn.env,
  shell: false,
  ...
});
```

**This invalidates the naive env-baseline assumption.** OpenClaw does *not* pass full parent env to spawned MCPs — it uses the MCP SDK's `getDefaultEnvironment()`, a minimal curated set, merged with explicit per-server `env`. The brief's design now reflects this: §2.3 ships **per-MCP wrapper scripts** rather than relying on parent-env inheritance, and §6.2 makes clear that retiring this requires *both* PR #72548 *and* issue #76493 landing — the SDK seam alone is insufficient.

`auth.profiles` machinery verified: referenced from `src/config/plugin-auto-enable.shared.ts:483` (`cfg.auth?.profiles`), `src/config/schema.labels.ts:537`, dedicated module at `src/agents/auth-profiles/`. Per repo `CLAUDE.md`: "model auth profiles in `~/.openclaw/agents/<agentId>/agent/auth-profiles.json`" — confirms `auth.profiles` is LLM/model auth only, as your prompt stated. Our credential layer stays out of that path.

(An earlier verification pass was overly skeptical about the existence of the repo itself, before the clone was located on disk. Corrected.)

---

## 0. First-principles framing — do we even need this?

**Yes, but only the parts that are specifically Isol8's problem to solve.** Decompose the credential need into three buckets:

| Bucket | Already handled where | Isol8 needs to build? |
|---|---|---|
| **LLM provider auth** (Anthropic, Bedrock, OpenAI) | OpenClaw `auth.profiles` machinery (your stated constraint) | No — out of scope |
| **Static API keys** (Stripe secret, Resend, internal API tokens) | AWS Secrets Manager + Fernet-encrypted DDB (your existing) | No — already done |
| **End-customer's third-party OAuth tokens** (their Slack, their Salesforce, their GitHub) | **Nothing today** — OpenClaw assumes single-user-on-own-machine, OS keychain | **Yes — this brief** |

**The pain point in plain English:** an Isol8 customer says "deploy Atlas the recruiter and connect it to *my* Greenhouse account so it can post jobs *as me*." The customer should click "Connect Greenhouse," authenticate once in a Greenhouse OAuth screen branded as Isol8, and never think about tokens again. Atlas's tools — a Greenhouse MCP server, a `greenhouse` CLI plugin, a custom skill that calls Greenhouse's REST API — must all transparently get a fresh token at runtime without the agent author having written any auth code.

**Why doesn't OpenClaw solve this natively?** Because OpenClaw is designed for one user on their own machine. The OS keychain is fine when the credential's owner is also the human at the keyboard; it is the wrong abstraction when the credential's owner is `owner_id=abc123` in your DB and the runtime is an ECS task they will never SSH into. This is a deployment-shape mismatch, not a missing feature in OpenClaw — and upstream PRs (#72548 et al, per your prompt) confirm the community is aware but the work is months out.

**Why not wait for upstream?** Two reasons. (1) The category timing window — Anthropic Managed Agents and Gumloop have already shipped credential layers; positioning Isol8 as "the open managed-agent platform" requires shipping this in weeks not quarters. (2) Even if PR #72548 lands tomorrow, it will give you a *plugin seam* (SecretRef), not a turnkey OAuth platform — you still need the vault, the consent flow, the refresh worker, the white-label Connect UI. Those are 90% of the work; the seam is 10%. Build the 90% now against today's OpenClaw surface; swap to the seam when it lands.

**What we are *not* solving:** revocable end-to-end audit at the API-call layer (that's a brokered-proxy posture; see §2.7 for why we explicitly do *not* go there). Per-call scope downscoping. Cross-tenant credential sharing. These are out-of-scope v1.

---

## 1. Recommended platform: Nango (self-hosted)

**Pick Nango.** It is the only platform that simultaneously satisfies all four hard constraints: acceptable license, **complete** self-host story (control plane + token vault, not just SDKs or per-server containers), true BYO OAuth client per provider, and a server-side `GET /connection/{id}` REST endpoint that drops cleanly into env injection.

### The five-way comparison (compressed)

| Dimension | **Nango** | Klavis AI | Composio | Arcade.dev | Pipedream Connect |
|---|---|---|---|---|---|
| License | ELv2 (OK — embed allowed) | Apache-2.0 | MIT SDK / **closed backend** | Closed (MIT framework only) | Closed SaaS |
| Self-host full stack on AWS | **Yes** (compose + Helm) | Partial (per-MCP-server only) | No | Enterprise K8s only | No |
| Integrations | 800+ | 100+ | 1,000+ (cloud) | 7,500 tools / 81 servers | 3,000+ |
| BYO OAuth client per provider | **Yes** | Unclear | Cloud only | Yes | Yes (added 2024-06) |
| Multi-tenant namespacing | `connection_id` | `user_id` (partial docs) | `user_id` (cloud) | tenant (enterprise) | `external_user_id` |
| White-label Connect UI | **Yes** | Unclear | Cloud only | Partial | Yes (SDK widget) |
| GH commits / 30d | **289** | 10 | 374 (SDK churn) | 15 (framework) | n/a |
| Token-fetch model | REST API → your env | Per-server | Brokered proxy | Runtime-mediated | Proxy *or* retrieval API |

### The deciding tradeoffs

1. **Composio is disqualified despite its MIT label.** Only the SDK is MIT. Running the actual OAuth/token system requires their hosted backend — your customers' tokens would transit a third party, violating self-host. (The community Docker Compose hacks in issue #291 are not an official product, open since July 2024 with no team commitment.)
2. **Pipedream is operationally excellent but architecturally wrong** here: no self-host at all, and the Workday acquisition (announced 2025-11-19) introduces real roadmap risk for an embedded dependency.
3. **Arcade's runtime-mediated model fights your design.** Arcade's primitive is "the runtime makes the API call so tokens never touch the agent." You want tokens *in the container env* so CLIs and arbitrary SDKs work. You'd be paying for execution infrastructure you actively don't use.
4. **Klavis is the cleanest license but the weakest ship.** Apache-2.0 + MCP-native is strategically the right posture, but the full-control-plane self-host story isn't shipped, and 10 commits/30d (vs Nango's 289) means it won't be in the relevant timeframe. Revisit in 6-12 months as a possible migration target.
5. **Nango's ELv2 constraint only bites if you ever pivot to "we offer managed Nango for other companies"** — which you won't, because Nango is internal infrastructure for Isol8.

### Where Nango is weak and we accept it

- Token-refresh SLA is not publicly published (treat as best-effort; mitigate with our own freshness check on read, §2.4).
- No native MCP-server-spawning capability — Nango is a credential platform, not an MCP host. We adapt by reading tokens out of Nango and injecting them into the OpenClaw-spawned process (which is exactly what we want — Nango stays narrow).
- ELv2 vs Apache-2.0 is a step down. The carve-out is documented and aligns with our use case, but it's not zero risk if AWS/Elastic-style licensing skirmishes recur.

---

## 2. End-to-end design — the env-baseline pattern

### 2.1 The architecture in one sentence

A small init binary runs at container start (and on a 25-minute refresh cadence) that pulls the current owner's connected providers from a Isol8-hosted Nango instance, writes canonical env vars into `/run/isol8/env/credentials.env`, and signals long-running children (MCP servers, watchers) to reload when their tokens change.

### 2.2 The init binary: `isol8-credinit`

**Language:** Go. Two reasons: (a) static binary, no runtime dependency in the OpenClaw image; (b) the Printing Press CLIs are Go too, so one toolchain. Single binary, ~8MB compressed, distributed via the Isol8 base image layer.

**Lifecycle in the container:**

```
ECS Task starts
  └─ entrypoint.sh
       ├─ exec /usr/local/bin/isol8-credinit --mode=bootstrap
       │     (blocks; exits 0 only when initial env file is written)
       ├─ exec /usr/local/bin/isol8-credinit --mode=refresh --daemon &
       │     (long-running; writes new env files + signals reloaders)
       └─ exec openclaw start
            (env file sourced via systemd-style EnvironmentFile mechanism;
             see §2.3 for why we don't just `source` it)
```

**Bootstrap call:**

```
GET https://nango.internal.isol8/api/v1/connection?provider_config_key=*&end_user_id={owner_id}
Authorization: Bearer {ISOL8_CRED_SERVICE_TOKEN}
```

The service token is an IAM-scoped credential, mounted via ECS task role, valid only inside the VPC. The Nango instance never sees a direct call from the public internet.

**On success:** writes `/run/isol8/env/credentials.env` atomically (`O_TMPFILE` + rename) with 0640 perms, owned by `root:isol8`.

**On failure:** exits non-zero only in `--mode=bootstrap`. In `--mode=refresh`, logs and retries with exponential backoff (1s, 2s, 4s, 8s, 30s ceiling). Never crashes the container — token expiry should degrade tools gracefully, not nuke the agent.

### 2.3 Env file + per-tool wrappers (not raw env inheritance)

**The verified upstream behavior changes the design from the obvious one.** Reading `src/agents/mcp-stdio-transport.ts:50-62`:

```ts
const baseEnv = {
  ...getDefaultEnvironment(),                    // MCP SDK curated env only
  ...this.serverParams.env,                      // explicit per-server env from config
};
const child = spawn(preparedSpawn.command, preparedSpawn.args, { env: preparedSpawn.env, ... });
```

OpenClaw does **not** pass full parent env to MCP children. It uses the MCP SDK's [`getDefaultEnvironment()`](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/src/client/stdio.ts) — a deliberately minimal whitelist (PATH, USER, HOME, SHELL, LOGNAME, PWD, LANG, TERM, and a few others) — merged with whatever's in `mcp.servers[].env`. Issue [#76493](https://github.com/openclaw/openclaw/issues/76493) confirms `mcp.servers[].env` currently accepts only `string|number|boolean` literals, so we can't reference our env file with a placeholder like `${SLACK_BOT_TOKEN}` either (or at least, not portably — even if interpolation works today, the schema doesn't promise it).

That kills the "init binary writes process env, OpenClaw spawns children that inherit it" design. Three options re-evaluated against verified behavior:

| Option | Verdict |
|---|---|
| `os.Setenv` from init, then `exec` openclaw, hope MCP inherits | **Doesn't work** — `getDefaultEnvironment()` filters parent env to a whitelist; `SLACK_BOT_TOKEN` etc. are dropped. |
| Literal values in `mcp.servers[].env` at OpenClaw config-load | **Doesn't work** — values are static; can't refresh on rotation. |
| **Per-tool wrapper scripts that source the env file at spawn time** | **Chosen.** Wrappers re-read `/run/isol8/env/credentials.env` then `exec` the real binary. Refreshed tokens reach children on next spawn for free. |

**The wrapper pattern.** For every MCP server, CLI, or plugin binary that needs credentials, the OpenClaw config (or the marketplace bundle) points at a wrapper, not the underlying binary:

```jsonc
// what's in openclaw.json (or equivalent):
"mcp.servers": {
  "greenhouse-mcp": {
    "command": "/usr/local/bin/isol8-tool",
    "args": ["greenhouse-mcp-server", "--stdio"]
  }
}
```

The single wrapper binary `isol8-tool`:

```bash
#!/bin/sh
# Resolves to /usr/local/bin/isol8-tool, owned root:isol8, 0755.
# Sources the cred env file (file is 0640 root:isol8; wrapper is setgid isol8 so it can read).
set -a
. /run/isol8/env/credentials.env
set +a
exec "$@"
```

Why a wrapper rather than a per-server shell snippet:
- One audited binary, one place to add scrubbing/logging.
- Easy to extend with materializer invocation (write `~/.config/gh/hosts.yml` on demand) before `exec`.
- Marketplace seller manifests just say `command: isol8-tool, args: [<their binary>, ...]` — no per-bundle bash.

**Three consumer paths under this design:**

1. **OpenClaw's own process at boot** — sourced via `entrypoint.sh` before `exec openclaw`. (Note: most of these env vars are irrelevant to OpenClaw itself; the source happens mainly so OpenClaw can pass them through `mcp.servers[].env` *if* #76493 lands a SecretRef shape we can reference. Until then, OpenClaw doesn't need them in its own process — only the wrappers do.)
2. **MCP servers / CLIs / plugins spawned by OpenClaw** — config always points at `isol8-tool`. Wrapper sources current env file every spawn. Refresh = next spawn picks up new values automatically.
3. **Long-running children that need mid-life refresh** (e.g., a streaming MCP server holding an hour-long SSE connection). Mutating a running process's env is not portable (`/proc/<pid>/environ` is read-only on Linux and absent on macOS), so the practical mechanism is restart-on-SIGHUP. The daemon SIGHUPs registered children when the env file content hash changes; the wrapper's signal handler `exec`s itself, which re-sources the env file and re-spawns the child binary with the new values. v1: treat the restart as a brief reconnect from OpenClaw's perspective — MCP clients already retry on transport close. v2: per-MCP reload protocols if any MCP server defines one (none does today).

**Marketplace impact.** The safety scanner gains a fourth rule (extension of §4.4): every credential-touching tool in a marketplace bundle must declare its `command` as `isol8-tool` (with args). Bundles that bypass the wrapper are rejected. This is the enforcement point that makes "no agent author writes auth code" actually true.

### 2.4 Refresh cadence and mechanism

**Cadence:** 25 minutes. Why: most OAuth access tokens are 1h-1d. 25min gives ≥2 refreshes per shortest typical lifetime, and stays comfortably inside Nango's own background refresh window. Configurable per-provider override for the few that issue short-lived (15min) tokens.

**Mechanism:** time-driven inside the daemon, not cron. Cron in a container is operational tax; a `time.Ticker` in Go is one line and observable.

**Change-detection:** before writing the env file, the daemon hashes the new content. If unchanged, no write, no signal. If changed:

1. Write the new file atomically.
2. Send `SIGHUP` to a documented set of children registered in `/run/isol8/reload-targets/*.pid`. Children that want to be notified write their pid here at startup; children that don't care (most short-lived CLIs) just inherit the new env on next spawn.

**Why not push-based?** Two reasons: (a) Nango doesn't natively push token-refresh events to consumers, and (b) the polling model degrades gracefully under Nango outage (last good tokens stay in the env file).

### 2.5 The canonical env var naming convention

**Source of truth:** a single YAML file in the Isol8 repo, `services/cred-init/providers.yaml`, mapping Nango provider keys to canonical env var names. The init binary embeds this file at build time.

```yaml
# Each entry: nango provider key → list of env vars to write
slack:
  - name: SLACK_BOT_TOKEN
    source: credentials.access_token
  - name: SLACK_TEAM_ID
    source: connection_config.team.id

github:
  - name: GITHUB_TOKEN
    source: credentials.access_token
  - name: GH_TOKEN              # gh CLI's preferred name
    source: credentials.access_token

salesforce:
  - name: SFDX_AUTH_URL
    source: credentials.raw.sfdx_auth_url
  - name: SALESFORCE_INSTANCE_URL
    source: connection_config.instance_url

hubspot:
  - name: HUBSPOT_ACCESS_TOKEN
    source: credentials.access_token

notion:
  - name: NOTION_TOKEN
    source: credentials.access_token

google_workspace:
  - name: GOOGLE_OAUTH_ACCESS_TOKEN
    source: credentials.access_token
  - name: GOOGLE_OAUTH_REFRESH_TOKEN
    source: credentials.refresh_token

greenhouse:
  - name: GREENHOUSE_API_KEY
    source: credentials.access_token
```

**The naming rule:** prefer the de-facto convention used by the provider's own CLI / SDK. `gh` reads `GH_TOKEN`; `aws` reads `AWS_ACCESS_KEY_ID`; `sf` (Salesforce CLI) reads `SFDX_AUTH_URL`. Where the SDK doesn't have a convention, use `{PROVIDER}_ACCESS_TOKEN`. Where multiple SDKs disagree (rare), write **both** vars from the same source.

**Maintenance model:** providers.yaml is reviewed in PRs like any code. New marketplace agents that need a new provider add an entry. The CI check verifies every provider in marketplace manifests has a providers.yaml entry.

### 2.6 The escape hatch: CLIs/SDKs that don't read env vars

Not every tool reads env. Some expect a config file (`~/.config/gh/hosts.yml`), an active login session (`gcloud auth login`), or a TOML/JSON credential at a specific path. For these we ship per-provider **materializers**, small Go functions invoked by `isol8-credinit` after the env file write:

```yaml
# Extension to providers.yaml
gcloud:
  - name: GOOGLE_APPLICATION_CREDENTIALS
    source: materialize.gcloud_adc_json   # writes a file, returns path
    write_path: /run/isol8/gcloud/adc.json
  - name: CLOUDSDK_CORE_PROJECT
    source: connection_config.project_id
```

Each materializer is a named function in `services/cred-init/materializers/`. Adding a new one for a new provider is a 10-20 line Go function + a providers.yaml entry. No agent author ever writes auth code.

**The escape-hatch contract for marketplace authors:** if your tool needs a file-based credential, write the materializer in the marketplace PR and have the cred-init reviewer approve it. No tarballs ship with file-writing logic of their own.

### 2.7 Security model — why env injection over brokered proxy

This is the most-contested architecture choice in this brief. The two options:

| Pattern | Pro | Con |
|---|---|---|
| **Env injection** (chosen) | Works for any tool — CLIs, MCPs, arbitrary SDKs, gRPC, raw HTTP. Zero coupling to protocol. | Tokens visible to every process in the container. Compromise of one tool exfils all of that owner's tokens. |
| **Brokered proxy** (Composio, Pipedream API-proxy, Anthropic Managed Agents) | Tokens never enter runtime. Centralized audit, per-call revocation possible. | Only works for HTTPS APIs the proxy understands. Breaks CLIs that aren't HTTPS-API-call-shaped (git push? aws s3 cp?). Breaks SDKs that talk to undocumented endpoints. |

**We pick env injection because the tool-primitive surface we have to support (MCP, CLI, plugin, skill) cannot be uniformly proxied.** The Printing Press CLIs in particular are arbitrary Go binaries that may shell out to other tools — there is no proxy that catches "binary X invokes binary Y which makes a TCP connection to Z." Env injection is the only pattern that's primitive-agnostic.

**Blast radius:** if a container is compromised, the attacker exfils that one owner's tokens. **Structurally guaranteed by:** (a) one container per owner, (b) the cred-init service token is scoped to `end_user_id={owner_id}` server-side at Nango, so even if exfiltrated it cannot list other owners' connections, (c) network egress controls (see CDK §3) prevent the container from reaching Nango's admin API.

**What customers' security teams will ask:**

| Q | A |
|---|---|
| Where do tokens live at rest? | Nango Postgres, encrypted with KMS-backed AES-256. Nango's own DEK is rotated quarterly. |
| Where in transit? | TLS 1.3 between init binary and Nango, intra-VPC only. Never crosses public internet. |
| Who can see tokens at the consumer? | Any process running as `openclaw` user in *that owner's* container. The container is dedicated per owner, never shared. |
| How do you revoke a token mid-incident? | Two paths: (a) Nango admin API deletes the connection — env file is re-fetched at next refresh tick and the var is absent. (b) Force ECS task restart — bootstrap fetches the now-revoked-empty state immediately. RTO ≤ 30s. |
| What if the cred-init binary is malicious? | Built from a pinned Isol8 image layer, signed with cosign, image-scanning gates the deploy. Same supply-chain posture as the rest of the platform. |
| What audit logs do you have? | Every fetch from cred-init → Nango is logged at Nango. Every connection grant/revoke is logged. Tool-level audit (which agent called which API) is on the OpenClaw side, out of scope here. |
| Cross-tenant blast radius? | Zero, by the one-container-per-owner invariant. Nango's tenant-scoped service token prevents lateral movement even if the init binary is compromised. |
| Token rotation under incident? | Documented runbook: rotate the Nango per-connection token (re-OAuth), then bounce affected ECS tasks. Or globally: rotate the cred-init service token, redeploy all tasks. Both ≤ 15min. |

**The thing we are accepting:** we are *not* going to claim "tokens never enter the runtime." We are claiming "the runtime is single-tenant, the blast radius is the smallest unit of business meaning (one owner), and we have fast revoke." That posture is the same one Sema4.ai documents publicly for their Control Room Vault, and is the same shape every Kubernetes-Secrets-as-env-vars deployment in production today is running.

---

## 3. CDK stack — `IsolCredentialsStack`

**Architectural parallel** to your existing `PaperclipStack` (Paperclip = one shared service per env, multi-tenant at the app layer). The credential stack follows the identical shape — one Nango deployment per env, owner-scoped via `connection_id`.

### 3.1 Stack components

```
IsolCredentialsStack
├─ VPC reference (shared from network stack)
├─ RDS Aurora Postgres Serverless v2 (Nango's DB)
│   ├─ multi-AZ
│   ├─ KMS-encrypted (CMK from network stack)
│   ├─ 7-day PITR
│   └─ secret in Secrets Manager, rotated 30d
├─ ElastiCache Redis (Nango's queue/cache)
│   └─ single-AZ in dev, multi-AZ in prod
├─ ECS Fargate service: nango-server
│   ├─ task definition pinned to nangohq/nango-server:<sha256>
│   ├─ desired count 2 (prod) / 1 (dev)
│   ├─ ALB target on internal subnet
│   ├─ task role: read DB secret, write to S3 audit bucket
│   └─ env from Secrets Manager: NANGO_ENCRYPTION_KEY, OAUTH_CLIENT_*
├─ ALB (internal, no public listener)
│   └─ host header route: nango.internal.isol8 → nango-server target group
├─ DDB table: isol8-nango-provider-config-snapshots
│   ├─ pk: owner_id, sk: provider#timestamp
│   ├─ TTL on stale snapshots (90d)
│   └─ used only by the marketplace deploy validator (§4) for fast "does this
│      owner have provider X connected?" lookups without round-tripping Nango
├─ Secrets Manager: isol8-cred-service-token (IAM-scoped to per-owner-task roles)
└─ CloudWatch + X-Ray:
    ├─ alarms: nango 5xx rate, refresh failure rate, DB connection errors
    └─ dashboard: connections per owner, refresh latency p99, token age distribution
```

### 3.2 Networking diagram

```
                ┌──────────────────────────────────────────────┐
                │            VPC: isol8-prod                   │
                │                                              │
   Internet     │   public subnet                              │
       │        │   ┌──────────────────┐                       │
       │        │   │ Public ALB       │                       │
       ▼        │   │ (chat.isol8.com) │                       │
   ───────────► │   └──────┬───────────┘                       │
   Customer     │          │                                   │
   browser      │   ┌──────▼──────────────────┐                │
       │        │   │   FastAPI backend (ECS) │                │
       │        │   └──────┬────────┬─────────┘                │
       │        │          │        │                          │
       │        │   private subnet  │                          │
       │        │          │        │                          │
       │        │   ┌──────▼───┐    │   ┌──────────────────┐   │
       │        │   │ Nango    │◄───┼──►│ Per-owner ECS    │   │
       │        │   │ ALB      │    │   │ task (OpenClaw)  │   │
       │        │   │ internal │    │   │ + cred-init      │   │
       │        │   └──┬───────┘    │   └──────────────────┘   │
       │        │      │            │                          │
       │        │   ┌──▼────────┐   │                          │
       │        │   │ Nango ECS │   │                          │
       │        │   │ + Aurora  │   │                          │
       │        │   │ + Redis   │   │                          │
       │        │   └───────────┘   │                          │
       │        │                                              │
       └────────┼─── (oauth callback) ──┐                      │
                │                       │                      │
                │   ┌───────────────────▼──┐                   │
                │   │ Connect UI (Lambda   │                   │
                │   │ behind public ALB)   │                   │
                │   └──────────────────────┘                   │
                └──────────────────────────────────────────────┘
```

**Key invariants:**
- Per-owner OpenClaw tasks reach Nango via the **internal** ALB only — VPC security groups prevent any other path. There is no public Nango endpoint.
- The OAuth callback URL is `https://connect.isol8.com/callback`, served by a thin public Lambda that forwards to Nango's callback handler over the internal ALB.
- The Nango admin API (where you'd manage provider configs) is reachable only from a bastion role; the per-owner task role has connection-scoped read only.

### 3.3 Secret management

- **OAuth `client_id`/`client_secret` per provider** (one per provider, per env) — stored in Secrets Manager, mounted into the Nango task via ECS secrets. Rotated when revoked at the provider; cadence policy: review yearly.
- **`NANGO_ENCRYPTION_KEY`** — Nango's own at-rest key. Stored in Secrets Manager, *never* rotated except in incident response (rotating it requires re-encrypting the entire token vault — Nango supports this but it's not zero-downtime).
- **`ISOL8_CRED_SERVICE_TOKEN`** — the bearer the cred-init binary uses to call Nango. Long-lived (90d), rotated on a calendar via a Lambda + GitHub Actions PR. Per-tenant scoping is achieved by Nango-side ACL on `end_user_id`, not by per-tenant tokens.

---

## 4. Marketplace integration

### 4.1 Manifest schema extension

Add to the marketplace agent manifest (`agent.yaml` or equivalent):

```yaml
name: atlas-recruiter
version: 1.4.0
publisher: isol8

requires:
  credentials:
    - provider: greenhouse
      reason: "Atlas posts jobs and tracks applicants in your Greenhouse account"
      scopes: [jobs.write, candidates.read]
      optional: false

    - provider: slack
      reason: "Atlas DMs hiring managers when a strong candidate applies"
      scopes: [chat:write, im:write]
      optional: true   # agent degrades without it, doesn't fail

    - provider: google_workspace
      reason: "Schedule interview slots on hiring managers' calendars"
      scopes: [calendar.events]
      optional: false

tools:
  - kind: mcp
    name: greenhouse-mcp
    command: greenhouse-mcp-server
    # No `env:` block — env-baseline handles all credentials.
  - kind: cli
    name: slack-poster
    binary: ./bin/slack-poster
  - kind: skill
    name: interview-scheduling
```

**Rules:**
- Every provider listed in `requires.credentials` must have a corresponding entry in `services/cred-init/providers.yaml`. CI fails the marketplace PR otherwise.
- Bundled tools (`tools[]`) must **not** declare `env:` blocks for credentials. The safety scanner flags any tool config that does (§4.4).
- `scopes` are advisory in v1 — used to display "this agent will use the following permissions" in the deploy UI, but not enforced at the OAuth flow (BYO OAuth clients have fixed scopes per Isol8's OAuth app registration).

### 4.2 Pre-deploy validation

When a customer clicks "Deploy Atlas":

```
1. Backend reads atlas/1.4.0/agent.yaml → required providers
2. Backend queries DDB isol8-nango-provider-config-snapshots for owner_id:
   "which of [greenhouse, slack, google_workspace] are already connected?"
3. If all required (non-optional) are connected → deploy proceeds, env file
   includes those provider's vars on next refresh.
4. If any required are missing → UI shows "Connect Missing Services" panel
   with one Connect button per missing provider.
5. Each Connect button opens Nango Connect UI in popup; Nango writes the new
   connection; backend invalidates the DDB snapshot; user can retry deploy.
6. Optional missing services don't block deploy — surfaced as "Connect for
   full capability" badges in the post-deploy agent page.
```

### 4.3 The Connect UI itself

White-labeled Nango Connect UI mounted at `connect.isol8.com`. Logo, colors, copy match Isol8's main app. The OAuth consent screen (rendered by Slack/Salesforce/etc.) shows **Isol8's** registered OAuth app (because we BYO the client per provider). Customer sees a coherent flow: "Connect to Greenhouse → Greenhouse asks 'authorize Isol8?' → done."

### 4.4 Safety scanner extensions

Extend `marketplace_safety.scan_tarball` with three new rules:

```python
class CredentialSafetyRules:
    def scan(self, tarball: Path) -> list[Finding]:
        findings = []
        findings += self._reject_hardcoded_tokens(tarball)
        findings += self._reject_direct_oauth_calls(tarball)
        findings += self._reject_undeclared_provider_use(tarball)
        return findings

    # Rule 1: any file matching credential-shaped regex (slack xoxb-, github
    # ghp_, AWS AKIA, generic JWT, RSA private key) → reject with location.
    def _reject_hardcoded_tokens(self, tarball): ...

    # Rule 2: any tool config containing env: blocks naming a known credential
    # var (cross-referenced against providers.yaml) → reject; credentials must
    # come from env-baseline, not bundle.
    def _reject_direct_oauth_calls(self, tarball):
        # Also flag direct calls to provider OAuth endpoints — agents that
        # try to run their own OAuth dance bypass the platform's vault.
        OAUTH_ENDPOINT_PATTERNS = [
            r"https://slack\.com/oauth/v2/access",
            r"https://github\.com/login/oauth/access_token",
            r"https://login\.salesforce\.com/services/oauth2/token",
            # ... maintained list, same source as providers.yaml
        ]
        ...

    # Rule 3: any tool that reads a provider env var not declared in the
    # manifest's requires.credentials → reject. (Caught by static analysis
    # of Go binaries via go-symbols; for skills, by import scan.)
    def _reject_undeclared_provider_use(self, tarball): ...
```

**Rule 2 is the important one** — it prevents a marketplace seller from sneaking in their own OAuth implementation that exfils tokens.

### 4.5 Seller-authored custom tools

Seller bundles a CLI? Two paths:
- **Reads standard env (preferred):** seller declares the provider in `requires.credentials`, the env-baseline mechanism does the rest. Zero seller code for auth.
- **Needs file-based creds:** seller files a "new materializer" PR against `services/cred-init/materializers/` *separately* (not in the bundle). Approved through normal review. Once merged, sellers can reference the materializer in their manifest.

Sellers never ship credential-handling code in their tarball. The safety scanner enforces this.

---

## 5. Implementation plan — 2-week milestones

### M1 (weeks 1-2): Nango self-hosted up in dev

- CDK stack: VPC reference + RDS Aurora + Redis + ECS Fargate + internal ALB
- Deploy `nangohq/nango-server` pinned tag, with sane defaults
- Register first two providers (Slack, GitHub) with BYO OAuth apps in dev
- Manually grant one test connection via Nango admin API; smoke-test token fetch
- **Exit criterion:** can `curl` Nango from a one-off ECS task and get back a Slack token

### M2 (weeks 3-4): `isol8-credinit` binary v1

- Go binary, embedded providers.yaml, two modes (bootstrap, daemon)
- Atomic env-file write, SIGHUP signaling, error backoff
- Materializer framework with `gcloud` as first reference materializer
- Containerize as a layer on the OpenClaw base image
- **Exit criterion:** new container boots, env file populated, MCP server spawned by OpenClaw can read `SLACK_BOT_TOKEN`

### M3 (weeks 5-6): Connect UI + customer-facing OAuth flow

- White-label Nango Connect UI deployed at `connect.isol8.com`
- Public Lambda for OAuth callback forwarding
- Backend endpoint: list-connected-providers (reads DDB snapshot, falls back to Nango)
- Backend endpoint: invalidate-snapshot (on connection grant/revoke webhook from Nango)
- UI: "Connections" page in customer dashboard
- **Exit criterion:** customer logs into Isol8, clicks "Connect Slack," authenticates, sees green checkmark; their per-owner container now has the token on next refresh

### M4 (weeks 7-8): Marketplace integration

- Manifest schema extension (`requires.credentials`)
- Pre-deploy validation in the deploy endpoint
- "Connect Missing Services" UI in deploy flow
- Safety scanner rules 1-3
- Backfill Atlas + Friday manifests with their declared credentials (even if currently empty)
- **Exit criterion:** deploying a marketplace agent that requires Greenhouse forces a Connect step if Greenhouse isn't already connected

### M5 (weeks 9-10): Production hardening

- Multi-AZ for prod Aurora + ALB + Redis
- Backup/restore drill of Nango Postgres
- Token-rotation runbook + tabletop exercise
- CloudWatch alarms wired to PagerDuty
- Load test: 1000 simultaneous refresh ticks against Nango
- **Exit criterion:** ready for first 50 production owners

### M6 (weeks 11-12): Provider catalog expansion

- Add Salesforce, HubSpot, Notion, Google Workspace, Greenhouse to providers.yaml
- Materializers as needed (Salesforce CLI uses `SFDX_AUTH_URL` env, no file needed; Google Workspace needs ADC JSON materializer)
- Seller documentation + marketplace SDK update
- **Exit criterion:** marketplace agents using top-10 SaaS APIs can ship

### Out of v1 (call out explicitly)

- Per-call audit at the API layer (would require brokered-proxy posture — different architecture, not env injection)
- Cross-tenant credential sharing (e.g., a team-shared Slack workspace) — needs Nango's "shared connections" feature; defer
- SCIM-driven auto-provisioning of connections — defer until enterprise asks

---

## 6. Open risks, dependencies, edge cases

### 6.1 Vendor risk (Nango)

- **License pivot:** Nango is ELv2 today, post-Elastic-relicense playbook. Probability of further restriction: low but nonzero. Mitigation: license clauses pinned at vendored version; fork escape valve documented (the OSS edition is feature-complete).
- **Acquisition:** Nango is YC-backed, ~50 employees. Acquisition by a hyperscaler or competitor would raise roadmap risk. Mitigation: keep our integration surface narrow (we use Nango as a token vault + Connect UI, not as a workflow engine) so a fork or migration to Klavis is a 4-6 week project, not 4-6 months.
- **Upstream-bug exposure:** Nango refresh bug → all customers' tokens stale. Mitigation: cred-init's freshness check (if token has expired_at < now, force a re-fetch before serving).

### 6.2 OpenClaw upstream alignment

**Bind point at MCP spawn — verified, not assumed.** `src/agents/mcp-stdio-transport.ts:50-62` uses `getDefaultEnvironment()` + explicit `serverParams.env`. Parent env inheritance does NOT work. The brief's mechanism is wrappers (§2.3), not env inheritance. This is settled, not a week-1 risk.

**The migration is gated on TWO landings, not one.** This is the most important correction from the original prompt:

| Upstream change | What it gives us | Status |
|---|---|---|
| PR [#72548](https://github.com/openclaw/openclaw/pull/72548) (SDK seam for SecretRef providers) | A clean way to ship a `nango` source plugin that `secrets.providers.myNango = {source: "nango", ...}` can dispatch to | OPEN, no review decision (2026-05-20) |
| Issue [#76493](https://github.com/openclaw/openclaw/issues/76493) (`mcp.servers[].env` accepts SecretRef) | The ability to write `env: { SLACK_BOT_TOKEN: {source: "nango", ...} }` in MCP config and have OpenClaw resolve it at spawn | OPEN (2026-05-15), no PR yet — needs someone to do the work the PR #72548 author explicitly deferred |

PR #72548 alone is insufficient: it ships the dispatcher seam plus two reference providers (GCP Secret Manager, OS keychain), but its body explicitly states that opening `mcp.servers[].env` (and `skills.entries.*.env`, `plugins.entries.*.config`) to accept SecretRef objects is **deferred to a follow-up**. The reason the brief's design ships per-tool wrappers instead of "wait for upstream" is precisely this: the seam without the field-coverage doesn't retire our wrappers.

**Migration path (if BOTH land):**

1. Write a Nango SecretRef provider plugin against the `openclaw/plugin-sdk/secret-provider` SDK published by PR #72548. Modeled on the `extensions/secrets-gcp/` reference plugin shipped in that PR. ~1 week.
2. Bundle the plugin as `extensions/secrets-nango/` in our OpenClaw image layer (we're already deriving from upstream, not forking — this is just an additional plugin).
3. Rewrite our generated `openclaw.json` per-tenant config: `secrets.providers.tenant = {source: "nango", endUserId: "<owner_id>", baseUrl: "https://nango.internal.isol8"}` and `mcp.servers.greenhouse-mcp.env.GREENHOUSE_API_KEY = {source: "nango", provider: "tenant", id: "greenhouse"}`. ~1 week.
4. Retire `isol8-tool` wrapper for MCP servers (CLI/plugin wrappers may stay if those still need file materializers). Wrappers stay for materializer cases (`gcloud` ADC JSON, etc.) since SecretRef is for env vars, not config files. ~3 days.

**Total migration cost: ~2-3 weeks.** Nango infra, Connect UI, safety scanner, providers.yaml, marketplace manifest schema all carry over unchanged. The env file disappears for MCP env, stays for materializers.

**Should we push upstream?** Three different actions:

- **PR #72548** (OPEN, no review decision yet): add a +1 with our use case in the thread; offer to write a Nango reference plugin once the SDK shape stabilizes — that's free goodwill *and* gives us a head start on step 1 above. Don't push for merge; the author is iterating on a large change and a reviewer hasn't yet weighed in publicly.
- **PR #82326** (OPEN, **APPROVED**): track this — its title ("Add plugin manifest contract for SecretRef provider integrations") suggests it's the manifest-contract piece that #72548's seam depends on. APPROVED-but-open means it's likely waiting on something specific (a follow-up commit, a CHANGELOG entry, a rebase). If it lands, #72548's path clears.
- **Issue #76493:** this is the one we should actually advocate for, including potentially writing the PR ourselves. The change is small (zod schema relaxation on one field + resolver call), high-impact for us, and currently has no PR attached. Doing this work upstream is the cleanest path to retiring wrappers; even if it doesn't land in our v1 timeline, having the PR open establishes Isol8 as a credible upstream contributor.

**If neither lands within 12 months:** the wrapper design is permanent, not transitional. That's fine — wrappers are 20 lines of shell + one Go binary, not a burden. Plan accordingly; don't budget a migration that may never come.

### 6.3 Edge cases

| Edge case | Handling |
|---|---|
| Owner revokes Slack at Slack's side (out-of-band) | Nango's refresh fails 401 → cred-init removes the var from env file → tools start failing with clear "not connected" errors. UI shows red badge. |
| Two ECS tasks for the same owner (during rolling deploy) | Both have their own cred-init; both fetch same tokens. No coordination needed. |
| Nango outage | cred-init keeps serving last good tokens; agents continue to function until token expiry. Alert fires on first failed refresh. |
| Provider rotates their OAuth client_id (forced rotation) | Update Secrets Manager, redeploy Nango task. Customers must reconnect (one-time UI prompt). |
| Marketplace agent declares a provider that's not in providers.yaml | CI rejects the manifest PR. Force adding the provider first. |
| Customer connects two Slack workspaces | Nango supports multiple connections per provider per end-user with a `connection_id`. Marketplace manifest extension v2 to specify "which workspace" needed before this ships; v1 = one connection per provider per owner. |
| Token leaked in container logs | Cred-init writes to env file with 0640, never logs token values. Backend redacts known token-shaped strings in agent transcripts. Process-level: any subprocess that logs env is at fault, scanner rule could catch hardcoded `env | grep`. |

### 6.4 Things we explicitly accept

- **Tokens in container env** is the design choice, not an oversight. Re-read §2.7 if challenged.
- **No per-call audit** until we move to brokered proxy. Document this in the customer security FAQ.
- **Single-tenant Nango per env** means a Nango outage degrades all customers simultaneously. The alternative (per-tenant Nango) is operationally prohibitive at the customer counts we project; multi-AZ + monitored backups + a documented runbook is the accepted compromise.

---

## 7. Cost model

Assumes us-east-1, 2026 pricing. Per-month figures.

| Component | 100 owners | 1,000 owners | 10,000 owners |
|---|---|---|---|
| Aurora Postgres Serverless v2 (Nango DB) | $60 (0.5 ACU avg) | $180 (1.5 ACU avg) | $720 (6 ACU avg) |
| ElastiCache Redis (cache.t4g.small ×1 / .medium ×2 / .large ×3) | $25 | $130 | $400 |
| ECS Fargate Nango tasks (1 task / 2 tasks / 4 tasks @ 0.5 vCPU, 1GB) | $20 | $40 | $80 |
| Internal ALB | $20 | $20 | $30 |
| Public Connect UI (Lambda + CloudFront) | $5 | $20 | $100 |
| DDB snapshot table (on-demand) | $2 | $15 | $120 |
| KMS, Secrets Manager, CloudWatch | $15 | $40 | $150 |
| Data transfer (intra-VPC) | $5 | $30 | $250 |
| **Subtotal AWS** | **$152** | **$475** | **$1,850** |
| Nango Enterprise self-host license (optional, if needed for SLAs/audit/SSO) | $0 (OSS) or call sales | call sales | call sales |
| **Total (OSS Nango)** | **~$150/mo** | **~$475/mo** | **~$1,850/mo** |

**Cost per active owner per month:** $1.50 at 100, $0.48 at 1k, $0.19 at 10k. This is rounding error against the per-owner ECS task cost itself (the OpenClaw container is the dominant line). The credential layer is not a meaningful contributor to platform unit economics.

**Hidden costs not in the table:**
- Nango Enterprise license if/when needed — pricing not public, budget $40-100k/yr at >1k owners
- Engineering: 1-2 engineers for the 12 weeks of M1-M6, then ~10% of one engineer ongoing
- Provider OAuth app registration toil: 1-2 hours per provider, mostly one-time

---

## Appendix A — Sources

Auth platform comparison:
- Nango: github.com/NangoHQ/nango, nango.dev/pricing, nango.dev/docs/guides/platform/self-hosting (May 2026)
- Klavis: github.com/Klavis-AI/klavis, klavis.ai (May 2026)
- Composio: github.com/ComposioHQ/composio, composio.dev/pricing (May 2026)
- Arcade: arcade.dev/pricing, docs.arcade.dev/en/home/hosting-overview (May 2026)
- Pipedream: pipedream.com/connect, newsroom.workday.com/2025-11-19 acquisition announcement (May 2026)

Comparable production deployments:
- Anthropic: anthropic.com/engineering/managed-agents, platform.claude.com/docs/agents-and-tools/mcp-connector
- Arcade critique: arcade.dev/blog/anthropic-managed-agents-missing-hands
- OpenAI: developers.openai.com/apps-sdk/build/auth, stytch.com/blog/guide-to-authentication-for-the-openai-apps-sdk
- Gumloop: docs.gumloop.com/core-concepts/credentials
- Lindy: lindy.ai/security, lindy.ai/integrations
- Sema4: sema4.ai/docs/automation/control-room/vault (env-injection precedent)
- MultiOn: docs.multion.ai/learn/browser-extension
- Pipedream BYO OAuth (2024-06-06): pipedream.com/blog/bring-your-own-oauth-clients
- n8n: deepwiki.com/n8n-io/n8n/3.2-credentials-api-and-security, docs.n8n.io/hosting/configuration/configuration-examples/encryption-key
- Zapier: zapier.com/blog/solving-end-user-auth (consent-screen branding precedent)

OpenClaw internals (verified against the clone at `~/Desktop/openclaw`, HEAD `510628ca6f`, 2026-05-21):
- [Issue #71593](https://github.com/openclaw/openclaw/issues/71593), [PR #72548](https://github.com/openclaw/openclaw/pull/72548), [PR #82326](https://github.com/openclaw/openclaw/pull/82326), [PR #23096](https://github.com/openclaw/openclaw/pull/23096), [Issue #76493](https://github.com/openclaw/openclaw/issues/76493) — all confirmed via `gh`; states and bodies pulled live
- MCP spawn env: `src/agents/mcp-stdio-transport.ts:50-62` (uses MCP SDK `getDefaultEnvironment()` + explicit `serverParams.env`, NOT full parent inheritance)
- `auth.profiles` machinery: `src/agents/auth-profiles/` module, referenced from `src/config/plugin-auto-enable.shared.ts:483` and `src/config/schema.labels.ts:537`; storage path per repo CLAUDE.md is `~/.openclaw/agents/<agentId>/agent/auth-profiles.json` (LLM/model auth only — confirms our credential layer stays out of this path)

## Appendix B — Glossary of decisions made vs deferred

| Decision | Made now | Deferred |
|---|---|---|
| Vault platform | **Nango self-hosted** | Klavis revisit Q4 2026 |
| Delivery mechanism | **Env file + SIGHUP reload** | Brokered proxy (not until per-call audit becomes a hard requirement) |
| Init binary language | **Go** | Rust if Go ever bites us |
| Refresh cadence | **25 minutes** | Per-provider tuning when first short-lived-token provider needs it |
| Provider naming | **De-facto SDK conventions** | Standardize via spec when industry consensus exists |
| BYO OAuth client per provider | **Yes from day 1** | n/a |
| Connect UI | **Nango white-labeled** | Custom build if Nango Connect can't be branded enough |
| Audit | **Connection grant/revoke at Nango; tool-call audit out of scope** | Per-call audit when proxy posture lands |
| Multi-tenant Nango | **One Nango per env (dev/stg/prod)** | Per-tenant Nango never (operationally untenable) |
| Marketplace credentials block | **Manifest extension `requires.credentials`** | Scope-enforcement at OAuth flow when BYO supports it |
| Migration path off wrappers | **Gated on BOTH PR #72548 AND issue #76493; ~2-3 weeks once both land. Wrappers stay for materializer cases regardless.** | n/a |
