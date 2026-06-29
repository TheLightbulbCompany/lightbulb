# CLI integrations via bundled skills — design spec

**Date:** 2026-06-28
**Status:** Draft for review
**First consumer:** Vercel CLI
**Related:** `vercel` MCP integration (shipped, PR #1088); `providers.yaml` credential rail; OpenClaw skill subsystem.

---

## 1. Problem & goal

We want a user to connect an integration that delivers a **command-line tool** to their always-on per-user OpenClaw container (ECS Fargate, 0.5 vCPU / 1 GB, Debian), **on demand at connect time**, **without baking the tool into the Docker image**. Baking every possible CLI does not scale, and most users won't need any given CLI.

The headline case: a user connects **Vercel** and their agents can run `vercel deploy` / `vercel logs` / `vercel ls` — authenticated as that user.

**Design decision (this spec):** deliver the CLI as a **bundled skill that comes with the integration**. Connecting the integration enables the skill and seeds its credential automatically — there is no separate "enable a skill" step for the user.

**The CLI is the primary Vercel surface — the GitHub model.** GitHub in Isol8 is a single paste-token integration: one PAT → the `gh` CLI → full read **and** write, one step. We mirror that for Vercel: **one pasted Account Token → the `vercel` CLI → full capability**, one step. The previously-shipped Vercel **MCP** (OAuth, read/inspect) becomes **optional/secondary** — a nice-to-have for docs-search and zero-paste reads, not required. The CLI alone covers both reading (`vercel logs`/`ls`/`inspect`) and writing (`deploy`/`rollback`/`env`). This collapses the "two credential steps" concern: a user who just wants Vercel working pastes one token.

---

## 2. Background — verified constraints (the evidence)

Every load-bearing claim below was verified against primary sources, the OpenClaw fork source (`~/Desktop/agent.nosync`), and/or the live fleet. Citations are file:line where internal.

1. **Container filesystem:** only `/home/node/.openclaw` is on EFS (NFSv4.1, persists). `npm` global prefix (`/usr/local`) and `/tmp` are ephemeral overlayfs, wiped on restart. The boot script symlinks (HOME-relative) `~/.npm` → EFS cache, `~/.npm/_npx` → `/tmp`, `~/.cache/uv` → `/tmp`; `XDG_*` → EFS. *(container-stack.ts:485-551)*

2. **The NFS small-file storm is real and is the thing to avoid:** extracting a large `node_modules` tree **onto EFS** serializes thousands of synchronous NFS creates, wedges the Node event loop, and fails the on-loop `/healthz` probe → ECS kill → crash-loop. The fix already in place keeps the heavy extract on `/tmp` and only small/compressed/read-mostly artifacts on EFS. AWS's own guidance: *"you don't ever want EFS in the critical path of your application's code."*

3. **`npx <pkg>` is storm-safe here and proven live.** On the **prod founder container**, `npx vercel@latest --version` extracted **14,349 files / 244 MB to `/tmp`**, added only **+1,141 compressed cache files to EFS**, and `/healthz` stayed **<600 ms with 0 errors** throughout a 68 s cold run. `_npx` extract and `_cacache` download are separable; symlinking `_npx`→`/tmp` while keeping `_cacache` on EFS is sound (no cross-FS rename, unlike uv's EXDEV hazard).

4. **3 days of prod telemetry:** zero `npm`/`npx`/MCP event-loop blocks > 1 s; the only multi-second blocks are an **unrelated provider-auth re-warm bug** (see §11, follow-up). MCP materialization is non-blocking.

5. **Exec runs on the `gateway` host (no sandbox), verified source + live.** `tools.exec.host` is unset in our config → defaults to `auto`. `auto` resolves to `sandboxAvailable ? "sandbox" : "gateway"` *(bash-tools.exec-runtime.ts:306)*. `sandboxAvailable = Boolean(defaults?.sandbox)` *(bash-tools.exec.ts:1298,1475)* — purely config-driven, and the sandbox is docker-based. We never configure `agents.defaults.sandbox`; the **live dev container** shows `EXEC: {mode: full}`, `SBX: NONE`. So effective host = **gateway**, and the desktop `node` override is still permitted (we do **not** pin `host`).

6. **Skill env injection reaches the gateway-host exec, with no restart.** For each enabled skill, OpenClaw injects its `primaryEnv` → resolved `skills.entries.<key>.apiKey` (and any `skills.entries.<key>.env`) into the gateway `process.env` per agent attempt *(env-overrides.ts:176-208; run/attempt.ts:1063, reverted :5414)*. The exec tool composes its child env **fresh per call** from `process.env` *(bash-tools.exec.ts:1564)*, so a gateway-host (and node-host) exec child sees the var. The **sandbox** host's env builder ignores `process.env` — but we don't use it (§2.5). A `skills.entries.*` config edit applies via chokidar hot-reload with **no restart** (`skills` prefix maps to reload kind `none`; snapshot bump on change) *(config-reload.ts:254-257; config-reload-plan.ts:134)*.

7. **Native skill install is explicit-only** *(install.ts; only callers are the `skills.install` RPC, onboarding, CLI)* — enabling a skill does **not** auto-install bins. **This is fine for us:** the npx path needs no install. (We deliberately avoid native `kind:node`, which does `npm install -g` and can land a large tree on EFS = the storm.)

8. **Marketplace leak hazard:** `catalog_slice.py:344` deep-copies the `tools` block into published artifacts **with no secret scrub** (`_scrub_secret_fields` covers only `mcp.servers[*]` + `skills.entries[*]`). A token in `tools.exec.env` would ship to every buyer = cross-tenant account takeover. **`skills.entries[*]` IS scrubbed**, so it is the safe home for the token.

9. **Vercel CLI auth:** the documented non-interactive path is the **`VERCEL_TOKEN` env var** (or `--token`) — *not* hand-writing `auth.json` (auto-generated, "should not be edited manually"). The token is a dashboard-minted **Account Token** (first one must come from the dashboard UI); it is **finite-lived** (explicit `expiresAt`, ~10-day inactivity expiry, leak auto-revocation) — so a 401→reconnect path is required (it's a paste credential, not OAuth). **Token alone ≠ deploy:** `vercel deploy/pull/list` are project-scoped and need `VERCEL_ORG_ID`+`VERCEL_PROJECT_ID`, a linked `.vercel/project.json` (`vercel link`), or `--scope`/`--project`. Token alone drives account-level commands (`whoami`, `logs <url>`).

---

## 3. Design overview — "the skill comes with the integration"

A **CLI integration** is an integration whose connect flow delivers and enables a **bundled skill** that teaches the agent to run the tool via `npx`, with the tool's credential injected as an env var.

```
User connects "Vercel CLI" integration  (Integrations panel)
   ├─ pastes a Vercel Account Token (+ optionally team/project)
   ├─ token stored encrypted in integration_secret_store
   └─ connect flow (existing materializer rail) writes to the owner's EFS config:
        skills.entries.vercel = { enabled: true, apiKey: <token-from-store> }
        (+ optional skills.entries.vercel.env: { VERCEL_ORG_ID, VERCEL_PROJECT_ID })
      and ensures the vercel SKILL.md is present in the EFS skills dir
   └─ chokidar hot-reload → no restart
        └─ next agent turn: VERCEL_TOKEN injected into gateway-host exec env
   └─ pre-warm: run `npx --yes vercel@<ver> --version` once at connect (behind the spinner)

Agent, next message: runs `npx --yes vercel@<exact-ver> deploy`
   ├─ extract → /tmp (storm-safe), download cache → EFS (persists)
   └─ VERCEL_TOKEN present → authenticated
```

Three concerns, each on existing machinery:

| Concern | Mechanism |
|---|---|
| **Can it run?** | `npx --yes vercel@<exact-ver>` (npx already on PATH; extract→/tmp). No shim, no `tools/bin`, no `pathPrepend`. |
| **Is it authed?** | `skills.entries.vercel.apiKey` (encrypted store → config) → `primaryEnv: VERCEL_TOKEN` injected on the gateway host. Scrubbed from marketplace slice. |
| **Does the agent know to?** | the bundled **SKILL.md** (knowledge + the exact `npx` invocation + auth/notes), enabled at connect. |

### What we are NOT building (and why)

- **No `tools.exec.pathPrepend`** — it does not exist in OpenClaw; writing a speculative key risks a config-brick crash-loop. Not needed: the agent runs `npx vercel` directly.
- **No custom `cli_tool` materializer / shim / `tools/bin`** — the native skill `apiKey`/`primaryEnv` env injection already delivers the credential; reinventing it adds surface and the marketplace-leak risk.
- **No host pin (`tools.exec.host: gateway`)** — would reject the desktop's `node` exec override. We rely on `auto`→`gateway` (because no sandbox is configured) and guard it with a regression test instead.
- **No native `kind:node` install** — `npm install -g` can land a 14k-file tree on EFS (the storm). npx-to-`/tmp` is the storm-safe path.
- **No `binary` / `kind:download` / go / uv kinds in v1** — deferred (§8).

---

## 4. Components

### 4.1 Integration registry entry
A `vercel_cli` provider (distinct key — `vercel` is already the MCP-OAuth integration; the registry enforces one wiring per key). Shape: a **paste-token** acquisition that, instead of (or in addition to) seeding a CLI config file, declares a **bundled skill** + the env var name the token maps to. Exact schema TBD in the implementation plan; it must express: token validation endpoint (`GET https://api.vercel.com/v2/user`, **by API call, not token-prefix** — prefixes overlap), the skill key, `primaryEnv: VERCEL_TOKEN`, and optional org/project capture.

### 4.2 The bundled skill (SKILL.md)
- **Frontmatter:** `primaryEnv: VERCEL_TOKEN`, metadata/description.
- **Body (knowledge):** instruct the agent to run `npx --yes vercel@<exact-ver> <cmd>`; note that auth is in `$VERCEL_TOKEN`; for deploys, the project is identified via `$VERCEL_ORG_ID`/`$VERCEL_PROJECT_ID` or `vercel link --yes`; common recipes (deploy a preview, read a failing build log, rollback).
- **Delivery:** seeded to the owner's EFS skills dir at connect (a few small **text** files — storm-safe), or baked into the image and enabled at connect. **Recommendation: seed-at-connect** so new CLI integrations don't require an image rebuild; revisit if seeding proves fiddly. Either way it is **enabled** (not just present) at connect.

### 4.3 Token handling (hard invariants)
- Raw token stored **encrypted** in `integration_secret_store` (existing).
- Materialized only to **`skills.entries.vercel.apiKey`** in the owner's `openclaw.json` (scrubbed from the marketplace slice) — **never** to any `tools.*` field.
- **Belt-and-suspenders:** extend `_scrub_secret_fields` (catalog_slice) to also scrub the top-level `tools` block, so a future mistake can't leak a secret placed there.
- File perms / owner: config written via the existing patcher (0600-equivalent, uid 1000), per the gh/gog precedent.

### 4.4 Connect flow
1. Validate the token (`GET /v2/user`).
2. Store encrypted; capture optional org/project.
3. Patch `skills.entries.vercel` (enabled + apiKey [+ env]) and ensure SKILL.md present → hot-reload (no restart).
4. **Pre-warm** synchronously behind the connect spinner: `npx --yes vercel@<ver> --version` (single-writer). Converts npx egress failures into a clean connect-time error and warms `_cacache` so the agent's first real call isn't the 68 s cold path.

### 4.5 Disconnect
The remover must delete **both** the skill enablement (set `enabled:false` / remove the entry) **and** the stored token, leaving **zero** token residue on EFS. Test: connect→disconnect leaves no token under the owner's EFS root. No `rm -rf` of large trees (the `_cacache` may stay; it's harmless and compressed).

### 4.6 Reconnect / expiry
On a 401 (expired/revoked token), surface a **"reconnect Vercel"** state in the Integrations UI. No self-refresh (paste credential). The agent should treat 401 from `vercel` as "ask the user to reconnect," not retry.

### 4.7 Exec-host regression guard
A test asserting our emitted config resolves exec to the **gateway** host — i.e. no `agents.defaults.sandbox` is configured and `tools.exec.host` is unset-or-`gateway`/`auto-without-sandbox`. Tripwire so a future image/config change can't silently flip exec into a sandbox and break every CLI token.

---

## 5. Vercel as v1 (concrete) — the primary surface

- Provider key: `vercel_cli` (paste-token), presented as the primary **Vercel** integration.
- Skill key: `vercel`, `primaryEnv: VERCEL_TOKEN`.
- Pinned: `vercel@<exact-version>` (exact, not `@latest` — exact is the only form that reuses a warm `_npx/<hash>` with no per-call registry round-trip).
- Connect: paste Account Token (+ team/project) → enable skill + seed token → pre-warm. **One step.**
- Covers read **and** write: `vercel logs`/`ls`/`inspect` (read) + `deploy`/`rollback`/`env` (write). Deploy works because we capture org/project (or the skill runs `vercel link --yes`).
- No image change, no boot-script change, no new RPC, no host pin.

### 5.1 The CLI token is NOT the MCP token (and can't be shared)

| | **MCP token** (shipped, optional) | **CLI token** (this design, primary) |
|---|---|---|
| Acquisition | OAuth (DCR + PKCE), no paste | **paste** a dashboard Account Token |
| Capability | identity-scoped; server-side session maps to account → read/inspect only | real API permissions → read **and** write (deploy/env/logs) |
| Shareable? | **No** — the public OAuth flow grants only `openid`/`offline_access`; Vercel's API/team permissions are private-beta and not requestable through it, so the OAuth token 403s on CLI operations | — |
| Lifecycle | self-refreshing (OAuth) | static, finite-lived → 401 ⇒ reconnect |
| Storage | MCP SDK OAuth token file (mcp_oauth materializer) | encrypted store → `skills.entries.vercel.apiKey` |

Unlike a GitHub PAT (which can drive both a CLI and GitHub's MCP), Vercel's hosted MCP is OAuth-only and will not accept the static Account Token — so the two surfaces cannot share one credential. The CLI token is the single credential a user needs for full Vercel capability; the MCP, if also connected, is a separate optional convenience.

---

## 6. Security invariants

1. Token lives **only** encrypted in `integration_secret_store` and at `skills.entries.vercel.apiKey` (scrubbed). Never in `tools.*` / never published.
2. `_scrub_secret_fields` extended to cover the top-level `tools` block (defense in depth).
3. Guide users toward a **team/project-scoped** Account Token to narrow blast radius.
4. Disconnect deletes the token file (no residue).
5. npx supply-chain: pin an **exact** version; consider `--ignore-scripts` for the pre-warm; document that the bundled skill only ever invokes the pinned package.
6. Per-owner isolation is inherent (per-user EFS access point); the token never crosses the tenant boundary.

---

## 7. Storm safety

The agent runs `npx --yes vercel@<ver>` — extract → `/tmp`, compressed download cache → EFS. Proven storm-safe live (§2.3). **Regression guard:** a test asserting the design never routes a multi-file extract to EFS (no native `kind:node`, no install-to-EFS), mirroring the existing `container-stack.test.ts` cache-routing guards.

---

## 8. Out of scope (v1)

- The **`binary` / `kind:download`** kind (single prebuilt binary on EFS for gh/gog-class tools). When added: registry schema `{arch → {url, sha256, archive_format, bin_path}}`, **mandatory sha256 verification before chmod +x** (fail closed), container-arch resolution, prefer native SSRF-guarded `kind:download`. No real binary tool needs this yet.
- `go` / `uv` kinds.
- Any image or boot-script change.
- Folding the CLI and MCP into a single Vercel integration (§9).

---

## 9. Open decisions

1. **One Vercel or two? — RESOLVED: CLI is primary (GitHub model).** The CLI is the headline Vercel integration — one pasted Account Token, full read+write. The shipped MCP stays as an optional/secondary surface (we don't remove it; it's just not required). The two are separate entries with separate credentials (they can't share one — §5.1).
2. **Deploy identity** (open): capture org+project id at connect (more backend work, smoother first deploy) vs. have the skill run `vercel link --yes` (zero backend, relies on the agent). *Recommendation: capture at connect.*
3. **Skill delivery** (open): seed-at-connect to EFS (flexible, no rebuild) vs. bake the SKILL.md in the image (static). *Recommendation: seed-at-connect.*

---

## 10. Testing / regression guards

- **Exec-host guard** (§4.7): config resolves exec to `gateway` (no sandbox).
- **Storm guard** (§7): no multi-file extract routed to EFS.
- **Secret-scrub test:** a token at `skills.entries.vercel.apiKey` (and, after the hardening, at `tools.*`) is absent from a published marketplace slice.
- **Disconnect residue test:** connect→disconnect leaves no token on EFS.
- **Token validation:** valid token → `GET /v2/user` 200; invalid → rejected at connect.
- **Live behavioral confirm (one-time, dev):** set `skills.entries.<probe>.apiKey` + `primaryEnv`, send one agent message that execs `printenv <VAR>`, expect the value on the first turn after the edit (no restart). Belt-and-suspenders for the source-proven §2.6 chain.

---

## 11. Verification evidence & a side-finding

- **Storm:** measured live on the prod founder container (§2.3) + 3-day fleet telemetry (§2.4). The runtime-install approach does not reintroduce it.
- **Exec host / sandbox / skill-env:** source-traced in `~/Desktop/agent.nosync` + confirmed on the live dev container (§2.5–2.6).
- **Side-finding (not part of this work):** the storm thermometer surfaced an **unrelated provider-auth re-warm bug** blocking the event loop 26–71 s every ~30 min on one prod container (`auth-profile-failure` loop). Tracked separately; worth its own fix.

---

## 12. Why this is small

The heavy lifting — storm-safe npx caching, EFS/`/tmp` split, gateway-host exec, native skill env injection, the credential store, the materializer rail, marketplace scrub for `skills.entries` — **already exists**. This design wires a bundled skill + a token into the existing connect flow. No new infrastructure, no image/boot/RPC change, no new credential rail.
