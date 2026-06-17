# Hermes Runtime Option — Verified Design (supersedes 2026-05-17 audit)

**Date:** 2026-06-16
**Status:** Verified design spec — buildable. Supersedes `docs/audit-2026-05-17-hermes-vs-openclaw.md` (commit `c9562d39`).
**Source basis:** Hermes v0.14.0 at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/hermes-agent`; OpenClaw upstream clone (sibling, gitignored); Isol8 backend/frontend/infra in this worktree; Paperclip read-only clone. Every load-bearing claim cites a `file:line` read during the 11-agent verification pass (`docs/superpowers/research/2026-06-16-hermes-runtime-verification/`).

**Adversarial verification (2026-06-16):** four independent skeptic agents re-read the source to refute the load-bearing claims — SSE resume/framing, `metadata` propagation, ChatGPT-OAuth refresh ownership, OAuth Provider Proxy multi-tenancy, the auth/trust topology, and the Runtime-port caller list. **All conclusions held at that pass — but a later whole-repo pressure-test plus founder review overturned two of them (the agent model and the metering framing). See "Revision 2" immediately below, which supersedes the affected passages.** Six citation/scoping corrections were applied in that pass: (1) the `Last-Event-ID` claim narrowed to the HTTP SSE path (unrelated kanban/Matrix hits exist) + the mid-run stream-orphan bug surfaced; (2) the device-trust evidence narrowed (a DM-user `pairing` subsystem exists but is not operator-device attestation); (3) the `gateway_pool` caller table fixed (`main.py` added, `connection_pool.py` removed as non-caller); (4) `resolve_provider_choice` relocated to `container_provision.py`; (5) `_register_task_definition` def line corrected; (6) the `ProvisioningStepper` phase array corrected to the real 6-phase `STEPS_PAID`. The architecturally load-bearing claims — **4 WS clients, runtime-orthogonal provider gate, flat single-bearer Hermes auth, non-resumable SSE** — are confirmed against source.

> Citation convention: `hermes-agent/...` = Hermes source; bare `apps/backend/...`, `apps/frontend/...`, `apps/infra/...` = this repo; `openclaw/...` = upstream OpenClaw clone; `paperclip/...` = Paperclip clone.

---

## ⚠ Revision 2 (2026-06-17) — corrections after deeper pressure-test (READ FIRST)

A whole-repo pressure-test against a writable fork (**Isol8AI/hermes-agent**, clone `~/Desktop/hermes-agent`, basis `4c46c35e`), prompted by founder review, **overturned two load-bearing claims below.** Where this section conflicts with §1–§11, **this section wins.** Evidence trail: `docs/superpowers/research/2026-06-17-hermes-pressure-test/` + memory `project_hermes_profiles_per_agent`.

**1. Hermes HAS a first-class per-agent model — "profiles." (Reverses §2 Q5, §4, §5, §2 Q8.)**
`hermes_cli/profiles.py` (commit `f6db1b27b`, *"feat: add profiles — run multiple isolated Hermes instances"*, #3681) — each profile is a fully independent `HERMES_HOME` with its **own** config.yaml/.env/SOUL/memory/sessions/skills/cron **+ own gateway daemon + own api_server port + own systemd/launchd service** (`profiles.py:923-961`). The `agent:main` hardcoding (`gateway/run.py:_parse_session_key`) and the `gateway/status.py:9-11` "multi-agent is future" comment describe only the *single-daemon* axis — and that comment is **stale** (profiles already shipped). The earlier maps + attack agents mis-read this twice.
- **Isol8 model:** an agent = a Hermes **profile = its own daemon on its own port** inside the per-owner container → **multi-daemon-per-container** (Friday, Atlas, marketplace agents each a profile). *Stronger* isolation than OpenClaw's shared `agents.list` (per-agent memory/skills/cron/creds/cwd are native).
- **Port methods are implementable, not synthesized:** `create_agent` ≈ `hermes profile create` + start daemon; `list_agents` ≈ list profiles; `send_chat(agent_id)` routes to that profile's port; `delete_agent` ≈ profile delete + service teardown. Backend keeps a thin `agent_id → profile → port` map — **not** the re-implemented agent-orchestration layer the pressure-test report's "WS1b backend agent registry" assumed (that workstream is deleted).
- **Seeding is once-per-profile, not per-run** (reverses §5 Hermes `seed_owner_agents`): write each agent's persona as its profile's default persona in `config.yaml` (resolved to the daemon's `ephemeral_system_prompt` — **uncapped**, sidestepping the 20k SOUL.md cap) at provision. Mirrors OpenClaw's seed-once posture.
- **Per-agent workspace/cwd IS native** (reverses Q8): each profile owns its `TERMINAL_CWD`/`HERMES_HOME`. Capability `per_agent_workspace` = ✅ for Hermes.
- **The real new cost (replaces the false backend-registry workstream):** running N subprocess-heavy daemons on the 0.5 vCPU / 1 GB box. **Default design: lazy per-profile daemon start** (spin a profile's daemon up on first invocation, idle it down) → a typical container runs ~1 active daemon. Adds a multi-daemon lifecycle + port-allocation concern *inside* the container; the `os.environ["HERMES_SESSION_ID"]` last-writer-wins hazard (`agent_init.py:867`) is *avoided* by process-per-profile. **Resourcing N daemons is the headline open risk — feasibility is not in question.** (Founder decision pending on lazy-start vs cap vs always-on; lazy-start is the spec default.)

**2. Metering does NOT need Hermes to report cost. (Reverses §8 metering hook + §11 unknown #3.)**
Isol8 already self-prices Bedrock via `cost_microcents` (`usage_service.py:6,91-96`). The `/v1/runs` `usage` payload's 3 token ints / no-USD (`api_server.py:3046-3049`) is irrelevant — Isol8 prices from tokens. Hermes *does* track cache tokens internally (`agent.session_cache_read_tokens`, `conversation_loop.py:1498-1499`) but omits them from the payload; since Bedrock cost is cache-dominated, the only real gap is an **optional trivial 2-field fork** to surface `cache_read`/`cache_write`. Non-Bedrock (OAuth/BYO) is user-paid → cost moot. No "self-pricing workstream."

**3. Smaller corrections (confirmed against source in the re-run):**
- **Codex creds are clean** — the credential-pool does NOT forage `~/.codex/auth.json`; only the *anthropic* pool path writes back to `~/.claude/.credentials.json` (`credential_pool.py:767-779`). Hardening targets the anthropic/host paths only.
- **Boot enable** — api_server auto-enables on the `API_SERVER_KEY` env var alone (`config.py:1477`); no `platforms.api_server.enabled:true` stanza needed. Prereqs = `API_SERVER_KEY` (≥8, non-placeholder) + `API_SERVER_HOST=0.0.0.0` (else silent loopback no-bind).
- **UID** — container UID 10000 vs EFS-AP 1000 has a clean `HERMES_UID=1000` lever + best-effort non-fatal chown (`entrypoint.sh:13-15,40-41`) — a validation task, not a redesign.
- **EFS still needs hardening** — cross-process locks are `fcntl.flock` (NFS no-op) incl. the gateway boot lock (`status.py:329`); single-writer `maxPercent=100` necessary but **not sufficient**; and **single-writer redeploy itself orphans in-flight `/v1/runs`** (volatile in-process state, no terminal frame, `api_server.py:658-671`) → drain runs before deploy. Now per-profile (each daemon owns its own SQLite dir), so no two daemons share a DB within a task.
- **Drift** — basis is **+201 commits past `v2026.5.16`** (not "v0.14.0"); recent Hermes makes `API_SERVER_KEY` mandatory day-one; releases are 800–1300 commits → **3-5 days/bump**, not 1-2.
- **Webhook ingress** — `gateway/platforms/webhook.py` is a real HMAC-inbound + push-back plugin path that may shrink the bespoke Paperclip adapter (WS7); evaluate before building — *not free* (needs a Python `BasePlatformAdapter` plugin).

**Recalibrated effort:** drop the false "+1.5-2.5wk backend agent-identity layer"; add a multi-daemon lifecycle + lazy-start + resourcing workstream (~1-2wk) and the heavier maintenance tail (3-5 days/bump). Net ≈ **10-14 eng-weeks**, container resourcing of N daemons being the headline open risk.

**Still unverified (rate-limited in both passes):** marketplace Hermes deploy / cron-remap semantics; the effort number lacks an independent adversarial pass.

---

## 1. Summary & decision

**Decision: add a signup-time runtime selector — `OpenClaw | Hermes` — that provisions a Hermes container as a FULL runtime replacement (not ACP delegation), additive and beta-flagged.**

- **Additive, not a migration.** OpenClaw containers and their entire WS-RPC / VPC-Link / device-pairing apparatus stay untouched. Hermes is a second `Runtime` **adapter** behind a per-container `runtime_kind` discriminator; the backend depends on the abstract port + a `Capabilities` descriptor, never on a brand (§4). No OpenClaw teardown. Verified greenfield: a repo-wide grep for `runtime_kind`, `hermes`, `RuntimeBackend` across `apps/backend`, `apps/frontend`, `apps/infra` returns **zero** hits (08-blast-radius §Headline; re-confirmed this pass).
- **Beta-flagged.** New users default to `runtime_kind="openclaw"`. A beta opt-in sets `"hermes"` on the billing row, stamped onto the containers row at provision time. Existing OpenClaw users never auto-migrate.
- **Full runtime swap.** A Hermes container runs `python -m gateway.run` (Hermes daemon), exposes the OpenAI-compat HTTP+SSE API server (`hermes-agent/gateway/platforms/api_server.py`), and replaces the OpenClaw Node daemon entirely. The backend talks to it over HTTP+SSE instead of WS-RPC.
- **The runtime axis is orthogonal to the provider axis.** Hermes consumes Bedrock / ChatGPT-OAuth / BYOK through the *same* `provider_choice`-keyed provision gate; runtime selection adds **no new gate** (11-onboarding §2, grounded in `apps/backend/core/services/provision_gate.py:156-324`).

**What verification changed vs the audit (headline):**

| Audit claimed | Verified reality | Impact |
|---|---|---|
| ChatGPT-OAuth is a "two-file dance" (`auth.json` + `~/.codex/auth.json`, both written on refresh) | **FALSE in v0.14.0.** One runtime store `~/.hermes/auth.json`; `~/.codex/auth.json` is interactive import-only, never written at runtime | Auth writer is simpler; refresh model is identical to OpenClaw today (03-chatgpt-oauth) |
| `metadata` field on `POST /v1/runs` propagates to tools | **FALSE.** Body reads 6 keys; no metadata bag reaches the agent | Paperclip issue/thread context needs a Hermes fork patch (02-metadata) |
| OAuth Provider Proxy wraps Claude Pro/ChatGPT/SuperGrok; could centralize auth | **FALSE.** Nous-only, loopback, unauthenticated, single-tenant | Dropped from scope (05-oauth-provider-proxy) |
| Personalities/`ephemeral_system_prompt` interchangeable; `workspace` is a run/init field | Personalities is a **daemon-global toggle** (one persona at a time); **no `workspace` run field exists** | Seed Friday/Atlas via per-call `ephemeral_system_prompt`; per-agent cwd is soft (04-personality) |
| `_OPENCLAW_RUNTIME_STATE_DIRS` strips `sessions/`,`agent/` in export | **GONE.** Export now tars `workspaces/{agent_id}/` directly | Marketplace §6 mapping rewritten (08-blast-radius Drift #1) |
| RuntimeBackend wraps `connection_pool.py` → covers RPC surface | **FALSE.** There are **4 independent OpenClaw-WS clients**, 3 bypass the pool | OpenClaw adapter must absorb all 4 (08-blast-radius Drift #2) |
| 57 OpenClaw-coupled files | **72 `.py` files** today (133 incl. seed assets) | Larger blast radius |

---

## 2. Verified answers to the 8 open questions

### Q1 — Does Hermes SSE emit `id:` lines (Last-Event-ID resume)?

**Answer: NO. No `id:` lines, no `event:` lines, no replay buffer, no `Last-Event-ID` handling.** The SSE handler writes `data: {json}\n\n` only, with a `: keepalive\n\n` comment every 30s of queue idle and a `: stream closed\n\n` sentinel on completion (`hermes-agent/gateway/platforms/api_server.py:3202-3213`). There is **no `Last-Event-ID` handling in the `/v1/runs/{id}/events` HTTP request path** (the repo's only `last_event_id` hits are in unrelated subsystems — `hermes_cli/kanban_db.py`, `gateway/platforms/matrix.py` — with zero bearing on the HTTP SSE API; do not let a naive repo grep mislead you). The queue (`self._run_streams[run_id]`) is a live `asyncio.Queue` consumed destructively and **popped in the handler's `finally` on ANY disconnect, even mid-run** (`api_server.py:3217-3218`) — so a dropped client orphans the still-executing run's stream irrecoverably (the agent keeps running; only the terminal status survives), and a second `GET .../events` after a drop returns 404 (`api_server.py:3183-3187`). This strengthens the poll-for-terminal recovery recommendation below. All three streaming surfaces (`/v1/runs/{id}/events`, `/v1/chat/completions`, `/v1/responses`) are non-resumable (01-sse-framing §2).

**Contradiction with audit framing:** the audit assumed an "ALB 60s idle timeout" risk. **Both halves are wrong for our stack:** the internal backend ALB idle timeout is already **300s** (`apps/infra/lib/stacks/network-stack.ts:79`), AND the per-user container is **not reached through the ALB at all** — the backend dials the container by direct private IP via ECS `describe_tasks` (`apps/backend/core/containers/ecs_manager.py:1182,1263,1979`). The only ceiling on the client→backend leg is API Gateway WS's hard 10-min idle / 2-hr max (defeated by existing ping/pong) (01-sse-framing §4).

**Design consequence:** the SSE event taxonomy the **Hermes adapter** (Transport facet) must parse is fixed (table below). Recovery on a genuine network blip = **poll `GET /v1/runs/{id}` for terminal `output`/`usage`** (loses intermediate deltas, never the final answer). Gapless mid-run resume would require forking `api_server.py` to add `id:` + a replay ring — defer unless product demands it.

Hermes `/v1/runs/{id}/events` event taxonomy (01-sse-framing §3):

| Event (`"event"` key in JSON) | Emit site | Payload | Maps to internal ChatEvent |
|---|---|---|---|
| `message.delta` | `api_server.py:2958-2963` | `delta` | `chunk` |
| `tool.started` | `:2835-2841` | `tool`, `preview` | `tool_start` |
| `tool.completed` | `:2843-2850` | `tool`, `duration`, `error` | `tool_end` |
| `reasoning.available` | `:2852-2857` | `text` | thinking block |
| `approval.request` | `:2990-2994` | `approval_data` + `choices:["once","session","always","deny"]` | approval prompt |
| `approval.responded` | `:3294-3300` | `choice`, `resolved` | approval resolved |
| `run.completed` | `:3073-3079` | `output`, `usage` | `done` |
| `run.failed` | `:3059-3064`, `:3111-3116` | `error` | `error` |
| `run.cancelled` | `:3094-3098` | — | `error` ("cancelled") |

There is **no** `run.started`/`run.queued` stream event — start is only the 202 body `{"run_id","status":"started"}` (`api_server.py:3152-3155`). "Done" = terminal `run.*` event followed by clean socket close.

### Q2 — Does `POST /v1/runs` `metadata` propagate into the agent so tools can read Paperclip context?

**Answer: NO.** `_handle_runs` (`api_server.py:2862-3156`) parses exactly six body keys — `input`, `instructions`, `previous_response_id`, `conversation_history`, `session_id`, `model` — and silently drops everything else; there is no `body.get("metadata")` (02-metadata §1). `_create_agent` (`api_server.py:851-910`) and `init_agent` (`agent/agent_init.py:74-208`) have no metadata parameter (02-metadata §2).

The only runtime-readable per-run context bag is `gateway/session_context.py`'s fixed contextvar set (`HERMES_SESSION_{PLATFORM,CHAT_ID,CHAT_NAME,THREAD_ID,USER_ID,USER_NAME,KEY,ID}` — `session_context.py:66-78`), read by tools via `get_session_env` (`:133-156`). But on the `/v1/runs` path `set_session_vars` is called with **only** `platform="api_server"` + `session_key` (`api_server.py:3022-3025`); the issue/thread/user slots resolve to `""`.

**Design consequence:** to give a Hermes agent Paperclip issue/thread attribution, **either** (a) carry the context inside the wake **text** (the `input`/`instructions` body — what the OpenClaw adapter does today via `buildWakeText`), which needs no fork; **or** (b) a small Hermes fork patch on the `isol8/*` branch model: read `body.get("metadata")` in `_handle_runs` and forward it into `set_session_vars` (mapping Paperclip `thread_id` onto the already-tool-read `HERMES_SESSION_THREAD_ID` slot). **Recommend (a) for v1** (zero fork), (b) only if tools need structured access. This is the same per-version-bump fork discipline we run for OpenClaw.

### Q3 — ChatGPT-OAuth refresh ownership / write-back

**Answer: container-owns, in-place on EFS, no write-back hook — and this is EXACTLY Isol8's current OpenClaw posture, which we already accept.** Refresh happens in-container: `resolve_codex_runtime_credentials` (`hermes-agent/hermes_cli/auth.py:2861-2903`) → `_refresh_codex_auth_tokens` → `_save_codex_tokens` writes the rotated access+refresh token back into `~/.hermes/auth.json` in place (`auth.py:2806-2824,2823`); Hermes POSTs the refresh itself to `https://auth.openai.com/oauth/token` with the Codex CLI public client_id `app_EMoamEEZ73f0CkXaXp7hrann` (`auth.py:90-91`). **No webhook/event egress** — to learn the rotated token the backend would have to read the EFS file (03-chatgpt-oauth §2-3).

Isol8 already does the identical thing for OpenClaw: `pre_stage_auth_profile_store` writes DDB-decrypted tokens to EFS, `chown 1000:1000`, and never reads back; DDB = seed, on-disk = authoritative after first boot (`apps/backend/core/containers/workspace.py:725-836,752-754`) (03-chatgpt-oauth §3).

**Design consequence:** the refresh-ownership architecture (container-owns, no write-back, DDB-as-seed, re-OAuth = reprovision) carries over unchanged. Only the file path + shape differs.

### Q4 — Hermes `auth.json` exact schema

**Answer: one file, `~/.hermes/auth.json`, `0o600`, parent `0o700`** (`auth.py:780` path; perms in `_save_auth_store` — parent `0o700` at `:1009`, file `0o600` via `O_EXCL` at `:1024`). **CONTRADICTION with audit §5.4** (which described a two-file dance and `"openai-codex:codex-cli"` profile keys) — false for v0.14.0 (03-chatgpt-oauth §CONTRADICTION). Verified shape:

```jsonc
{
  "version": 1,                         // AUTH_STORE_VERSION (auth.py:63)
  "updated_at": "<ISO8601>",
  "active_provider": "openai-codex",    // optional
  "providers": {
    "openai-codex": {
      "auth_mode": "chatgpt",           // auth.py:2697
      "last_refresh": "<ISO8601>",
      "tokens": {
        "access_token":  "<jwt>",       // REQUIRED (auth.py:2666-2674)
        "refresh_token": "<str>",       // REQUIRED (auth.py:2675-2681)
        "id_token":      "<jwt>"        // OPTIONAL
      }
    }
  }
}
```

**No `expires_at` field** — expiry derived from the access-token JWT `exp` claim, refreshed at `exp <= now+120s` (`auth.py:1552-1557,92`). Reader only hard-requires `tokens.access_token` + `tokens.refresh_token` (`_read_codex_tokens`, `auth.py:2658-2681`) (03-chatgpt-oauth §1a, §4).

**Design consequence:** the Hermes auth writer translates the *same* DDB row used today. Mapping:

| DDB (oauth_service) | OpenClaw `credentials/oauth.json` (`workspace.py:781-793`) | Hermes `~/.hermes/auth.json` |
|---|---|---|
| top key | `"openai"` | `providers."openai-codex"` |
| `access_token` | `.access` | `.tokens.access_token` |
| `refresh_token` | `.refresh` | `.tokens.refresh_token` |
| `expires_at` (ms) | `.expires` | **omit** (JWT-derived) |
| `account_id`/`email` | `.accountId`/`.email` | omit (Hermes ignores) |
| — | — | `.auth_mode="chatgpt"`, `.last_refresh=<now>`, envelope `{version:1,providers:{...}}` |

Track `version:1` against `AUTH_STORE_VERSION` (`auth.py:63`) on every Hermes bump — same lockstep discipline as OpenClaw's `_AUTH_STORE_VERSION`.

### Q5 — Can a personality `system_prompt` be ~600 lines without truncation? Behavior delta vs `ephemeral_system_prompt`?

**Answer: `ephemeral_system_prompt` has NO length cap and NO truncation** — concatenated verbatim to the cached system prompt (`hermes-agent/agent/conversation_loop.py:736-740`, `agent/chat_completion_helpers.py:923-927`), only ASCII-sanitized (`conversation_loop.py:1721-1724`) and 60-char log-previewed (`agent_init.py:836-837`). **A personality IS an `ephemeral_system_prompt`** — `/personality <name>` resolves the config entry and stores it as the daemon's single `_ephemeral_system_prompt` (`gateway/run.py:9650-9683,15222-15223,15399`) (04-personality §1-2).

**CRITICAL contrast:** the *file-based* context path (SOUL.md/AGENTS.md) **IS capped at 20,000 chars** head/tail-truncated (`agent/prompt_builder.py:824-826,1292-1301`) **AND injection-scanned** — content tripping the scanner is replaced wholesale with `[BLOCKED: ...]` (`prompt_builder.py:55-73,1324,1363`). A ~600-line (~20-40KB) persona delivered as a SOUL.md file would be silently truncated and possibly blocked; delivered as `ephemeral_system_prompt` it is neither (04-personality §1,§3).

**Behavior delta:** `personalities[]` is **daemon-global, config-mutating, one-persona-at-a-time** (`run.py:9670-9683`) — it cannot host Friday AND Atlas simultaneously. `ephemeral_system_prompt` (run-create `instructions`) is **per-run, never persisted, never shared** (`api_server.py:2978-2984`) — two concurrent runs carry two different personas (04-personality §2).

**Design consequence (corrected by Rev 2 — profiles model):** Friday and Atlas are **separate Hermes profiles** (each its own daemon). Seed each profile's persona **once** as its default personality in that profile's `config.yaml` (resolved to the daemon's `ephemeral_system_prompt` — uncapped, sidestepping the 20k SOUL.md cap). A personality being daemon-global is exactly right here because **one profile = one agent**. Do NOT rely on per-run `ephemeral_system_prompt` resend or shared SOUL.md files. (The "one daemon, two personas via per-run prompt" framing this paragraph originally carried is superseded — see Rev 2 §1.)

### Q6 — OAuth Provider Proxy (v0.14.0): usable in hosted multi-tenant?

**Answer: NO — non-starter, and the audit over-sold it.** `hermes proxy start` is a localhost-bound (`DEFAULT_HOST="127.0.0.1"`, `hermes_cli/proxy/server.py:53`; default port `8645` at `:52`, distinct from api_server's `:8642`), unauthenticated (any bearer accepted and discarded, `proxy/__init__.py:7-10`), single-credential (reads the one global `~/.hermes/auth.json`, `adapters/nous_portal.py:112-122`), **Nous-only** forwarder — the adapter registry has exactly one entry `{"nous": NousPortalAdapter}` (`proxy/adapters/__init__.py:15-17`) (05-oauth-provider-proxy §1-2).

**CONTRADICTION (flag loudly):** the v0.14.0 release notes (`RELEASE_v0.14.0.md:14,190`) AND the prior audit (§5.4) both claim it wraps "Claude Pro / ChatGPT Pro / SuperGrok." The shipped code wraps only Nous Portal. Planning around proxying ChatGPT-OAuth through it = planning around vaporware (05-oauth-provider-proxy §CONTRADICTION).

**Design consequence:** close audit open-question #6 as **rejected**. Each Hermes container authenticates to its provider directly from its own `auth.json`; a localhost proxy adds a hop for zero benefit. The only reusable artifact is the `UpstreamAdapter` ABC shape (`proxy/adapters/base.py:38-91`) if we ever build our own central proxy.

### Q7 — Skill installation in-container: network, safety scanning, sandboxing

**Answer: real built-in scanner exists but is bypassable; installs are HTTP (not git); marketplace deploys must NOT use the in-container installer.** **CORRECTION to audit:** it cited `hermes_bootstrap.py:288` for `~/.hermes/skills/` — `hermes_bootstrap.py` is a 130-line Windows UTF-8 shim with zero skill code. The real engine is `tools/skills_hub.py` (2830 lines); `SKILLS_DIR = ~/.hermes/skills` is correct (`skills_hub.py:48-49`) (06-skill-install §CORRECTION).

- **Three install paths, different postures** (06-skill-install §1): human CLI (`do_install`, `skills_hub.py:408-611`) does quarantine→scan→policy-gate→confirm→promote; **agent-runtime install is a NO-OP scan by default** (`skill_manager_tool.py:78-102`, `skills.guard_agent_created=False` default, because "the agent can already run the same code via `terminal()`"); bundled/optional skills are offline file copies (25 in-image category dirs).
- **Network:** HTTP via `httpx` (GitHub Contents API / raw URL / registry JSON), NOT `git clone` (`skills_hub.py:469,520,963`); bundled installs need none; remote installs need outbound HTTPS, gated by `is_safe_url` SSRF block + `check_website_access` (`skills_hub.py:126-160`).
- **Scanner** (`tools/skills_guard.py`, 932 lines): exfil, cred-store reads, prompt-injection, hardcoded secrets, invisible-unicode, structural checks; trust matrix (builtin/trusted/community/agent-created) — but `--force` overrides any block (`skills_guard.py:664`), and the agent path is opt-out.
- **No per-skill sandbox** — skill scripts run via the same `terminal()` exec; the container is the only isolation boundary (06-skill-install §2c). MCP config is trusted-by-default, no scan (`hermes_cli/mcp_config.py`) — same posture as OpenClaw.

**Design consequence:** marketplace deploys land as **pre-scanned, pre-scrubbed artifacts un-tarred directly into `SKILLS_DIR`** (mirroring `catalog_service.deploy_from_artifact`), bypassing Hermes's bypassable installer. Add a publish-time `format="hermes-skill"` scanner to `apps/backend/core/services/marketplace_safety.py` — and **port Hermes's own `skills_guard.THREAT_PATTERNS`** server-side (it is a strict superset of ours: adds prompt-injection + invisible-unicode + cred-read patterns). If agents are allowed `hermes skills install github/...` at runtime, lock down ECS egress; otherwise ship bundled + marketplace skills only.

### Q8 — AGENTS.md/SOUL.md/cwd: can the orchestrator set cwd per run via a `workspace` field?

**Answer: NO — largest correction to both brief and audit.** `grep workspace agent/agent_init.py` returns one hit: `agent_init.py:1007 _init_kwargs["agent_workspace"]="hermes"` — a **memory-provider scoping label**, not a filesystem cwd. There is no `workspace` parameter in `init_agent` (`agent_init.py:87-140`) and no `workspace`/`cwd` field on `POST /v1/runs` (only the 6 keys from Q2) (04-personality §4).

cwd is resolved from the **daemon-global `TERMINAL_CWD` env var**, set once at boot from config.yaml `terminal.cwd` (`gateway/run.py:619-622,445`, defaulting to `$HOME`), read live per tool call (`agent/tool_executor.py:118,586`). The only per-call override is the **model-chosen `workdir` tool argument** — agent-driven, not orchestrator-driven (04-personality §4).

Context-file gating (`agent/system_prompt.py:89-98,222-231`):

| `skip_context_files` | `load_soul_identity` | SOUL.md (HERMES_HOME) | Project files (AGENTS.md/CLAUDE.md/.cursorrules from `TERMINAL_CWD`) |
|---|---|---|---|
| False (default) | (ignored) | injected | injected |
| True | False | **not** → `DEFAULT_AGENT_IDENTITY` | not injected |
| True | True | injected | not injected |

**Design consequence (corrected by Rev 2 — profiles model):** per-AGENT workspace/cwd isolation **IS** available — each agent is its own profile with its own `HERMES_HOME` and `TERMINAL_CWD`, set per-profile at provision. So `per_agent_workspace` = ✅ for Hermes (capability table updated). The "no hard per-agent cwd field / accept the limitation" conclusion this paragraph originally carried applied only to the single-daemon multiplexing model and is superseded — see Rev 2 §1. (Per-USER isolation = the container boundary, as before.)

---

## 3. Auth & trust model (priority section)

There are **two physically distinct authenticated hops**, with completely different mechanisms. Conflating them is the recurring error (09-auth-openclaw §headline).

### Three-part topology — OpenClaw TODAY vs Hermes PROPOSED

| Leg | OpenClaw today | Hermes proposed |
|---|---|---|
| **(a) frontend → API GW** | Clerk JWT (or Paperclip service-token JWT) in `?token=`, validated by Lambda authorizer; stamps `{userId,orgId,authKind}` into request context (`apps/infra/lib/stacks/api-stack.ts:447-457`, `apps/infra/lambda/websocket-authorizer/index.py:177-276`, `apps/infra/lambda/ws-connect/index.py:16-17,54-59`) | **IDENTICAL — unchanged.** Pure SaaS-shell, 100% runtime-agnostic (09-auth-openclaw §a) |
| **(b) backend → container** | Per-container random `gateway_token` (`secrets.token_urlsafe(32)`, `ecs_manager.py:1775`) + **KMS-sealed Ed25519 operator device** + `paired.json` + canonical v2 signed-connect handshake over protocol-v4 frames, `ws://<ip>:18789` (`connection_pool.py:490-596`, `core/crypto/operator_device.py:187-264`) | **Single per-container bearer `API_SERVER_KEY`** in `Authorization: Bearer`, `hmac.compare_digest`, `https://<ip>:8642/v1/*` HTTP+SSE. No device, no scopes, no nonce (10-auth-hermes §1-2) |
| **(c) operator/device trust** | Three clients on the same `:18789`, all using the v4 signed-device handshake: backend operator device (`operator.admin`); Paperclip operator-relay (service-token at edge + own pre-paired Ed25519 device on Hop 2); desktop node-host (**backend holds NO key**, pure relay, client signs locally) (09-auth-openclaw §c) | **No analog.** Flat single bearer = full access. Device-attested operator model collapses to "backend holds the key" (10-auth-hermes §4) |

### Hermes `API_SERVER_KEY` — full lifecycle

- **What it is:** a single optional global bearer per Hermes process, validated by `_check_auth` (`hermes-agent/gateway/platforms/api_server.py:742-762`), configured from `platforms.api_server.key` (config `extra`) or `API_SERVER_KEY` env (`api_server.py:647`). If unset → **all requests allowed** (`:750-751`). Network-exposed binds **refuse to start** without a key ≥8 chars (`api_server.py:3433-3451`) — so a non-loopback Hermes container is guaranteed to have one (10-auth-hermes §1).
- **Mint:** backend generates a per-container secret at provision time — `secrets.token_urlsafe(32)`, parallel to today's `gateway_token` (`ecs_manager.py:1775`).
- **Store:** put it in **AWS Secrets Manager** (per-container secret, referenced by the ECS task definition `secrets` block) — a strict improvement over `gateway_token`, which is stored only in the DDB `containers` row + EFS `openclaw.json` (`config.py:742-745`). The task def injects it as the `API_SERVER_KEY` env var. The backend reads it from Secrets Manager (or caches it on the containers row, KMS-encrypted like `operator_priv_key_enc` at `ecs_manager.py:2293,2320-2327`) to attach on every request.
- **Provision-per-user:** one secret per container, written into the task def at `provision_user_container` time (the same point that does provider-specific pre-task setup, `ecs_manager.py:1438-1496+`).
- **Rotation:** rotate by writing a new Secrets Manager version + re-registering the task def + forcing a single-writer new deployment (maxPercent=100, per `project_efs_sqlite_wal_two_task_corruption`). Simpler than OpenClaw's (which today has effectively no rotation path — `config.py:700-706`).
- **How the Paperclip `hermes_gateway` adapter presents it:** as `Authorization: Bearer <authToken>` on **every** HTTP request including the long-lived SSE GET (Hermes reads header only, **no `?token=` fallback** for SSE — `api_server.py:3173-3199`) (10-auth-hermes §1). The backend synthesizes the adapter config with `authToken=<API_SERVER_KEY>` (see §5).

### What does NOT carry over (source-backed)

| Dropped capability | Why (source) |
|---|---|
| **Desktop pairing (Ed25519 operator-device)** | Hermes has **no** connect-time device attestation, no per-device token issuance, no `deviceTokens[]`, no per-device scopes — the only `ed25519` hits are SSH-key denylists + Matrix E2EE (10-auth-hermes §2). (Scoping note: Hermes *does* ship a `pairing` subsystem — `hermes_cli/pairing.py`, `gateway/pairing.py` — but it is a chat-platform **DM user-authorization allowlist** (one-time codes approving a phone/Signal/iMessage user to *talk to the bot*), categorically NOT operator-device key attestation, and irrelevant to the backend↔container control-plane hop. A bare `grep -ri pairing` returns 50+ such hits — none are a trust binding.) The Tauri pairing flow (`operator_device_id` + `operator_priv_key_enc` on the containers row) targets an OpenClaw-only protocol → **dead against Hermes** |
| **Exec-approval as a *trusted-device*-gated action** | The *mechanism* carries over — Hermes has `POST /v1/runs/{id}/approval` `{choice}` + SSE `approval.request` (`api_server.py:3223-3299`). But the *trust binding* does not: approval is authorized by the **same single bearer** as everything else. There is no "only the paired operator device may approve" (10-auth-hermes §4-2). If we want that policy, the **Isol8 backend** must enforce it (gate which approvals it forwards) — Hermes cannot distinguish callers |
| **Scoped auth (`operator.admin`/`read`/`write`)** | Hermes has exactly one privilege level; the capabilities endpoint self-describes auth as `{"type":"bearer","required":bool}` (`api_server.py:979-982`) (10-auth-hermes §2) |
| **`tui_gateway` WS as the backend channel** | Its `/api/ws` mount is gated by an **ephemeral per-boot random `_SESSION_TOKEN`** (`?token=` query, `web_server.py:3384-3401,86`) behind a default-off flag, loopback-only — not provisionable. **CORRECTION to audit** (which left it "TBD"). The HTTP api_server is the only viable backend channel (10-auth-hermes §3) |

**Net:** the Hermes backend↔container channel is *simpler* but *flatter* — one provisioned bearer, header-based, HTTP+SSE. v1 ships with **desktop pairing and device-attested exec-approval unavailable for Hermes containers** (web app only). Approval still works, gated by the backend's possession of the key.

---

## 4. Runtime port & adapters

**Design principle: ONE `Runtime` contract; OpenClaw and Hermes are interchangeable adapters.** The rest of the backend depends only on the port + a `Capabilities` descriptor and **never names a runtime brand**. Every runtime-specific divergence — wire protocol, on-disk config/auth formats, identity-seeding shape, marketplace artifact codec — is **contained inside `core/runtime/<kind>/`**, not leaked across the tree.

### Package layout

```
core/runtime/
  __init__.py     # registry: get_runtime(runtime_kind) -> Runtime   (the ONLY brand switch point)
  base.py         # Runtime port (Protocol) + Capabilities + shared types
                  #   (ChatEvent, AgentSpec, OwnerProfile, ContainerSpec, Artifact) — all brand-blind
  openclaw/       # adapter #1 — consolidates today's scattered OpenClaw code behind the port
  hermes/         # adapter #2 — new cohesive package
```

### The port — one cohesive adapter implements every facet

```py
class Runtime(Protocol):
    kind: str
    capabilities: "Capabilities"

    # — Transport (live session) —
    async def connect(self, owner_id: str) -> RuntimeConnection: ...
    async def send_chat(self, owner_id: str, agent_id: str, message: str, session_key: str,
                        on_event: Callable[[ChatEvent], Awaitable[None]]) -> ChatResult: ...
    async def abort(self, owner_id: str, run_id: str) -> None: ...
    async def resolve_approval(self, owner_id: str, run_id: str, choice: ApprovalChoice) -> None: ...
    async def list_agents(self, owner_id: str) -> list[Agent]: ...
    async def create_agent(self, owner_id: str, spec: AgentSpec) -> Agent: ...
    async def list_sessions(self, owner_id: str, agent_id: str) -> list[Session]: ...
    async def revoke_provider(self, owner_id: str, provider: str) -> None: ...

    # — Provisioner (artifacts the brand-blind ecs_manager consumes) —
    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec: ...  # image, port, env, healthcheck, EFS layout
    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None: ...   # to EFS
    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None: ...       # to EFS

    # — IdentitySeeder —
    async def seed_owner_agents(self, owner_id: str, profile: OwnerProfile) -> None: ...  # Friday + Atlas

    # — ArtifactCodec (marketplace) —
    async def export_agent(self, owner_id: str, agent_id: str) -> Artifact: ...
    async def deploy_artifact(self, owner_id: str, artifact: Artifact) -> str: ...
    safety_format: str   # which marketplace_safety scanner profile applies

def get_runtime(runtime_kind: str) -> Runtime: ...   # registry — the ONE place that maps kind -> adapter
```

### Capabilities descriptor — what actually kills `if runtime_kind == "hermes"`

Generic code asks a **capability**, never a brand. A new runtime (or a Hermes upgrade that gains a feature) = flip one flag in one adapter, touch nothing else:

| Capability | OpenClaw | Hermes | Brand-blind consumer that queries it |
|---|---|---|---|
| `desktop_pairing` | ✅ | ❌ | `routers/desktop_auth.py`; container-row pairing fields |
| `device_exec_approval` | ✅ | ❌ | approval-forward gate / `ApprovalCard` policy |
| `control_ui_proxy` | ✅ | ❌ | `routers/control_ui_proxy.py` (hides / 404s for Hermes) |
| `resumable_stream` | ✅ | ❌ | reconnect logic (poll-for-terminal when ❌, Q1) |
| `per_agent_workspace` | ✅ | ✅ | native per Hermes profile (own `HERMES_HOME`/cwd) — **Rev 2 flip** |
| `multi_agent` | ✅ `agents.list` | ✅ profiles (daemon-per-agent) | agent CRUD + routing — **Rev 2 flip** |
| `durable_runs` | ✅ | ❌ | in-flight `/v1/runs` lost on task replacement; drain before deploy (Rev 2) |
| `structured_run_context` | ✅ | ❌ | Paperclip context: structured metadata vs wake-text (Q2) |

### Why this is *separation*, not two parallel stacks

- **Shared once** (lives in `base.py` / the generic layer, brand-blind): the `ChatEvent` shape; the canonical `OwnerProfile`; the provisioning orchestration in `ecs_manager` (it consumes `container_spec()`, never builds one); the marketplace state machine; the provision gate; Paperclip synthesis *dispatch*; the usage/metering callback.
- **Adapter-internal** (the irreducible divergence the port exists to *contain*): WS-RPC vs HTTP+SSE; `openclaw.json` vs `~/.hermes/config.yaml`; the two auth-file shapes; markdown-file seeding vs `ephemeral_system_prompt`; the tarball/slice codec. Making these *identical* is not the goal — keeping them behind a stable contract is.

### Migration order — consolidate OpenClaw behind the port FIRST

Workstream 1 is **not** "add an abstraction + wrap chat." It is **extract the port, then move today's scattered OpenClaw specifics behind it** — standalone-valuable cleanup that de-risks Hermes, because the contract is proven by OpenClaw filling it *before* any Hermes code exists. Today's OpenClaw logic is scattered across **four WS clients + four writer/codec modules**; the OpenClaw adapter must absorb all eight.

**(i) Four independent OpenClaw-WS clients** — CORRECTION to audit §8.4, which assumed wrapping `connection_pool.py` covers the surface; **3 of the 4 bypass the pool entirely** (08-blast-radius Drift #2):

1. `core/gateway/connection_pool.py` — the persistent pool; v4 signed-device handshake (`:490`); grew 1761→2152 lines → adapter **Transport** (live session).
2. `routers/container_rpc.py` — own `ws_connect` (`:22`), `_openclaw_handshake` (`:40`) + `_call_gateway_rpc` (`:85`); the generic `POST /rpc` proxy; **never touches the pool** → adapter `list_agents`/`create_agent`/`list_sessions`/generic RPC.
3. `routers/control_ui_proxy.py` — raw HTTP+WS proxy to `http://{ip}:18789/` (`:149/:204/:271`) → gated by `capabilities.control_ui_proxy` (no Hermes target; reconstruct from `/v1` or shelve for beta).
4. `core/services/container_auth.py` (2026-06-13) — raw `ws_connect` (`:62`) to revoke a provider credential → adapter `revoke_provider`.

`GATEWAY_PORT=18789` is triplicated (`ecs_manager.py:37` canonical, `connection_pool.py:25`, `node_connection.py:42`) — collapse into the OpenClaw adapter.

**(ii) Four writer/codec modules** holding OpenClaw on-disk/format specifics → become the OpenClaw adapter's Provisioner / IdentitySeeder / ArtifactCodec facets: `core/containers/config.py` (`write_openclaw_config`), `workspace.pre_stage_auth_profile_store`, `friday_identity.py`, `agent_export.py` + `catalog_service`.

### Verified current caller list (the refactor surface)

**`get_gateway_pool`/`gateway_pool` callers — 11 files** (NOT the audit's "~25"; only 5 are routers) (08-blast-radius §3):

| File | Role |
|---|---|
| `core/containers/__init__.py` | singleton factory |
| `main.py` | lifespan boot — `get_gateway_pool()` at `:24`, `:200` (must also start/stop the Hermes backend) |
| `core/services/admin_service.py` | admin RPC |
| `core/services/catalog_service.py` | marketplace deploy |
| `core/services/node_proxy.py` | per-user node-host tracking |
| `core/services/paperclip_openclaw_pairing.py` | (new) Paperclip device pairing |
| `routers/admin.py` | admin surface |
| `routers/channels.py` | channel RPC |
| `routers/container.py` | container lifecycle |
| `routers/teams/agents.py` | Teams agent CRUD |
| `routers/websocket_chat.py` | primary chat path (`:183/:356/:528/:877/:1197`) |

Plus the 3 non-pool raw clients above. **Every one becomes a `get_runtime(runtime_kind)` call against the port — no caller references a brand.** The OpenClaw transport code relocates under `core/runtime/openclaw/`; the Hermes adapter is `core/runtime/hermes/`; the registry is `core/runtime/__init__.py`.

---

## 5. Adapter facets (Provisioner / auth / IdentitySeeder)

These are **facets of the two adapters from §4**, not free-standing `*_hermes.py` twins bolted next to each OpenClaw function. Each adapter owns its Provisioner (`container_spec`/`write_config`), auth writer (`write_auth`), and IdentitySeeder (`seed_owner_agents`) — all implementing the shared port. The canonical `OwnerProfile` + persona template text live **once** in the generic layer; each adapter only renders them into its own on-disk shape.

| Facet | OpenClaw adapter (`core/runtime/openclaw/`) | Hermes adapter (`core/runtime/hermes/`) |
|---|---|---|
| **`write_config`** | wraps `write_openclaw_config` → `openclaw.json` on EFS (`apps/backend/core/containers/config.py:1209`; file grew to 1349 lines) | `~/.hermes/config.yaml`: `model`, `mcp_servers`, `platform_toolsets.api_server.enabled`, `terminal.cwd` (= EFS workspace), `platforms.api_server.key` (or via env). BYOK → `~/.hermes/.env` lines |
| **`write_auth`** | wraps `pre_stage_auth_profile_store` → EFS `credentials/oauth.json`, `chown 1000` (`workspace.py:725-836`); BYOK inlined into `openclaw.json` | `~/.hermes/auth.json` (Q4 shape), `0o600`/parent `0o700`/UID 1000; BYOK → `~/.hermes/.env`. No write-back; DDB=seed (Q3) |
| **`seed_owner_agents`** | wraps `friday_identity.py` (728 lines): writes IDENTITY/SOUL/USER/BOOTSTRAP markdown into `agents/<id>/agent/`; OpenClaw auto-loads them | **(Rev 2)** Each agent = a profile; write each profile's persona **once** into its `config.yaml` default personality (uncapped) at provision — seeded, not re-sent per run. Per-profile, mirrors OpenClaw's seed-once posture. |

Both `seed_owner_agents` implementations compile from the same canonical `OwnerProfile` (`friday_identity._resolve_owner_profile`, hoisted into the generic layer); the shared persona template renders to markdown (OpenClaw) vs concatenates to one string (Hermes). **No `friday_hermes.py` beside `friday_identity.py`** — the divergence lives inside the Hermes adapter only.

**Verified personality mechanism (Rev 2):** Friday and Atlas are **separate profiles**, each with its persona set **once** in its own `config.yaml` (resolved to that daemon's `ephemeral_system_prompt` — uncapped, unscanned). `personalities[]` being daemon-global is a non-issue (one profile = one agent). SOUL.md files are avoided (20k-capped + injection-scanned).

---

## 6. Provisioning & infra

- **`runtime_kind` on the `containers` row.** `container_repo.upsert` (`apps/backend/core/repositories/container_repo.py:77-94`) is a generic `{**fields}` dict-merge — adding `runtime_kind` is free. This is the runtime-of-record for already-provisioned containers (11-onboarding §3).
- **ECS_manager branch.** The branch point is `background_provision` → `provision_user_container` (`apps/backend/core/services/container_provision.py:32-59`, `ecs_manager.py:1438-1496+`). Add `runtime_kind` to `resolve_provider_choice`'s return (it lives in `apps/backend/core/services/container_provision.py:18`, **not** `ecs_manager.py`) and thread it through `background_provision` → `provision_user_container`; branch on it for **image / config builder / port / healthcheck** parallel to the existing provider branches. The task def is re-registered every provision (`_register_task_definition`, def at `ecs_manager.py:376`; `:1625` is a call site), so the image legitimately differs per provision. `ensure_user_ready` (`user_provision.py:49-138`) needs **no change** (runtime-agnostic) (11-onboarding §3).
- **Hermes image / ECR.** Build a pinned Hermes image (`python -m gateway.run`) → new ECR repo (e.g. `isol8/hermes-extended`), tagged per-env in a `hermes-version.json` mirroring `openclaw-version.json`'s SSOT discipline. Bind api_server on the container IP (non-loopback) with `API_SERVER_KEY` set (forces a key, §3).
- **EFS layout.** Per-user access point maps `/users/{owner_id}/hermes/` → `~/.hermes` inside the container. Contains `config.yaml`, `auth.json`, `.env`, `skills/`, `cron/jobs.json`, `state.db`, `workspace/`. **Note `project_efs_sqlite_wal_two_task_corruption`:** Hermes also uses WAL-SQLite (`state.db`, kanban.db) on EFS — provision with **single-writer maxPercent=100** to avoid two-task corruption, same as OpenClaw 6.6.
- **Ingress — backend-mediated (recommended).** Backend dials the Hermes container by **direct private IP** via ECS `describe_tasks` (same path as OpenClaw, `ecs_manager.discover_ip` at `:1182`) over HTTP+SSE. No new ALB host route, no browser→container SSE (impossible anyway — EventSource can't set the bearer header, §3 / 10-auth-hermes §1). Frontend stays WS→backend; backend translates via the **Hermes adapter**. This keeps the frontend runtime-agnostic (audit §8.2 option (a)).
- **CDK deltas:** Hermes ECR repo; per-container `API_SERVER_KEY` Secrets Manager secret + task-def `secrets` wiring; Hermes task-definition scaffolding (image, `HERMES_HOME=/mnt/efs/users/{owner_id}/hermes`, env). **No teardown** of the WS API Gateway / VPC Link V1 / NLB / `ws-connections` table — all stay for OpenClaw (08-blast-radius §infra: `api-stack.ts:451/474/126/258` intact).

---

## 7. Signup/onboarding UX

There are **two** onboarding surfaces (audit under-described as one) (11-onboarding §1,§5):

1. **`/onboarding/page.tsx`** — Clerk tenancy only (Personal vs Org). Captures **no** provider/billing/runtime. Leave untouched.
2. **`ProvisioningStepper.tsx` on `/chat`** — the real phase machine. The paid-path steps array `STEPS_PAID` (`ProvisioningStepper.tsx:90-97`) is `payment → container → gateway → channels-sidecars → agent → ready` (**6 phases**). There is no standalone `provider` step and no second `channels` step in the array — provider selection is logic *within* the `payment` phase (e.g. `:1393`). Phase progression is derived purely from data.

**Runtime selector placement:** fold it into the **`ProviderPicker` card-grid shown during the `payment` phase**, alongside the provider/credential choice — never as a later step. Grounded reasons (11-onboarding §2):

- The provision gate is **provider-keyed and runtime-orthogonal** (`provision_gate.py:156-324`). Hermes reuses the identical Bedrock/OAuth/BYOK credential (Q2-audit §5). **No `runtime_required` gate is needed** because runtime defaults to OpenClaw.
- Runtime and provider are independent axes; present runtime as a small segment/toggle on the provider screen (default OpenClaw), provider cards underneath.
- Extend `ProviderPicker.handlePick` (def `ProvisioningStepper.tsx:1383`) so its POST body grows from `{provider_choice}` (`:1387-1389`) to `{provider_choice, runtime_kind}` (frontend `lib/providerCards.ts`) — lands on the billing row in the same synchronous pre-Stripe write, no new round-trip.

**What persists where** (11-onboarding §3):

- **Source of truth — billing-accounts row.** Extend `TrialCheckoutRequest` (`apps/backend/routers/billing.py:257-274`) with `runtime_kind: Literal["openclaw","hermes"]="openclaw"`; extend `billing_repo.set_provider_choice` (`billing_repo.py:231-295`) to also `SET runtime_kind`. Rides the same synchronous write (`billing.py:410-419`) + Stripe-metadata webhook backup.
- **Stamped copy — containers row** at provision time (the runtime-of-record).

**Re-provision-to-switch constraint** (11-onboarding §4): switching runtimes is a **destructive re-provision**, not an in-place toggle — the image/task-def/port differ (OpenClaw Node WS `:18789` vs Hermes Python HTTP `:8642`), and EFS layout is not cross-compatible (`openclaw.json`/`agents/` vs `~/.hermes/`). UX for an existing user (Settings/LLM panel): a **"Switch runtime" destructive action** with a confirm dialog ("re-provisions your container; Friday/Atlas + workspace rebuilt; few minutes"), then write `runtime_kind` to the billing row + trigger re-provision with `trigger="recovery"` (skips `payment`, replays the cold-start animation). Credential is reused — **no re-OAuth/BYO/credits** (gate is runtime-orthogonal). Must use single-writer maxPercent=100.

---

## 8. ACP reconciliation

The runtime selector reuses the existing provider/credential plumbing — concretely mapped to `runtime_kind`:

| Reusable plumbing | Where it lives today | How it maps to `runtime_kind` |
|---|---|---|
| **Credential-gated matrix** | `provision_gate.py:156-324` (Layer 1 subscription → Layer 2 provider-picked → Layer 3 provider creds) | **Unchanged.** Gate is provider-keyed, runtime-orthogonal. Hermes user satisfies the identical `oauth_required`/`byo_provider_required`/`credits_required` gate (11-onboarding §2) |
| **`ACP_ENABLED`-style flag** | (beta opt-in pattern) | New beta opt-in → sets `runtime_kind="hermes"` on the billing row; default `"openclaw"` keeps every existing row + legacy fallback safe. No symmetric `runtime_required` gate |
| **Metering hook** | `usage_service` records per-owner/per-member counters → Stripe Meters; `record_usage()` invoked from the usage callback (`core/containers/__init__.py`) | **(Rev 2)** Isol8 **self-prices Bedrock** via `cost_microcents` (`usage_service.py:91-96`) — Hermes USD not needed. The adapter feeds token counts from `run.completed.usage` (3 ints) into `record_usage()`; since Bedrock cost is cache-dominated, an **optional trivial 2-field Hermes fork** surfaces `cache_read/cache_write` (already tracked, `conversation_loop.py:1498-1499`) for accuracy. Non-Bedrock = user-paid (cost moot). Both runtimes feed one ledger. |
| **Provider-specific pre-task setup** | `provision_user_container` OAuth pre-stage (`_pre_stage_oauth_for_user`, `ecs_manager.py:1480`) + BYO secrets (`_build_byo_secrets_for_user`, `:1490`) | Branch by `runtime_kind` to call the Hermes auth/config writers (§5) instead of the OpenClaw ones, after the provider branch |

---

## 9. Marketplace

- **`runtime_kind` filter.** Add `runtime_kind: "openclaw"|"hermes"` to the `marketplace-listings` DDB table (required for new listings; existing rows default `"openclaw"`). `marketplace_search.py` filters listings by the buyer's container `runtime_kind` so the catalog shows only compatible listings. Status state machine (draft→review→published→retired) and admin approval (`routers/marketplace_admin.py`) unchanged (audit §6.4).
- **Cross-runtime deploy rejection.** A buyer on one runtime deploying a listing of the other → **reject with a clear error**, no translation (skills install model, tool-name references, and slice shape differ). A future opt-in `?translate=true` for seller-declared-compatible listings is deferred (audit §6.3).
- **Hermes artifact shape** (corrected for 08-blast-radius Drift #1 — `_OPENCLAW_RUNTIME_STATE_DIRS` is gone; OpenClaw export already tars `workspaces/{agent_id}/`): Hermes runtime-state (`sessions/`, `checkpoints/`, `cron/output/`, `state.db`) lives OUTSIDE the publishable `~/.hermes/workspace/` dir, so the Hermes tarball needs fewer filters than OpenClaw. `hermes-slice.json` carries `personality` (the persona string, NOT a `personalities[]` config entry — it becomes the deployed agent's `ephemeral_system_prompt`), `mcp_servers`, `skills`, `cron_jobs`, `toolset_overrides`. Deploy un-tars into `SKILLS_DIR`/workspace + merges config.yaml under file lock, bypassing Hermes's bypassable in-container installer (Q7).
- **Publish-time safety** (Q7): add `format="hermes-skill"` to `marketplace_safety.py`; port Hermes's `skills_guard.THREAT_PATTERNS` (superset of ours); keep the existing hard-block on credential leaks in the slice (`agent_export.py:462-466`).

---

## 10. Workstreams, effort, risks

Updated from the audit's 10 workstreams / 8-13 eng-weeks. Verification **increased** the estimate: the OpenClaw adapter must absorb 4 WS clients (not 1) plus 4 writer/codec modules, there is a new `credentials/mcp_server.py` rail that writes `openclaw.json` (`core/services/credentials/mcp_server.py:67-77`, new 2026-06-01), and the desktop/exec-approval trust model has no Hermes analog.

| # | Workstream | Audit est. | Revised | Why changed |
|---|---|---|---|---|
| 1 | Runtime port + consolidate OpenClaw behind it (4 WS clients + 4 writer/codec modules) | 2-3 | **3-5** | ports-and-adapters extract; 8 scattered OpenClaw modules to absorb (08 Drift #2); 72 coupled files |
| 2 | Hermes adapter — HTTP+SSE Transport + facets | 1-2 | 1-2 | event taxonomy fixed (Q1); poll-fallback recovery, no resume |
| 3 | Hermes provisioning + CDK + ECR + Secrets Manager key | 1-2 | 1-2 | + per-container `API_SERVER_KEY` secret + single-writer constraint |
| 4 | Hermes config writer (`config.yaml`/`.env`) | 0.5-1 | 0.5-1 | — |
| 5 | Hermes auth writer (`auth.json`) | 0.5-1 | **0.5** | simpler — one file, no two-file dance (Q3/Q4) |
| 6 | Friday/Atlas Hermes seeding (`friday_hermes.py`) | 0.5 | 0.5 | per-call `ephemeral_system_prompt` (Q5) |
| 7 | `hermes_gateway` Paperclip adapter (npm) | 1-2 | 1-2 | ~400-600 LoC; context via wake text not metadata (Q2) |
| 8 | Backend adapter synthesis (`synthesize_hermes_adapter`) | 0.5 | 0.5 | — |
| 9 | Beta-flag + onboarding `runtime_kind` plumbing | 0.5 | 0.5 | folds into ProviderPicker (11) |
| 10 | Marketplace `runtime_kind` filter + Hermes safety scanner | 0.5 | **1** | + publish-time hermes-skill scanner (Q7) |
| — | **Total** | **8-13** | **~10-14 eng-weeks (Rev 2)** | Rev 2 deletes the false backend-registry inflator, adds a multi-daemon lifecycle + lazy-start + resourcing workstream (~1-2wk); maintenance tail 3-5 days/bump; container resourcing of N daemons is the headline open risk |

**Out of scope for v1** (carried from audit §9.2, confirmed): Hermes-flavored seller publish flow; cross-runtime artifact translation; tearing down WS ingress; exposing Hermes kanban to Paperclip (audit §7B.8 option (a) — ignore, matches OpenClaw); **Hermes desktop app + device-attested exec-approval** (no Hermes analog — §3; web app only).

**Risks:**

1. **Hermes release cadence** — v0.10→v0.14 in 30 days. Pin + lag 1-2 releases; budget 1-2 days/bump for the same audit pattern as OpenClaw, including the `AUTH_STORE_VERSION` / event-schema checks.
2. **No device-attested trust** — exec-approval/desktop trust collapses to "backend holds the bearer." If product wants paired-device approval on Hermes, that policy must be built into the Isol8 backend (§3). Risk = scope creep if surfaced late.
3. **Paperclip context via wake-text** — v1 carries issue/thread context in the run text, not structured metadata (Q2). If tools need structured access, a Hermes fork patch is required — schedule it as a known follow-up, not a surprise.
4. **SSE non-resumability** (Q1) — a genuine network blip loses intermediate deltas (final answer recoverable via polling). Acceptable for v1; gapless resume = Hermes fork.
5. **Two runtimes forever** — set a 6-month decide-and-consolidate review at kickoff.
6. **`control_ui_proxy` has no Hermes target** — the control dashboard's RPC-proxy panels (`routers/control_ui_proxy.py`, `container_rpc.py`) have no Hermes equivalent SPA; either reconstruct from `/v1` endpoints or shelve those panels for Hermes beta.

---

## 11. Remaining unknowns (verification could NOT resolve from source)

1. **Live JWT cold-start refresh on Hermes** — Q3/Q4 establish Hermes derives expiry from the access-token JWT `exp` and refreshes at +120s. Whether a *seeded-but-aged* access token (with a still-valid refresh token) triggers a clean silent refresh vs an interactive relogin prompt was reasoned from code but **not run live** (03-chatgpt-oauth §residual). Verify on a booted container before relying.
2. **Hermes `/v1/runs` body schema for Paperclip context end-to-end** — Q2 confirms `metadata` is dropped; the wake-text path (option a) is the OpenClaw analog but the exact `input`/`instructions` formatting that makes a Hermes agent reliably *act on* embedded Paperclip context was not tested (no live run). Spike must validate.
3. **Hermes usage/cost fields precision** — `run.completed` carries `usage` (`api_server.py:3073-3079`) but the exact token-count field names + whether cost-in-USD is present (for the metering hook §8) were not field-verified against a live response. Confirm before wiring `record_usage()`.
4. **ECS egress posture for runtime skill installs** — Q7 establishes remote skill installs need outbound HTTPS gated by `is_safe_url`; whether our per-user ECS SG/NAT currently permits `api.github.com` egress (and whether we *want* it) is a policy decision, not a source fact.
5. **Hermes cron-job ID schema vs OpenClaw's `_remap_cron_jobs_for_deploy`** — the marketplace deploy must remap cron IDs into the new agent; Hermes's `cron/jobs.json` schema was not diffed against OpenClaw's remap logic (audit §6.5 #2 still open).
6. **`tui_gateway` WS viability if we ever want bidirectional steering** — confirmed dashboard-local/ephemeral-token/default-off (10-auth §3), so unusable as-is; whether a fork could make it a provisionable channel (vs sticking with HTTP+SSE) is unexplored — not needed for v1.
7. **OpenClaw `control_ui_proxy` reconstruction cost** — how much of the control dashboard is RPC-only (reconstructible from Hermes `/v1`) vs SPA-coupled was not quantified.

---

## 12. Business-model fit (2026-06-17)

The flat **$50/mo** model = uniform **0.5 vCPU / 1 GB** box; inference via Bedrock-credits / ChatGPT-OAuth / BYO (orthogonal to runtime). Hermes must not break that. **Verdict: fits — via lazy-start single-active-daemon — with one live measurement gating go/no-go.**

- **One Hermes daemon is designed for tiny hardware.** Nous ships it to *"run on a $5 VPS… costs nearly nothing when idle"* (`README.md:15`). Base = Debian + Python 3.13 (uv venv) + Node/npm; Playwright Chromium is baked **outside** the `/opt/data` volume (`PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright`, Dockerfile) → no per-boot browser reinstall. A single idle daemon on the existing box is plausible — same ballpark as OpenClaw today.
- **Concurrency reality favors us.** A user chats with one agent at a time; Friday/Atlas/marketplace agents rarely all compute at once. **Lazy per-profile daemon start** (spin a profile's daemon up on first message, idle it down after inactivity) keeps the box to ~1 active daemon → runtime load ≈ OpenClaw today (already ~72-85% of the fee on the CPU axis, `project_openclaw_mcp_connect_storm_crashloop`). **Always-on multi-daemon is rejected on unit economics** (N Python daemons on a 1 GB box).
- **Inference billing unchanged.** Runtime is orthogonal to provider (§1); Hermes draws the same Bedrock-credits / OAuth / BYO through the same gate + the same `cost_microcents` self-pricing (Rev 2 §2). No new inference-cost path; the $50 math is unchanged. (Hermes's value is its runtime/learning-loop capabilities, not a cheaper box.)
- **Cold-start cost.** Heavy image (Node + Playwright + `[all]` Python extras) → larger ECR pull on task placement (one-time, warm-ECR mitigated) + lazy spin-up latency on first message to an idle agent (seconds). Mitigation: keep Friday's daemon warm, idle the rest.
- **New work: an in-container supervisor.** Lazy-start needs a process that starts the right profile's daemon on demand, allocates its port, health-checks, and idles it. This is the "multi-daemon lifecycle" workstream in the Rev 2 effort (~1-2wk).
- **The ONE must-measure-live item (go/no-go):** idle + under-load **RSS of a single Hermes daemon on the 1 GB box**. Source can't give exact RSS; a ~1-hour spike (boot a Hermes container; measure RSS idle, during a Friday run, during a Playwright task) closes it. If one-daemon idle RSS is materially above OpenClaw's, the answer is a **Hermes-specific box size = a pricing decision**, not a redesign.

**Net:** business-model-compatible on the existing $50 box **assuming (a) lazy-start and (b) one-daemon idle RSS ≈ OpenClaw**. (b) is the single live measurement to take before committing the build.

---

## 13. Ingress & marketplace under the profiles model (2026-06-17, verified)

**Paperclip ingress — keep backend-mediated; the Paperclip adapter stays thin.** Hermes ships a generic `WebhookAdapter` (`gateway/platforms/webhook.py`): HMAC-validated inbound, payload→prompt transform, per-route skills, idempotency cache, rate-limiting, and a `deliver`/`deliver_extra` response push-back (config under `platforms.webhook.extra.routes`). It is a near-exact issue→run→comment-back fit — **but** its `deliver` targets are a fixed built-in set (`_BUILTIN_DELIVER_PLATFORMS`: github/telegram/feishu/…) with **no generic `http_post`/Paperclip target**, so direct Hermes→Paperclip push-back is not free. **Decision: keep the recommended backend-mediated ingress (§6)** — Paperclip → Isol8 backend → (lazy-start the target profile daemon) → `POST /v1/runs` to that profile's port → response routed back to Paperclip. This mirrors today's `openclaw_gateway` path (the Paperclip adapter points at Isol8; the backend does the runtime-specific routing). So the Paperclip-side adapter for Hermes ≈ the existing `openclaw_gateway` adapter **repointed** (thin), and the Hermes-specifics live in the backend's Hermes adapter — **WS7 shrinks vs the audit's bespoke ~400-600 LoC transport.** `webhook.py` remains a viable *direct-ingress alternative* if we ever bypass the backend, but it's not on the v1 path.

**Marketplace deploy + cron — simpler under profiles; cron-remap is MOOT.** Each profile has its own `HERMES_HOME` → its own `cron/jobs.json` (`cron/jobs.py:4,37-39`; job ids `uuid4().hex[:12]`), `skills/`, `SOUL.md`, `config.yaml`. A marketplace deploy = **create a new profile and drop in the agent's self-contained config/skills/cron/SOUL** — there is no shared container-wide cron table, so the OpenClaw "remap cron ids + agent refs into the buyer's shared table" problem **does not exist** (resolves pressure-test unknown #5 and the §9 cron caveat). `create_agent`/`deploy_artifact` for Hermes = profile-create + un-tar into the profile dir; the per-agent isolation that OpenClaw has to *simulate* via id-remapping is **native**. Cross-runtime deploy stays rejected (§9). Net: a simplification vs OpenClaw's shared-container deploy.

---

## Executive summary (10 bullets)

1. **Decision: additive, beta-flagged signup-time `OpenClaw|Hermes` full-runtime selector.** Greenfield (zero `runtime_kind`/`hermes` hits in the repo); no OpenClaw teardown; new users default OpenClaw.
2. **The runtime axis is orthogonal to the provider/credential axis** — Hermes reuses the identical Bedrock/OAuth/BYOK provision gate (`provision_gate.py:156-324`), so **no new gate code** is needed, only a `runtime_kind` field + a `provision_user_container` branch.
3. **Auth collapses from OpenClaw's KMS-sealed Ed25519 operator-device + scoped v4 handshake to a single per-container `API_SERVER_KEY` bearer** (Secrets Manager, header-only, no `?token=` SSE fallback). Simpler but flatter — desktop pairing and device-attested exec-approval have **no Hermes analog** and are dropped for v1 (web only).
4. **Friday/Atlas are separate Hermes profiles (daemon-per-agent), each persona seeded once into its profile `config.yaml`** (uncapped; Rev 2 §1). The earlier "per-run `ephemeral_system_prompt` on one daemon" framing is superseded — Hermes's per-agent model is profiles (#3681), not in-daemon multiplexing.
5. **SSE is non-resumable** — no `id:`/`event:` lines, no replay buffer; recovery = poll `GET /v1/runs/{id}` for the terminal `output` (loses intermediate deltas). The audit's "ALB 60s timeout" premise is wrong (ALB is 300s and not even on the container path).
6. **`metadata` does NOT reach the agent** — Paperclip issue/thread context must ride in the wake text (v1, zero fork) or via a small Hermes fork patch into `session_context` (`HERMES_SESSION_THREAD_ID`).
7. **One `Runtime` port + two interchangeable adapters (OpenClaw/Hermes), NOT two parallel backends** — generic code depends on the port + a `Capabilities` descriptor, never a brand. Consolidating OpenClaw behind the port is workstream 1 (de-risks Hermes); the OpenClaw adapter must absorb FOUR scattered WS clients (`connection_pool`, `container_rpc`, `control_ui_proxy`, `container_auth`; 3 bypass the pool) plus its config/auth/identity/export writers. Only 11 files reference the pool (not the audit's ~25).
8. **Four audit claims falsified:** the OAuth "two-file dance" (one file in v0.14.0), the OAuth Provider Proxy capabilities (Nous-only vaporware, contradicts its own release notes), `_OPENCLAW_RUNTIME_STATE_DIRS` (removed), and the `workspace` run field (does not exist).
9. **ChatGPT-OAuth refresh model is identical to today's OpenClaw posture** (container-owns, in-place EFS write, no write-back, DDB=seed) — not a new problem; one file `~/.hermes/auth.json`, JWT-derived expiry, schema mapped.
10. **Effort ≈ ~10-14 eng-weeks (Rev 2)** — the false backend-agent-registry inflator is removed (profiles are native), a multi-daemon lifecycle + lazy-start workstream is added (§12), and Paperclip ingress + marketplace deploy *shrink* (§13). Maintenance tail 3-5 days/bump. **Headline open risk = resourcing N daemons on the $50 box (§12)**, with one live single-daemon RSS measurement gating go/no-go.

### Claims I am LESS than confident about (feed to adversarial verification)

- **`API_SERVER_KEY` rotation via Secrets Manager + single-writer redeploy is clean** — reasoned from the OpenClaw `gateway_token` + KMS pattern, but no Hermes-side rotation behavior was tested; Hermes reads the key once at boot (`api_server.py:647`), so a rotate definitely needs a restart — confirm no in-flight-run breakage.
- **Per-container `API_SERVER_KEY` belongs in Secrets Manager vs KMS-on-the-containers-row** — I recommended Secrets Manager as "strictly better" than today's `gateway_token` storage; this is a design preference, not source-forced. Either works.
- **Backend-mediated ingress (no ALB host route) is correct for Hermes** — strongly indicated (EventSource can't set the bearer header; backend already dials by direct IP), but I did not verify the Hermes container's non-loopback bind + intra-VPC reachability end-to-end.
- **The Hermes adapter's `send_chat` can feed `record_usage()` faithfully** — depends on `run.completed.usage` field names + cost presence, which I did NOT field-verify (unknown #3).
- **Wake-text Paperclip context is sufficient for Friday/Atlas behavior** — the mechanism exists but reliable agent action on embedded context is untested (unknown #2).
- **`control_ui_proxy` panels are reconstructible from Hermes `/v1`** — flagged as a risk, but the actual reconstruction cost / feasibility is unquantified (unknown #7).
- **`friday_hermes.py` can share `friday_identity._resolve_owner_profile` cleanly** — assumes the template text is separable from the markdown-file rendering; not verified by reading `friday_identity.py`'s internal structure this pass.
- **Hermes also uses WAL-SQLite on EFS the same way OpenClaw 6.6 does** — inferred from `state.db`/`kanban.db` on EFS + the memory note; the actual WAL/`-shm` single-host sensitivity for Hermes was not source-verified, only assumed by analogy.
