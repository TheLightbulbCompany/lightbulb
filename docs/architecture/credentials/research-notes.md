# Credential-Linking Research Notes

**Date:** 2026-05-21
**Status:** Research notes — additive to `problem-discovery.md` (the rubric) and the deleted `solution-brief-stale.md` (the prior attempt; in git history). Updates the runtime-delivery analysis with verified facts from OpenClaw HEAD `510628ca6f`.
**Scope:** "Easy linking" — how third-party credentials (Slack, Greenhouse, etc.) actually reach an MCP server, CLI, plugin, or skill running inside a per-user OpenClaw container. Provisioning side (OAuth broker choice) is touched but not re-evaluated here.

---

## TL;DR

The stale brief's wrapper-shim (`lightbulb-tool`) is **not required**. OpenClaw already has the runtime machinery to deliver credentials cleanly:

1. `mcp.servers[].env` accepts string values, passed explicitly to the child at spawn — full parent-env-inheritance is NOT needed.
2. Config-load env substitution (`${VAR}`) is a **documented first-class feature**, not "accidental." The discovery doc undersells this.
3. **OpenClaw watches `openclaw.json` and triggers `dispose-mcp-runtimes` on any `mcp.*` change** (verified: `src/gateway/config-reload-plan.ts:102`). Hot-reload disposes MCP runtimes; next call re-spawns with fresh env. Token rotation is essentially free.
4. Lightbulb AI already has `config_patcher.py` — a file-locked, atomic deep-merge primitive for openclaw.json on EFS. The exact tool needed to write tokens into MCP env at runtime.

**Recommended design:** write fetched tokens directly into `mcp.servers[].env` (and equivalents for skills/channels) via `config_patcher`. Let OpenClaw's existing reload pipeline handle propagation. No new wrapper binary, no init container, no env-file machinery to maintain.

**What we still need to build:**
- Token fetcher loop (cron task on the backend that pulls from Nango → calls `config_patcher`)
- `providers.yaml` mapping (Nango provider key → openclaw.json path to populate)
- Materializers for file-based credentials (gcloud ADC JSON, kubeconfig, etc.) — small Python module per provider
- Marketplace manifest schema (still undefined; deferred to a separate RFC)

**What we DON'T need to build:**
- `lightbulb-tool` wrapper shim
- `lightbulb-credinit` Go binary
- Per-MCP wrapper scripts
- SIGHUP reload protocol for OpenClaw children
- A new file watcher on the credential file

---

## 1. Corrections to the discovery doc

The discovery doc's §4 catalog of OpenClaw credential surfaces is accurate as far as it goes, but understates what's actually available. Three corrections:

### 1.1 `${VAR}` substitution is documented, not accidental

`src/config/env-substitution.ts` opens with a docstring containing this exact example:

```json5
{
  models: {
    providers: {
      "vercel-gateway": { apiKey: "${VERCEL_GATEWAY_API_KEY}" }
    }
  }
}
```

The implementation (`resolveConfigEnvVars`) walks the entire parsed config and substitutes any `${UPPERCASE_VAR}` string from `process.env`. It is called once at config-load from `src/config/io.ts:1158`. Missing vars raise `MissingEnvVarError` unless an `onMissing` callback is provided (which the load path does, capturing warnings instead of crashing).

Discovery doc §4.2 calls this "accidental … the schema doesn't promise it." That framing is wrong — there is a published file header docstring, an explicit pattern matcher (`ENV_VAR_NAME_PATTERN`), an escape syntax (`$${VAR}`), and a missing-var error class. This is supported surface.

### 1.2 Parent-env inheritance is irrelevant to the design

The discovery doc treats `getDefaultEnvironment()` filtering as a blocker. It's only a blocker for designs that depend on the parent env leaking through. The actual MCP spawn path (`src/agents/mcp-stdio-transport.ts:50-62`) is:

```ts
const baseEnv = { ...getDefaultEnvironment(), ...this.serverParams.env };
```

`serverParams.env` comes from `mcp.servers[].env` in the resolved config. Anything you write into that field reaches the child verbatim, regardless of what's in the parent env. The two ways to populate that field are:

1. **At config-load**: write `"SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}"` in openclaw.json; `resolveConfigEnvVars` substitutes from `process.env` once.
2. **At file-write time**: write `"SLACK_BOT_TOKEN": "xoxb-..."` (literal) directly into openclaw.json via a backend-side patcher.

Either reaches the spawned MCP. The first requires the env var to be present in the OpenClaw process at startup. The second requires the file to be re-readable when OpenClaw reloads (which it is — see §1.3).

### 1.3 OpenClaw hot-reloads on `mcp.*` config changes — verified

`src/gateway/config-reload.ts` runs a chokidar watcher on the openclaw.json path. On change, it diffs the parsed config and consults the reload-plan rule table in `src/gateway/config-reload-plan.ts`. The relevant rule (line 102):

```ts
{ prefix: "mcp", kind: "hot", actions: ["dispose-mcp-runtimes"] },
```

`dispose-mcp-runtimes` is a **hot** action (no gateway restart). It disposes the in-memory MCP runtime state; the next agent call that needs an MCP server re-spawns it, picking up whatever the current `mcp.servers[].env` says.

This is the keystone fact. It means refresh isn't a separate engineering problem — it falls out of the file-watcher OpenClaw already runs.

Caveats:
- Hot-reload disposes the *runtime*, not in-flight calls. A long-running tool call might use the old token until it completes. Failure mode is graceful (401 from the provider, MCP client retries on transport close).
- The watcher relies on chokidar. On EFS, polling is required (`CHOKIDAR_USEPOLLING=true`, already set per repo CLAUDE.md). Polling latency is ~1s; refresh delay is bounded by that plus the write-to-MCP-respawn gap.

### 1.4 The stale brief's wrapper-shim concept solves a problem we don't have

Discovery doc §4 and the stale brief §2.3 both argue that wrappers are needed because parent env doesn't inherit. That's a non-sequitur — we never needed parent env inheritance, we needed `mcp.servers[].env` to be populated. There are two ways to do that, neither requires a wrapper.

The wrapper-shim approach still has one legitimate use: **materializers** for file-based credentials. `gcloud` reads `GOOGLE_APPLICATION_CREDENTIALS` pointing at an ADC JSON file; we have to write that file somewhere. But a materializer is a per-provider Python function on the backend (writes the file to EFS, sets the env var to its path), not a wrapper script invoked per-spawn. Different shape, different scope.

---

## 2. The three viable delivery designs (verified)

Same rubric as discovery doc §10.3, but now informed by §1 above. Designs ranked by complexity, simplest first.

### Design C — Literal injection via config_patcher (RECOMMENDED)

**Mechanism:** backend cron task fetches current tokens from Nango (or whatever broker we pick), calls `config_patcher.patch_user_config(owner_id, {mcp: {servers: {greenhouse: {env: {GREENHOUSE_API_KEY: "<token>"}}}}})` with the appropriate paths. `config_patcher` does the file-locked atomic deep-merge. OpenClaw's chokidar watcher fires, dispose-mcp-runtimes runs, next agent call re-spawns the MCP with the fresh env.

**Pros:**
- Uses existing Lightbulb AI infra (`config_patcher` is already production-tested, handles EFS-specific lockf/atomic-rename/chown).
- Refresh is automatic via OpenClaw's hot-reload — zero additional refresh machinery.
- No new in-container processes. Backend pushes; container pulls via its existing reload pipeline.
- Works identically for MCP env, skills `apiKey`, channel plugin tokens — all are openclaw.json paths.

**Cons:**
- Tokens land in openclaw.json on EFS. Blast radius: one container per owner, EFS encrypted at rest with per-tenant KMS, only that owner's task role can read it. Same blast radius as any other "tokens in container" design.
- Audit trail must be reconstructed from `config_patcher` writes, not from the file itself (since the file is mutated frequently).
- Marketplace safety scanner can't enforce "no tokens in config" globally — it has to distinguish seller-shipped values (rejected) from platform-injected values (allowed). Differentiable because the platform injects post-deploy, not at bundle-publish.

**Acceptance criteria scoring (§7.4, runtime):**
- #16 Credentials at boot: ✅ written before first chat message (provision-gate ensures this)
- #17 All four primitives: ✅ MCP env, skills.entries.*.apiKey, channels.*.accounts.*.botToken, plugin configs all writable through the same patcher
- #18 Survives refresh: ✅ hot-reload disposes MCP runtimes; next call re-spawns
- #19 Multi-tenant blast radius: ✅ per-container EFS, structural

### Design B — `${VAR}` interpolation + parent-env preload

**Mechanism:** openclaw.json contains `${SLACK_BOT_TOKEN}` placeholders. An init step at container start writes a `.env` file the entrypoint sources before exec'ing openclaw, so the OpenClaw process has the real values. Config-load substitution wires them into `mcp.servers[].env`. On refresh, re-write the .env file and signal OpenClaw to reload its config (the chokidar watcher on the openclaw.json file doesn't fire because openclaw.json didn't change — so we'd need to also touch openclaw.json or send a SIGHUP).

**Pros:**
- openclaw.json never contains literal tokens. Cleaner for marketplace authoring (sellers write `${VAR}` references; platform docs say "these placeholders are populated at deploy").
- Audit is simpler: tokens live in one .env file with clear ownership.

**Cons:**
- Config-load substitution runs ONCE at OpenClaw startup. Refresh requires either restarting OpenClaw (kills in-flight chat) or touching openclaw.json to retrigger the watcher (which then runs substitution with the new parent env — but only if we somehow refresh the parent env first; processes can't mutate their own env). The simplest version is: rewrite the .env file, modify openclaw.json's mtime, watcher fires, substitution re-runs.
- Requires a new init binary OR an entrypoint shell script. More moving parts than Design C.
- Doesn't work without restart for already-spawned MCP children (their env is fixed at spawn).

**Score:** Same as C on #16/#17/#19. **Worse on #18** because in-life refresh requires more orchestration.

### Design A — Wrapper shim (`lightbulb-tool`)

**Mechanism:** as described in the stale brief §2.3. Every credential-needing tool is invoked through `lightbulb-tool` which sources `/run/isol8/env/credentials.env` then `exec`s the real binary. Refresh updates the env file; next spawn picks it up.

**Pros:**
- Tokens never in openclaw.json or any other config artifact.
- Same mechanism for MCPs, CLIs, plugins, skills (anything invoked as a process).

**Cons:**
- New binary + new convention + new safety-scanner rule (every credentialed tool must declare `command: lightbulb-tool, args: [<real_command>, ...]`).
- Long-running MCP children still need SIGHUP+respawn to see new tokens (file is re-sourced only at exec time; in-process env doesn't update). We'd build the SIGHUP daemon ourselves.
- Materializer handling still needed separately for file-based creds.

**Score:** Same as C on #16/#17/#19. Roughly equal on #18, but at the cost of building+maintaining the wrapper, the SIGHUP daemon, and the safety-scanner enforcement.

### Design D — SecretRef in MCP env (#76493 + #72548) — DEFERRED

**Status:** PR #72548 (plugin seam) explicitly defers `mcp.servers[].env` SecretRef coverage. Issue #76493 (open the schema) has no PR. Neither lands in our timeline.

**Note:** even if both land, the migration cost from Design C is ~1 week (write a Nango SecretRef plugin against #72548's SDK, swap literal values for `{source: "nango", provider: "Lightbulb AI", id: "slack/access_token"}` references). Nothing about Design C makes a future Design D migration harder.

---

## 3. End-to-end linking flow — the recommended shape

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Lightbulb AI backend (FastAPI)                          │
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
│   │ POST /webhooks/nango   (event-driven, the primary refresh path)  │  │
│   │   verify HMAC signature (Nango sends X-Nango-Signature)          │  │
│   │   resolve event → owner_id (end_user_id on the connection)       │  │
│   │   fetch full connection from Nango (event payload is metadata)   │  │
│   │   patch = providers_yaml.render({provider: conn})                │  │
│   │   config_patcher.patch_user_config(owner, patch)                 │  │
│   │   materializers.write(owner, provider, conn)  # if file-based    │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ┌──────────────────────────────────────────────────────────────────┐  │
│   │ credential_safety_poll (cron, hourly — fallback for missed       │  │
│   │ webhooks; reuses the same patch function)                        │  │
│   │   for owner in active_containers:                                │  │
│   │     conns = nango.list_connections(end_user_id=owner)            │  │
│   │     patch = providers_yaml.render(conns)                         │  │
│   │     config_patcher.patch_user_config(owner, patch)  # no-op if   │  │
│   │                                                      # unchanged │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                                  │                                       │
└──────────────────────────────────┼───────────────────────────────────────┘
                                   │ EFS write
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│       Per-owner ECS task — OpenClaw container                            │
│                                                                         │
│   /mnt/efs/users/{owner}/                                               │
│     openclaw.json    ◀── chokidar watcher (CHOKIDAR_USEPOLLING=true)    │
│     ├─ mcp.servers.greenhouse.env.GREENHOUSE_API_KEY = "<live>"         │
│     ├─ skills.entries.slack.apiKey = "<live>"                           │
│     └─ channels.telegram.accounts.default.botToken = "<live>"           │
│     gcloud/adc.json  (materialized)                                     │
│                                                                         │
│   OpenClaw process:                                                     │
│     ├─ config-reload.ts: chokidar fires on openclaw.json change         │
│     ├─ config-reload-plan.ts: mcp.* path → dispose-mcp-runtimes         │
│     ├─ MCP runtimes disposed                                            │
│     └─ next chat message: respawn MCPs with new env from updated config │
└─────────────────────────────────────────────────────────────────────────┘
```

**Components, by who owns them:**

| Component | Status | Lives in |
|---|---|---|
| Nango self-hosted | Build (per stale brief §3) | New CDK stack `IsolCredentialsStack` |
| Connect UI | Build (Nango white-label) | `connect.isol8.com` |
| Backend `/connect/{provider}` endpoint | Build | `apps/backend/routers/connections.py` (new) |
| Backend `POST /webhooks/nango` (HMAC-verified) | Build | `apps/backend/routers/webhooks.py` (extend) |
| `credential_refresh.apply_connection(owner, provider, conn)` | Build — shared by webhook + safety poll | `apps/backend/core/services/credential_refresh.py` (new) |
| `credential_safety_poll` cron task | Build — hourly fallback | same module |
| Nango webhook subscription | One-time per env — registered at Nango admin or via `cdk deploy` of `IsolCredentialsStack` | n/a (config) |
| `providers.yaml` mapping | Build | `apps/backend/core/services/credentials/providers.yaml` (new) |
| `config_patcher.patch_user_config` | **EXISTS** | `apps/backend/core/services/config_patcher.py` |
| Materializers (gcloud, kubeconfig, etc.) | Build per-provider as needed | `apps/backend/core/services/credentials/materializers/` (new) |
| Marketplace manifest `requires.credentials` | Schema RFC pending | `apps/backend/schemas/marketplace.py` |
| Safety scanner rule "no literal tokens in seller bundles" | Build | `apps/backend/core/services/marketplace_safety.py` extension |
| OpenClaw config-reload pipeline | **EXISTS** in OpenClaw upstream | n/a (pinned version) |

**Total new code we own:** ~6 files. The stale brief proposed ~12 (Go binary + entrypoint + materializer framework + SIGHUP daemon + per-tool wrappers + scanner rules + Connect UI plumbing).

### 3.1 Refresh-cadence model (decided)

Refresh is **webhook-driven** with an hourly safety poll. Rationale:

- Nango handles upstream token refresh on its own — it refreshes shortly before expiry using the stored refresh token. We don't poll Nango to *cause* refresh; refresh happens regardless of whether we're listening.
- Nango fires webhooks on `auth` events (token refreshed) and `connection` events (created/updated/deleted). We subscribe once per env and react.
- The webhook handler is the primary path. Tokens hit EFS within seconds of Nango refreshing them.
- The hourly safety poll catches any missed webhooks (network blip, Nango outage during webhook fanout, etc.) and reuses the same `apply_connection` function.
- Per-provider polling cadence is not needed — webhooks fire on the provider's natural cadence (every ~1h for Google, every ~8h for GitHub, rarely for Slack bot tokens, never for Greenhouse API keys).

At expected scale (1,000 owners × ~5 providers × mixed lifetimes), this produces roughly 10,000 `config_patcher` invocations per day platform-wide, single-digit calls/sec average. Well within EFS and Nango self-hosted capacity.

**Webhook security**: Nango sends `X-Nango-Signature` (HMAC-SHA256 over the body using a shared secret). The handler must verify before processing — same pattern as the existing Stripe webhook handler. Failed verification → 401, log, drop.

**Webhook idempotency**: the handler is naturally idempotent because `config_patcher`'s `_locked_rmw` returns early when the patch would be a no-op. Replay-safe.

**Polling-only fallback**: if webhook integration slips for any reason, the safety-poll-at-20-min cadence alone covers the 1-hour-token case with margin. That's a single cron task with no new endpoints — the v1 fallback if `IsolCredentialsStack` ships without webhook plumbing.

---

## 4. What's still open

These are out of scope for "easy linking" specifically and need separate research:

1. **OAuth broker choice (Nango vs alternatives).** The stale brief picked Nango. The decision is independent of the runtime-delivery design — whatever broker we pick, `credential_refresh_worker` fetches from it and `config_patcher` writes the result. Worth a separate review against §7's full acceptance criteria, especially #1 (one-click connect) and #12 (one-time platform onboarding).

2. **Marketplace manifest schema.** What does a seller actually write to declare "I need Slack"? Discovery doc §11 open question #3. Needs an RFC. Affects what `providers.yaml` looks like and what the deploy-time validator checks.

3. **AWS cross-account roles.** Discovery doc §10's missing fourth category. The delivery shape is the same (write `AWS_ROLE_ARN` + materialize a credential_process script into the container) but the provisioning shape is completely different from OAuth (customer-deployed CFN template + sts:AssumeRole). Separate research thread.

4. **Agent-identity credentials (Jarvis bootstrap).** Tier 2 in the discovery doc. The delivery mechanism is the same as customer creds (Design C writes to openclaw.json), but the *storage* is different — these are platform-owned secrets refreshed by humans, not customer-owned via OAuth. Likely: AWS Secrets Manager for storage, same refresh worker pattern. Confirm in a separate pass.

5. **What happens when Nango is down.** Design C degrades gracefully — the last known-good tokens stay in openclaw.json until the next refresh succeeds. But we need to write a real runbook before launch.

---

## 5. Acceptance-criteria mapping (the rubric)

Design C against the 19 criteria in discovery doc §7:

| § | Criterion | Design C | Notes |
|---|---|---|---|
| 7.1 — End customer | | | |
| 1 | One-click connect per provider | ✅ | Same as stale brief — Nango Connect UI |
| 2 | Never sees a token | ✅ | UI shows green checkmark only |
| 3 | Never leaves Lightbulb AI | ✅ | Same |
| 4 | One place to see status | ✅ | `/settings/connections` page |
| 5 | Connection breakage visible | ✅ | Nango webhook → backend → UI badge |
| 6 | Disconnection works | ✅ | UI calls Nango revoke; next refresh removes from openclaw.json |
| 7 | Deploy flow connects what's missing | ✅ | Same as stale brief — pre-deploy validation |
| 7.2 — Marketplace seller | | | |
| 8 | Manifest-declared requirements | ⏳ | Schema RFC still open |
| 9 | Zero auth code in bundle | ✅ | Sellers reference standard env var names |
| 10 | Safety scanner rejects bundles with hardcoded tokens | ⏳ | Build (extends existing scanner) |
| 11 | Documented provider catalog | ⏳ | `providers.yaml` is the catalog |
| 7.3 — Platform operator | | | |
| 12 | One-time provider onboarding | ✅ | Per-provider Nango app registration, then catalog entry |
| 13 | Platform-internal agent bootstrap | ⏳ | Tier 2 — separate research thread |
| 14 | Auditable | ✅ | Nango logs grants/revokes; `config_patcher` logs all writes (already does — see backup `.bak` and log_context) |
| 15 | Rotatable under incident | ✅ | Force a refresh-worker run; tokens propagate within polling interval |
| 7.4 — Agent runtime | | | |
| 16 | Credentials at boot | ✅ | provision-gate ensures populated openclaw.json before first chat |
| 17 | All four primitives | ✅ | All four read from openclaw.json paths the patcher writes |
| 18 | Survives refresh | ✅ | `dispose-mcp-runtimes` hot-reload — verified |
| 19 | Multi-tenant blast radius is one customer | ✅ | One container per owner, structural |

⏳ = work to do, not a blocker for the runtime-delivery design itself.

---

## 6. The single decision this surfaces

**Are we comfortable writing literal tokens into openclaw.json on EFS?**

This is the load-bearing tradeoff. The alternative (Design A or B) is more moving parts in exchange for keeping openclaw.json token-free.

Arguments for the yes:
- Per-tenant EFS, KMS-encrypted at rest, only-owner task role can read it.
- Same blast radius as any in-container credential storage.
- Lets us delete an entire layer of machinery (wrapper binaries + refresh daemons + SIGHUP protocols).
- Reuses production-tested `config_patcher`.

Arguments for the no:
- Audit-trail purity: tokens-in-config is harder to grep/scan than tokens-in-vault.
- Marketplace safety scanner has to grow logic to distinguish seller bundles (token-free) from deployed configs (tokens present).
- A future security review might prefer "tokens never touch persistent storage in the container" as a principle, even if mechanically equivalent.

Recommendation: yes. The single-tenant per-container invariant is the load-bearing isolation primitive; we already trust it for the gateway WebSocket pool, the EFS workspace, the per-user ECS task. Adding "credentials in openclaw.json" doesn't change that posture, and the cost savings (eliminated wrapper + daemon) are real.

If the answer turns out to be no after a security review, fall back to Design B. The interface (`config_patcher.patch_user_config` taking provider-keyed updates) is symmetric — only the data shape changes (literal vs `${VAR}`), and a coordinated init container appears.
