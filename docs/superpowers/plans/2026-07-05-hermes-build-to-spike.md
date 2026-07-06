# Hermes Build → Phase-0 Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dev owner provisioned with `runtime_kind="hermes"` can chat with a Hermes-backed Friday **end-to-end in dev** — the Phase-0 spike. That milestone proves the port abstraction holds for a non-OpenClaw runtime, a Hermes container boots in Fargate, HTTP+SSE transport works through our networking, and the four live unknowns (wake-text context, JWT refresh, active-daemon RSS, persona seeding) resolve. Marketplace/ArtifactCodec, the signup UI, prod rollout, and the N-daemon lifecycle are **explicitly out of scope** — they come after the spike proves the concept (see "Beyond this plan").

**Architecture:** The port (`core/runtime/`, shipped in Phase A #1130) already abstracts *talking to a running container*. This plan adds the two remaining port facets a runtime needs to be *launched and seeded* (Provisioner, IdentitySeeder), then adds `core/runtime/hermes/` as the second adapter — HTTP+SSE transport instead of WS-RPC, `config.yaml`/`auth.json` instead of `openclaw.json`, per-profile provisioning — plus the Hermes container image (built from the re-based fork) and the `runtime_kind` routing. No OpenClaw path changes; Hermes is additive and dev-flag-gated.

**Tech Stack:** Python 3.12 FastAPI backend (`apps/backend/`, uv), AWS CDK TypeScript (`apps/infra/`), the Isol8 Hermes fork (`Isol8AI/hermes-agent`, clone at `~/Desktop/hermes-agent.nosync`), ECR, ECS Fargate, EFS, Secrets Manager.

## Build status — 2026-07-05

**All backend phases are IMPLEMENTED, REVIEWED, and PARKED as stacked PRs (nothing merged).** A final whole-stack integration review (`main..H3b`) returned **"Stack coherent + OpenClaw byte-identical: YES"** — all five cross-phase seams matched (api_server_key field, 3-way base-family name, probe port, registry fail-safety, DTOs); the Hermes config write is confirmed wired (`write_user_configs` → `get_runtime_for_owner(owner).write_config` → `config.yaml`).

Parked stack (merge in order): B1 #1142 ← B2 #1146 ← B3 #1147 ← B4 #1148 ← H0 #1151 ← H2 #1153 ← H1 #1154 ← H3a #1156 ← H3b #1158. Plan #1149.

**Fork changes are committed LOCAL-only (not pushed):** `~/Desktop/hermes-agent.nosync` branch `isol8/v2026.7.1-isol8.1` — usage cache-fields patch, WAL→DELETE knob, and the `docker.yml` repoint so the fork publishes `ghcr.io/isol8ai/hermes-agent`.

**Only H4 (the live dev spike) remains — it needs the user's hands:** merge the stack (or a temp integration branch), `aws sso login --profile isol8-admin`, push the Hermes fork + cut the `v2026.7.1-isol8.1` release (publishes the base image), create the `isol8/hermes-extended` ECR repo, let the build workflow push the extended image, then dev-switch an owner to `runtime_kind="hermes"` and validate H4.2a (transport harness) → H4.2b (chat via /chat). Known live-unknowns are enumerated in the H4 section below.

---

## Global Constraints

- **Additive + dark by default.** New users stay `runtime_kind="openclaw"`; absent field ⇒ openclaw. No existing container migrates. Every phase is independently revertable; nothing user-visible ships until the spike validates. This mirrors the ACP `ACP_ENABLED` dark-launch pattern (`service-stack.ts`).
- **Builds off the parked Phase-B stack.** Backend phases (H0, H2, H3) branch off `feat/runtime-port-b4` (the tip of PRs #1142/#1146/#1147/#1148) so they inherit the full caller migration. They land as further stacked PRs and merge *after* B1–B4.
- **Verified source basis.** Every Hermes source fact cited here was verified at fork basis `4c46c35e` (see `docs/superpowers/specs/2026-06-16-hermes-runtime-option-design.md` Rev 3 and `project_hermes_runtime_verification_round3` memory). Hermes moves fast — **re-verify each cited `api_server.py`/`hermes_state.py` line against the re-based fork (H1) before building against it.**
- **Never weaken the zero-behavior-change guarantee for OpenClaw.** H0 is pure refactor: OpenClaw behavior byte-for-byte identical (same test-parity discipline as Phase A/B — targeted suites at baseline count; full suite once per phase; never run frontend vitest).
- **Backend commands from `apps/backend/`**, tests `uv run pytest <path> -v`. Commit convention `feat(hermes):` / `refactor(runtime):`. No PR/task/review references in code comments.
- **The billing trap (verified):** Hermes `run.completed.usage` `input_tokens` is **cache-inclusive**. Meter the non-cached figure, sourced via the H1 fork patch — never the raw `input_tokens`.
- **Fork changes = two code patches + one CI change** (keep the fork close to upstream): the ~3-line usage cache-fields surface, the ~5-line WAL→DELETE journal-mode env knob, AND a `.github/workflows/docker.yml` change so the fork publishes an image AT ALL. **⚠ Two forks, do not conflate:** the **OpenClaw** fork is `Isol8AI/agent` → `ghcr.io/isol8ai/agent`; the **Hermes** fork is `Isol8AI/hermes-agent` → must publish `ghcr.io/isol8ai/hermes-agent` (NOT `…/agent`). As shipped from upstream, the Hermes fork's `docker.yml` is gated `if: github.repository == 'NousResearch/hermes-agent'` with `IMAGE_NAME: nousresearch/hermes-agent`, so **our fork publishes nothing** — H1 must un-gate it + repoint `IMAGE_NAME` to `ghcr.io/isol8ai/hermes-agent`, mirroring what was already done to the OpenClaw fork's release workflow. No other fork divergence for the spike.

---

## Phase H0 — Complete the port's provisioning facets (backend, OpenClaw-only refactor)

**Why first:** the port can talk to a running container but cannot *launch or seed* one through a brand-blind interface. A Hermes container can't be provisioned until `container_spec()`/`write_config()`/`seed_owner_agents()` exist on the port. This phase is more of the same safe refactor as B1–B4 (OpenClaw fills the new methods from existing code; zero behavior change) and is the hard prerequisite for everything Hermes.

### Task H0.1: Provisioner facet on the port

**Files:**
- Modify: `apps/backend/core/runtime/base.py` (add facet methods + DTOs to the `Runtime` Protocol)
- Modify: `apps/backend/core/runtime/openclaw/adapter.py` (implement by delegating to existing code)
- Test: `apps/backend/tests/unit/runtime/test_openclaw_provisioner.py`

**Interfaces:**
- Produces on the `Runtime` Protocol:
  - `def container_spec(self, owner_id: str, ctx: "ProvisionCtx") -> "ContainerSpec"` — image ref, container port, env, healthcheck, EFS mount layout. OpenClaw fills from `ecs_manager` constants + `openclaw-version.json` resolution (the CDK-resolved image the base task def already uses).
  - `async def write_config(self, owner_id: str, profile: "ConfigProfile") -> None` — OpenClaw delegates to `core.containers.config.write_openclaw_config` (`config.py:1421`).
  - `async def write_auth(self, owner_id: str, creds: "ProviderCreds") -> None` — OpenClaw delegates to `workspace.pre_stage_auth_profile_store` + the BYO-secret pre-stage.
  - DTOs (frozen dataclasses in `base.py`, brand-blind): `ContainerSpec`, `ProvisionCtx`, `ConfigProfile`, `ProviderCreds`. Keep fields to what the spike needs; do NOT model OpenClaw-only concepts on the brand-blind DTO.
- Consumes (OpenClaw side): the existing `write_openclaw_config(...)` signature (`config.py:1421`) and `ecs_manager.provision_user_container` (`ecs_manager.py:1487`) internals.

- [ ] **Step 1: Write the failing test** — assert `get_runtime("openclaw").container_spec("org_1", ctx)` returns a `ContainerSpec` whose `image`/`port`/`env` match what `ecs_manager` uses today (read `ecs_manager.py:1487+` for the real values and assert against them), and that `write_config` calls through to `write_openclaw_config` with the same profile mapping (patch + assert-awaited).
- [ ] **Step 2: Run — expect FAIL** (`AttributeError: container_spec`). `uv run pytest tests/unit/runtime/test_openclaw_provisioner.py -v`
- [ ] **Step 3: Implement** the DTOs in `base.py` and the three methods on `OpenClawRuntime`, each a thin delegation to the existing function. `container_spec` returns the values `ecs_manager` currently inlines — do not change those values.
- [ ] **Step 4: Run — expect PASS**, plus `uv run pytest tests/unit/runtime/ -v` (all prior green).
- [ ] **Step 5: Commit** `refactor(runtime): Provisioner facet (container_spec/write_config/write_auth) on the port`

### Task H0.2: `ecs_manager` consumes `container_spec()` (make provisioning brand-blind at the seam)

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (`provision_user_container` / `write_user_configs` at `:1487` / `:2078`)
- Test: existing `tests/` provisioning suites (baseline first)

**Interface:** `ecs_manager` asks `get_runtime_for_owner(owner_id).container_spec(...)` for the image/port/env/healthcheck instead of building them inline; `write_user_configs` calls `runtime.write_config(...)`. For OpenClaw the resolved values are identical — this is a move, not a change.

- [ ] **Step 1: Baseline** the provisioning suites: `uv run pytest tests/ -k "provision or ecs_manager or container_provision" -v` (record count).
- [ ] **Step 2:** Route `provision_user_container` through `container_spec()` / `write_config()`. Preserve the exact task-def shape, env keys, and the `resolve_running_container` cadence (cold-start side effects — same caution as Phase B).
- [ ] **Step 3: Re-run** the baseline suites — count must match exactly.
- [ ] **Step 4: Commit** `refactor(runtime): ecs_manager provisions via the port's container_spec`

### Task H0.3: IdentitySeeder facet

**Files:**
- Modify: `apps/backend/core/runtime/base.py` + `openclaw/adapter.py`
- Modify: `apps/backend/core/services/user_provision.py` (owner path calls the runtime's seeder)
- Test: `apps/backend/tests/unit/runtime/test_openclaw_seeder.py`

**Interface:** `async def seed_owner_agents(self, owner_id: str, profile: "OwnerProfile") -> None` on the port. OpenClaw delegates to the existing Friday/Atlas seeding — `paperclip_provisioning.provision_org` (`:172`) + `friday_identity._resolve_owner_profile` (`friday_identity.py:561`). **Do not restructure the seeding**; wrap it. (Note: the Phase-B review flagged the `friday_identity._resolve_owner_profile` separability as unverified — read that function fully before wrapping and report if the template text is *not* cleanly separable from the markdown rendering.)

- [ ] **Step 1–5:** TDD wrap (failing test asserting `seed_owner_agents` drives the same `provision_org` call → implement delegation → OpenClaw path unchanged → commit `refactor(runtime): IdentitySeeder facet on the port`).

### Task H0.4: `get_runtime_for_owner` reads `runtime_kind`

**Files:**
- Modify: `apps/backend/core/runtime/__init__.py` (`get_runtime_for_owner`, `:17`)
- Modify: `apps/backend/core/services/container_auth.py` (never-raise audit — see below)
- Test: `apps/backend/tests/unit/runtime/test_registry.py`

**Interface:** `get_runtime_for_owner(owner_id)` reads `runtime_kind` off the owner's `containers`/`billing_accounts` row (default `"openclaw"` when absent/unknown) and returns `get_runtime(kind)`. **This makes the function async-or-fallible** — the exact exposure the Phase-B final review flagged. Two obligations:
1. Keep it **infallible-with-fallback**: any repo error ⇒ log + return the openclaw runtime (never raise), so `container_auth.revoke_provider_auth_in_container`'s never-raise contract survives.
2. If a `runtime_kind` lookup must be `async` (DDB), audit every caller migrated in Phase B — the `.call_rpc`/`.revoke_provider` call sites now `await get_runtime_for_owner(...)`; confirm none breaks a never-raise or sync path. Prefer a **cached sync read** (the container row is already resolved milliseconds earlier on most paths) over making the registry async, to avoid re-touching the Phase-B call sites.

- [ ] **Step 1–5:** TDD (failing test: a row with `runtime_kind="hermes"` returns a hermes runtime once registered, unknown/missing ⇒ openclaw, repo-error ⇒ openclaw + logged → implement → commit `feat(runtime): get_runtime_for_owner resolves runtime_kind (default openclaw, fail-safe)`).

**Phase H0 gate:** full backend suite matches main baseline + 4 new facet tests; OpenClaw provisioning behavior unchanged. Open **PR H0** stacked on `feat/runtime-port-b4`.

---

## Phase H1 — Hermes fork + container image (fork repo + infra, PARALLEL track)

**Why parallel:** independent of the backend refactor; it's the longest, least-certain pole (the fork is thousands of commits behind upstream). Start it alongside H0.

### Task H1.1: Pin the fork tag — RESOLVED (H1.1 investigation, 2026-07-05)

**Files:** the fork clone `~/Desktop/hermes-agent.nosync` (NOT this repo). Do not commit fork changes into isol8.

**Decision: PIN `v2026.7.1` (= v0.18.0, 2026-07-01).** Verified: the fork's `basis-v0.14.0-spec` (`4c46c35e`) is a **pure upstream ancestor with ZERO Isol8 divergent commits** — so there is nothing to rebase and **no apply-conflict surface**; both patches are authored *fresh* on the pinned tag. `v2026.7.1` is the latest annotated release, a clean ancestor of `main` (only 366 unreleased commits behind main tip). Rebase-onto-main is explicitly rejected (fast-forwards to untested main for no benefit).

**Citations re-verified against `v2026.7.1`** (all HOLD; new line numbers in `gateway/platforms/api_server.py` unless noted):
- SSE emit sites: `message.delta` 4023, `tool.started` 3896, `tool.completed` 3904, `reasoning.available` 3913, `run.completed` 4145–4157, `run.failed` 4131/4183, `run.cancelled` 4166. No `id:`/resume (writes `data:` only, 4283; destructive queue pop 4275; queue deleted on disconnect 4288).
- `_handle_runs` body keys: `input` 3944, `instructions` 3952, `previous_response_id` 3953, `conversation_history` 3958, `session_id` 4000, `model` 4036. `metadata` still dropped. `instructions` → `ephemeral_system_prompt` (4007), appended AFTER the built-in prompt.
- `run.completed.usage`: still 3 ints (4117–4121), sourced from `agent.session_prompt/completion/total_tokens`; cache accumulators exist (`agent.session_cache_read_tokens/_write_tokens`).
- `API_SERVER_KEY` read once at boot (779), no rotation; `API_SERVER_HOST` default `127.0.0.1` (89), needs `0.0.0.0`; startup guard requires key ≥16 chars.
- `apply_wal_with_fallback` at `hermes_state.py:341` — single journal-mode chokepoint; no env override upstream; note it gained a read-only WAL probe (369) + on-disk-WAL guard (386) — our `DELETE` knob runs above them so it's unaffected.

- [ ] Create branch `isol8/v2026.7.1-isol8.1` off tag `v2026.7.1` for the patches (H1.2). Local only until the patches + tests are green.

### Task H1.2: The two fork patches

**Files (fork, on `isol8/v2026.7.1-isol8.1`):** `gateway/platforms/api_server.py`, `hermes_state.py` + in-fork tests.
- [ ] **Usage cache-fields (~3 lines):** in the runs usage dict (`api_server.py:4117–4121`, and optionally the twin at 3833–3837), surface `cache_read_tokens`/`cache_write_tokens` (from `agent.session_cache_read_tokens`/`_write_tokens`) + non-cached `input_tokens` (= `input_tokens − cache_read`). Source values confirmed present. In-fork test on the payload shape. **NOTE: this is a short-lived BRIDGE** — upstream `main` already carries `cache_read_tokens/cache_write_tokens/reasoning_tokens/estimated_cost_usd/actual_cost_usd` in the runs usage (`main:api_server.py:1643-1645`); a future main-derived pin retires this patch. Keep it minimal.
- [ ] **WAL→DELETE knob (~6 lines):** at the TOP of `apply_wal_with_fallback` (`hermes_state.py:341`, before the WAL probe at 369), honor env `HERMES_SQLITE_JOURNAL_MODE=delete` → run `PRAGMA journal_mode=DELETE` and return `"delete"`. **Gotcha: `hermes_state.py` does NOT import `os`** — the patch must add `import os` (hence ~6 lines). The on-disk-WAL guard doesn't block it (the knob runs the pragma directly, above the guard). In-fork test.
- [ ] Commit LOCALLY on `isol8/v2026.7.1-isol8.1`; do NOT push until reviewed. These ride the fork's `docker-release.yml`.

### Task H1.3: Extended Hermes image + CDK base task-def family + version pin

**Files (isol8 repo):**
- **First, the fork must publish a base image (H1.2 CI change):** adapt the Hermes fork's `.github/workflows/docker.yml` — remove the `if: github.repository == 'NousResearch/hermes-agent'` gate and set `IMAGE_NAME: ghcr.io/isol8ai/hermes-agent` — then pushing the `isol8/v2026.7.1-isol8.1` tag publishes `ghcr.io/isol8ai/hermes-agent:v2026.7.1-isol8.1`. (Without this the fork builds nothing for us; the upstream `docker.yml` only runs on `NousResearch/hermes-agent`.)
- Create: `apps/infra/hermes/Dockerfile` (mirror `apps/infra/openclaw/` — `FROM ghcr.io/isol8ai/hermes-agent:v2026.7.1-isol8.1` [the Hermes fork's published base — NOT `ghcr.io/isol8ai/agent`, which is OpenClaw], bake `aiohttp` [NOT a core dep — api_server silently no-ops without it, H1.1-confirmed] + runtime deps)
- Create: `.github/workflows/build-hermes-image.yml` (mirror `build-openclaw-image.yml`; push to ECR `isol8/hermes-extended`)
- Modify: `openclaw-version.json` → add a `hermes` block (`upstream`, `extendedImage`, per-env `dev.tag`/`prod.tag`) OR a sibling `hermes-version.json`; wire the CDK image resolver.
- **Create the Hermes CDK base task-def family** in `apps/infra/lib/stacks/container-stack.ts` — `isol8-{env}-hermes` mirroring the OpenClaw base def, but with the Hermes image, container **port 8642** (not 18789), and the Hermes base env (`API_SERVER_HOST=0.0.0.0`, `HERMES_SQLITE_JOURNAL_MODE=delete`, `HERMES_UID=1000`). **This is where image/port/base-env live** (H0.2 finding: the launch path clones from a CDK base def; `container_spec.image` is a task-def *family name*, and the task-def registration is NOT sourced from the facet — it's CDK-owned). Regenerate `deploy.yml` if stack structure changes.

- [ ] Build for the target arch, push to ECR, pin `dev.tag`. Checkpoint: image pullable by a per-user ECS task (match the OpenClaw extended-image pull path), and the `isol8-{env}-hermes` base task def exists in dev.

**Phase H1 gate:** a Hermes image tag exists in ECR and is pinned for dev; the 2 patches are in it; citations re-verified. No isol8 runtime change yet.

---

## Phase H2 — Hermes adapter (backend, `core/runtime/hermes/`)

**Off `feat/runtime-port-b4`, after H0's facets exist.** Implements the port for Hermes. Dark until H3 routes an owner to it.

### Task H2.1: `HermesRuntime` skeleton + Capabilities

**Files:** create `core/runtime/hermes/__init__.py`, `core/runtime/hermes/adapter.py`; modify `core/runtime/__init__.py` (register `get_runtime("hermes")`); test `tests/unit/runtime/test_hermes_registry.py`.

**Interface:** `HermesRuntime` with `kind="hermes"` and `capabilities = Capabilities(desktop_pairing=False, device_exec_approval=False, resumable_stream=False, per_agent_workspace=True, multi_agent=True, durable_runs=False, structured_run_context=False, per_run_model=False)` — the verified Hermes values. Registered so `get_runtime("hermes")` returns it. Import-light (no `core.containers` at module import — the subprocess tripwire test must stay green).

- [ ] TDD: registry returns a hermes runtime with the exact capability flags; `isinstance(rt, Runtime)` once the facets below exist.

### Task H2.2: Transport — HTTP+SSE `send_chat` + event translation

**Files:** `core/runtime/hermes/transport.py` (or in adapter); test `tests/unit/runtime/test_hermes_transport.py`.

**H0-review dependency — the chat transport is NOT on the port yet.** The `Runtime` Protocol today exposes `call_rpc` (OpenClaw WS-RPC) + `sessions`, but **no `send_chat`/SSE facet**, and the hot chat path (`websocket_chat.py`) still talks to the pool directly (that's Phase C, deferred). So for H4's "chat with a Hermes Friday end-to-end" to work, TWO things are needed and must be scoped here:
1. **Add a transport facet to the port** — `async def send_chat(self, owner_id, agent_id, message, session_key, on_event) -> ChatResult` on the `Runtime` Protocol (OpenClaw implements it over the pool; Hermes over HTTP+SSE per below). This is the port's chat seam that Phase C would eventually route the hot path through.
2. **A minimal chat-routing bridge for the spike** — the `/chat` message path must reach `get_runtime_for_owner(owner_id).send_chat(...)` for the Hermes dev owner. This is a *slice* of Phase C (not the full `websocket_chat.py` migration): route just the Hermes owner's turns through the port's `send_chat`, leaving OpenClaw owners on the existing pool path untouched. Decide in H2/H3 whether this is a narrow branch in `websocket_chat` (`if runtime.kind == "hermes"`, acceptable as a spike bridge) or pulling Phase C's `send_chat` migration forward. Flag: **do not** try to do the full Phase-C hot-path migration inside this spike.

**H2.2-review REQUIREMENT — the routing envelope (load-bearing, will silently break rendering if missed):** the Hermes `send_chat` emits ChatEvents whose *visible-content* shape matches OpenClaw's (`chunk`/`tool_start`/`tool_end`/`done`/`error`), but they do **NOT** carry the `runId` / `agent_id` / `session_id` keys the frontend uses for **multi-bubble routing + per-session demux** (`connection_pool.py:716-720,1301-1327`). Also: the Hermes `send_chat` sets `instructions` = the seeded PERSONA (H2.4). The bridge MUST **merge** per-run Paperclip/issue context WITH the persona (persona first, then context) — do NOT replace instructions. And the bridge (or the `on_event` sink it wires) MUST inject the routing keys — `runId` from the `POST /v1/runs` 202 `run_id`, `agent_id`/`session_id` from the request context — before the event reaches the frontend, OR confirm the single-agent spike frontend tolerates their absence. Also: set the **`X-Hermes-Session-Key` header** on the run (not just the `session_id` body field) — the `gateway.session_context` bag that tools read is populated from the header, so Paperclip/thread context needs it there.

**Interface:** `send_chat(owner_id, agent_id, message, session_key, on_event)`:
- POST `/v1/runs` (bearer `API_SERVER_KEY`) with the wake text in `input`/`instructions` (Paperclip context rides the text — `metadata` is dropped; `instructions` → per-run `ephemeral_system_prompt`, appended after the built-in prompt).
- Stream `GET /v1/runs/{run_id}/events` (SSE), translating the **verified event taxonomy** into the internal `ChatEvent` shape:

  | Hermes `event` | → internal | payload |
  |---|---|---|
  | `message.delta` | `chunk` | `delta` |
  | `tool.started` | `tool_start` | `tool`, `preview` |
  | `tool.completed` | `tool_end` | `tool`, `duration`, `error` |
  | `reasoning.available` | thinking block | `text` |
  | `run.completed` | `done` | `output`, `usage` |
  | `run.failed` / `run.cancelled` | `error` | `error` |

  No `id:` framing / no resume: on a dropped SSE connection, **poll `GET /v1/runs/{id}` for terminal `output`/`usage`** (loses deltas, never the final answer). The surface is **two-call**: `POST /v1/runs` → 202 `{run_id, status:"started"}`, then open `GET /v1/runs/{run_id}/events`; a disconnect is *lossy* (re-drive, don't re-attach).
- **In-band approval — MUST handle (H1.1 finding):** Hermes emits `approval.request` (choices `once/session/always/deny`) for host-side dangerous exec and **blocks the run in the executor thread until answered via `POST /v1/runs/{run_id}/approval`**. If the adapter ignores it, the run hangs. For the spike: run the agent under a no-prompt exec policy (Hermes config so it never emits `approval.request`) **or** have the adapter auto-respond `approve` — the container is the user's own sandbox, equivalent trust to OpenClaw's `allowCommands`. Decide in H2.3's `config.yaml`; if config can't fully suppress prompts, the transport auto-approves. (`device_exec_approval=False` capability reflects there's no desktop-attested approval path.)
- **Do not add speculative event filtering** (memory `feedback_no_speculative_event_filtering`) — translate the known events; pass through unknowns as they are handled for OpenClaw.

- [ ] TDD against a mock SSE server (feed each event type, assert the translated `ChatEvent`s); a drop-mid-run test asserting the poll-fallback yields the terminal result.

### Task H2.3: Provisioner facet for Hermes

**Files:** `core/runtime/hermes/provisioner.py`; test.

**Interface:** implement `container_spec`/`write_config`/`write_auth` for Hermes:
- `container_spec`: the Hermes image (H1 pin), container port `8642`, env `API_SERVER_KEY` (per-container secret, H3), `API_SERVER_HOST=0.0.0.0` (else silent loopback bind — verified), `HERMES_SQLITE_JOURNAL_MODE=delete` (the H1 knob), `HERMES_UID=1000` (EFS-AP match), the EFS mount for `HERMES_HOME`.
- `write_config`: emit `config.yaml` (model/provider block keyed to the owner's `provider_choice`; no secrets in config — see the scrub discipline). **Before building this, rename `ConfigProfile.gateway_token`** (H0-review Minor): it's an OpenClaw-transport-named required scalar on a brand-blind DTO, and Hermes has no `gateway_token` (it injects `API_SERVER_KEY` via `container_spec` env, not config). Rename to transport-neutral (e.g. `container_auth_token`) and/or make it optional so the Hermes `write_config` isn't forced to pass a meaningless field. (Small ripple: DTO field + the OpenClaw `write_config` consumer + tests; byte-identical for OpenClaw.)
- `write_auth`: stage `auth.json` / provider creds onto EFS (Bedrock via task-role IAM = no secret; BYO key / OAuth per the existing pre-stage).

- [ ] TDD asserting the emitted `config.yaml`/env for each provider path; a schema check that the config is one Hermes will accept (guard against the config-brick class of bug — `reference_openclaw_config_brick`).

### Task H2.4: IdentitySeeder for Hermes (resolve the persona design fork)

**Files:** `core/runtime/hermes/seeder.py`; test.

**Interface:** `seed_owner_agents` for Hermes. **Decision required (the deferred persona fork):** seed each agent's persona either as a `personalities[]` registry entry + `display.personality` pointer, OR by passing `instructions` per `/v1/runs` call. Recommend the **per-run `instructions`** path for the spike (simpler, uncapped, no config-registry surface) and revisit if identity must persist in config.

**H0.3-review finding — factor out shared owner facts:** `friday_identity._resolve_owner_profile` is NOT cleanly separable AS A WHOLE (kept OpenClaw-internal in H0.3), but its **fact-gathering is brand-blind**: Clerk `get_user`/`get_organization`, `lookup_owner_email`, billing `resolve_provider_choice` — only the *rendering* (`provider_label`, greeting/display fallbacks) is OpenClaw-specific. **Do this in H2.4:** extract a shared `resolve_owner_facts(owner_id) -> {display_name, first_name, email, provider_choice, owner_type, …}`, resolve it ONCE (at the facet/caller), carry it on `OwnerProfile`, and have each runtime render its own persona from those facts. Otherwise the Hermes seeder redoes the identical Clerk/billing lookups and two runtimes can disagree on "who is this owner." This enriches `OwnerProfile` beyond H0.3's `{agents, owner_type}` — an additive DTO change, no OpenClaw behavior change if OpenClaw keeps rendering from the same facts.

- [ ] TDD: seeding produces the persona the transport later sends; Friday/Atlas resolvable.

### Task H2.5: Usage → `record_usage`

**Files:** `core/runtime/hermes/adapter.py` usage hook; test.

**Interface:** on `run.completed`, feed token counts to `record_usage()` using the **non-cache-inclusive** `input_tokens` + `cache_read`/`cache_write` from the H1 patch — never the raw `input_tokens` (the billing trap). Bedrock is invocation-metered independently (AIP-attributed), so this path is analytics; still get it right.

- [ ] TDD asserting the metered figure is the non-cached input, not the cache-inclusive one.

**Phase H2 gate:** hermes adapter unit-complete + full runtime suite green; dark (no owner routed). Open **PR H2** stacked on H0.

---

## Phase H3 — Provisioning + routing (backend + dev switch)

**Off H0/H2.** Wires `runtime_kind` end-to-end so a dev owner can be pointed at Hermes.

### Task H3.1: `runtime_kind` on the owner record + provision branch

**H0-review findings (load-bearing).** H0 makes **EFS, config-write, and auth-stage** brand-blind via the facet, and sources the **healthcheck port/timeout** from the facet — BUT two things stay OpenClaw-hardcoded and are H3's real work:
1. The **task-def registration** (image/port/env/cpu/mem) + **base-family selection** (`EcsManager._base_task_def_family`, `_build_register_kwargs_from_base`) — ecs_manager clones a **CDK-managed base task-def** and image pinning is CDK-owned (IaC SSoT).
2. **`is_healthy` resolves the healthcheck with an EMPTY owner** (`get_runtime_for_owner("")`), so it's pinned to the OpenClaw probe (port 18789). A Hermes container (port 8642) would be probed on 18789 and **never pass readiness** → the H4 boot would stall. (H0.2's "healthcheck is brand-blind" was an overstatement — the *values* come from the facet, but the *resolution* is empty-owner.)

- [ ] Add `runtime_kind` to the `containers`/`billing_accounts` write path (default `"openclaw"`).
- [ ] **Make base-family selection runtime-aware:** `_base_task_def_family` / `_build_register_kwargs_from_base` clone from `isol8-{env}-{runtime_kind}` (the H1.3 CDK Hermes base def carries the Hermes image/port/base-env), and source any per-runtime cpu/mem/env deltas from the runtime. This is the real "brand-blind provisioning" work the facet set up but did not itself complete. Guard with `get_runtime_for_owner` (H0.4) so OpenClaw owners are byte-identical.
- [ ] **Thread the owner through `is_healthy`** (`ecs_manager.py:1292`): `is_healthy(self, ip, owner_id)` → `get_runtime_for_owner(owner_id).container_spec(...).healthcheck`, so a Hermes container is probed on its own port. `is_healthy` is always called from `resolve_running_container` (which has `user_id` and has just primed the cache), so the owner is in scope. Without this a running Hermes container logs perpetual `ip_unhealthy=true` and a provisioning one never transitions to `running` — the spike stalls. Byte-identical for OpenClaw (all owners resolve OpenClaw today).
- [ ] Verify the config-write + seed paths (already facet-routed in H0) flow the Hermes runtime through `write_config`/`seed_owner_agents`.
- [ ] **HERMES_HOME alignment — VERIFIED aligned (H1 review); add the defensive explicit env.** The chain `HERMES_HOME ↔ /opt/data mount ↔ backend write-path` is correct as-shipped: the Hermes fork base Dockerfile sets `ENV HERMES_HOME=/opt/data` (inherited by the extended image), Hermes resolves `$HERMES_HOME/config.yaml`, the CDK mounts the per-owner EFS access point (chroot `/users/{owner_id}`) at `/opt/data`, and the backend writes `config.yaml`/`auth.json`/`personas/` to EFS `/users/{owner_id}/…` → surfaces at `/opt/data/…`. **H3 defensive hardening only:** add an explicit `HERMES_HOME: "/opt/data"` to the CDK task-def env (alongside the API_SERVER_KEY secret wiring) so a future base-image change can't silently move it. (H1's `hermes gateway run` command + `/opt/data` mount + aiohttp bake are H4-boot validations, all favorable.)
- [ ] **Prime the runtime-kind cache on write (H0.4-review requirement):** immediately after setting `runtime_kind="hermes"` on the container row, `await prime_runtime_kind(owner_id, container_row=<the row just written>)`. Note the cache is **process-local across a multi-task backend** — the one-shot prime warms only the writing task; the durable warming is the per-request `prime` on `resolve_running_container` (the `/container/status` ~3s poll + chat/resolve paths). `get_runtime_for_owner` is fail-safe (cold miss → OpenClaw), so the only symptom of a cold task is one self-healing OpenClaw resolution before the first poll — acceptable, but spike validation (H4) must drive a status poll / resolve on the serving task before exercising an adapter-selecting Phase-B RPC (hooks/channels/recover/revoke/admin-sessions).
- [ ] Commit `feat(hermes): runtime_kind provisioning — per-runtime base task-def selection`.

### Task H3.2: per-container `API_SERVER_KEY` + single-writer
- [ ] Mint a per-container `API_SERVER_KEY` (Secrets Manager, mirroring `gateway_token` storage) injected via `container_spec` env. Set **`maxPercent=100`** (single-writer) for Hermes services — WAL-on-EFS + volatile in-flight `/v1/runs` both demand it (`project_efs_sqlite_wal_two_task_corruption`). CDK changes in `container-stack.ts`/`service-stack.ts`; regenerate `deploy.yml` if stack structure changes (`feedback_cdk_pipelines_github_deployyml_drift_check`). Commit.

### Task H3.3: dev-only switch
- [ ] A dev-gated way to set one owner `runtime_kind="hermes"` (debug endpoint or a beta flag on the billing row; 403 in prod like the other debug endpoints). Commit `feat(hermes): dev-only runtime_kind switch (403 in prod)`.

### Task H3.4: the Hermes chat bridge — REQUIRED (H2 whole-branch review found this unowned; without it the spike cannot chat)

**The gap:** `send_chat` exists on the port and Hermes implements it, but **NOTHING calls it** — `routers/websocket_chat.py`'s hot path goes straight to `get_gateway_pool()` → `chat.send` WS-RPC. A Hermes owner (HTTP+SSE, port 8642) on that path fails the WS-RPC handshake. This bridge fell between H2 (built the facet, no caller) and H3.1–H3.3 (which own `runtime_kind`/base-family/switch, NOT chat-message routing). It is the single most load-bearing integration for the spike — H4.2 cannot run without it.

- [ ] **Route the turn:** add a Hermes branch in `websocket_chat` — `if get_runtime_for_owner(owner_id).kind == "hermes": await runtime.send_chat(...)` — leaving OpenClaw owners on the existing pool path UNTOUCHED (a narrow spike branch, NOT the full Phase-C hot-path migration). At the `on_event` sink, **inject the routing envelope** `runId`/`agent_id`/`session_id` (H2.2-review requirement — the frontend needs them for multi-bubble + per-session demux) and **merge** per-run Paperclip/issue context WITH the seeded persona in `instructions` (H2.4-review; persona first, then context; do NOT replace), and set the **`X-Hermes-Session-Key` header**.
- [ ] **The control-plane surface (H2-review #1b):** the `/chat` page on load fires `agents.list`, `sessions.list`, container-status, etc. — all WS-RPC today. For a Hermes owner, `call_rpc`/`sessions` are `NotImplementedError` stubs, so the page may not mount even if `send_chat` works. Decide + implement the minimal answer so the chat window renders: e.g. Hermes-owner branches that return the seeded Friday/Atlas roster for `agents.list` and an empty/`/v1`-backed `sessions.list`, and a container-status that reflects the Hermes container. Keep it minimal — just enough for the page to mount and the turn to route.
- [ ] Commit `feat(hermes): spike chat bridge — route Hermes turns through send_chat + minimal control-plane`.

**Phase H3 gate:** full suite green; `ACP_ENABLED`-style, still dark for real users. Open **PR H3** stacked on H2.

**De-risking option (recommended):** validate the transport BEFORE the UI bridge. A tiny harness that calls `get_runtime_for_owner(dev_owner).send_chat(...)` directly against a booted Hermes container proves the container boots in Fargate + HTTP+SSE streams through our networking + the agent acts on wake-text — the real unknowns — WITHOUT the `websocket_chat`/control-plane plumbing. Do that first (H4.2a below), then the full `/chat` path (H4.2b) once H3.4 lands.

---

## Phase H4 — Phase-0 dev spike (the working milestone)

**Not a PR — a live validation run in dev, after H0–H3 merge and the H1 image is pinned.** This is where "Hermes working" is proven and the four live unknowns close.

- [ ] **H4.1 Boot:** flip the dev switch for a throwaway dev owner (own Clerk account, NOT the E2E account), provision, and confirm: the Hermes container reaches RUNNING, `api_server` binds on `0.0.0.0:8642`, `/health` responds, single-writer held. Capture the boot wall-time and **idle daemon RSS in Fargate** (validates the macOS-proxy measurement against real Debian/Fargate).
- [ ] **H4.2a Transport (do FIRST — de-risks without UI plumbing):** call `get_runtime_for_owner(dev_owner).send_chat(...)` directly against the booted container (a small harness). Confirm the run POSTs, the SSE streams, `_translate_event` produces the ChatEvents, the agent acts on the wake-text, and usage meters. This proves the container + HTTP+SSE + networking + the whole adapter WITHOUT `websocket_chat`. Confirm here the H2-inferred unknowns: the approval `{"choice":"always"}` field name, the `run.completed.usage` cache fields (fork patch present), and JWT refresh (if OAuth path).
- [ ] **H4.2b Chat via `/chat` (needs H3.4):** send a turn through the normal `/chat` path; confirm the SSE→ChatEvent translation renders (chunks, tool markers, done) identically to OpenClaw in the UI, the routing envelope is injected, and the page mounts (control-plane RPCs answered). End-to-end through WS→backend→container.
- [ ] **H4.3 Close the live unknowns:**
  - **Wake-text context:** give Friday a task with embedded Paperclip issue/thread context in the wake text; confirm the agent *acts on it* (the mechanism is confirmed; reliable action is the open question).
  - **Approval field name (H2.2-review):** confirm live that `POST /v1/runs/{id}/approval` accepts `{"choice":"always"}` — the field name was inferred from the `choices` enum, not re-verified against `_handle_run_approval` source. If the run wedges on a dangerous-exec approval, this is the first thing to check.
  - **Drop-before-approval edge (H2.2-review):** if the SSE drops between an `approval.request` and the auto-approve POST, the run wedges server-side `waiting_for_approval` and the poll never reaches terminal (UI gets a generic error after 30s). Acceptable degradation; note if it shows up.
  - **JWT cold-start refresh** (if testing the ChatGPT-OAuth/Codex provider path): seed an aged-but-valid token; confirm silent refresh, no interactive relogin.
  - **Active-daemon RSS:** measure memory during a real inference turn (the idle number is known; the active working set gates the N-daemon lazy-start decision).
- [ ] **H4.4 Report:** write the spike results to `docs/superpowers/research/2026-07-<dd>-hermes-phase0-spike/` — go/no-go on each unknown, the real Fargate RSS, and the updated effort estimate. Update the memory.

**Success = Hermes working:** a dev owner chats with a Hermes Friday end-to-end, and the four unknowns are answered with live evidence.

---

## Beyond this plan (post-spike, separate plans)

Deliberately excluded so the spike stays the focus; each becomes its own plan once the spike proves the concept:
1. **ArtifactCodec / marketplace for Hermes** — the `export_agent`/`deploy_artifact` facet, the `config.yaml` scrubber, the `jobs.json` cross-tenant-`origin` sanitizer, the Hermes requirements-detector, cross-runtime deploy rejection (all specced in Rev-3 §9/§13).
2. **Signup runtime selector UI** — the beta-flagged `OpenClaw | Hermes` picker, gated by provider-credential availability.
3. **Phase C of the port refactor** — the hot chat-path (`websocket_chat.py`) migration + the `Runtime.resolve()`/multi-send affordance (carry-forward from the Phase-B review). Not a Hermes prerequisite, but retires the last transport straggler.
4. **N-daemon lazy-start lifecycle** — spin per-profile daemons on first message, idle them down; the resourcing design the RSS spike (H4.3) informs.
5. **Prod rollout + billing validation** — promote the Hermes image to prod, validate Bedrock invocation-metering attributes a Hermes turn to the owner, the trial/subscription gate is runtime-orthogonal.

---

## Self-Review

- **Spec coverage:** H0 (facets) + H2 (adapter) + H3 (provisioning/routing) cover the Rev-3 §3–§6 Isol8-side work needed to *launch and chat*; H1 covers the image/fork (§3.1); H4 closes all four Rev-3 "Remaining open" live items. Marketplace (§9/§13) and signup UI are explicitly deferred — flagged, not dropped.
- **Groundedness:** H0 cites real current signatures (`config.py:1421`, `ecs_manager.py:1487/2078`, `friday_identity.py:561`, `paperclip_provisioning.py:172`, `__init__.py:17`); H2 cites the verified SSE taxonomy + `instructions` mechanism. H1/H4 are honestly spike-shaped (fork conflicts + live boot are discovered, not pre-written) and say so.
- **Risk ordering:** the biggest unknown (does Hermes boot + chat in our AWS env) is attacked at H4 as early as the prerequisites allow; the safe refactor (H0) is the only hard blocker in front of it, and H1 runs in parallel.
- **Re-verify obligation:** H1 must re-check every Hermes `file:line` citation against the re-based fork before H2 builds on them — stated in Global Constraints and H1.1.
