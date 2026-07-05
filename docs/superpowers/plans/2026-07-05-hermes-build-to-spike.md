# Hermes Build → Phase-0 Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dev owner provisioned with `runtime_kind="hermes"` can chat with a Hermes-backed Friday **end-to-end in dev** — the Phase-0 spike. That milestone proves the port abstraction holds for a non-OpenClaw runtime, a Hermes container boots in Fargate, HTTP+SSE transport works through our networking, and the four live unknowns (wake-text context, JWT refresh, active-daemon RSS, persona seeding) resolve. Marketplace/ArtifactCodec, the signup UI, prod rollout, and the N-daemon lifecycle are **explicitly out of scope** — they come after the spike proves the concept (see "Beyond this plan").

**Architecture:** The port (`core/runtime/`, shipped in Phase A #1130) already abstracts *talking to a running container*. This plan adds the two remaining port facets a runtime needs to be *launched and seeded* (Provisioner, IdentitySeeder), then adds `core/runtime/hermes/` as the second adapter — HTTP+SSE transport instead of WS-RPC, `config.yaml`/`auth.json` instead of `openclaw.json`, per-profile provisioning — plus the Hermes container image (built from the re-based fork) and the `runtime_kind` routing. No OpenClaw path changes; Hermes is additive and dev-flag-gated.

**Tech Stack:** Python 3.12 FastAPI backend (`apps/backend/`, uv), AWS CDK TypeScript (`apps/infra/`), the Isol8 Hermes fork (`Isol8AI/hermes-agent`, clone at `~/Desktop/hermes-agent.nosync`), ECR, ECS Fargate, EFS, Secrets Manager.

## Global Constraints

- **Additive + dark by default.** New users stay `runtime_kind="openclaw"`; absent field ⇒ openclaw. No existing container migrates. Every phase is independently revertable; nothing user-visible ships until the spike validates. This mirrors the ACP `ACP_ENABLED` dark-launch pattern (`service-stack.ts`).
- **Builds off the parked Phase-B stack.** Backend phases (H0, H2, H3) branch off `feat/runtime-port-b4` (the tip of PRs #1142/#1146/#1147/#1148) so they inherit the full caller migration. They land as further stacked PRs and merge *after* B1–B4.
- **Verified source basis.** Every Hermes source fact cited here was verified at fork basis `4c46c35e` (see `docs/superpowers/specs/2026-06-16-hermes-runtime-option-design.md` Rev 3 and `project_hermes_runtime_verification_round3` memory). Hermes moves fast — **re-verify each cited `api_server.py`/`hermes_state.py` line against the re-based fork (H1) before building against it.**
- **Never weaken the zero-behavior-change guarantee for OpenClaw.** H0 is pure refactor: OpenClaw behavior byte-for-byte identical (same test-parity discipline as Phase A/B — targeted suites at baseline count; full suite once per phase; never run frontend vitest).
- **Backend commands from `apps/backend/`**, tests `uv run pytest <path> -v`. Commit convention `feat(hermes):` / `refactor(runtime):`. No PR/task/review references in code comments.
- **The billing trap (verified):** Hermes `run.completed.usage` `input_tokens` is **cache-inclusive**. Meter the non-cached figure, sourced via the H1 fork patch — never the raw `input_tokens`.
- **Two fork patches only** (keep the fork close to upstream): the ~3-line usage cache-fields surface and the ~5-line WAL→DELETE journal-mode env knob. No other fork divergence for the spike.

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

### Task H1.1: Re-base the fork + tag

**Files:** the fork clone `~/Desktop/hermes-agent.nosync` (NOT this repo). Do not commit fork changes into isol8.

**This is spike-shaped — conflicts discovered live, not pre-written.**
- [ ] Decide rebase-vs-pin: either rebase `basis-v0.14.0-spec` onto a recent upstream tag, or pin a recent upstream tag directly and re-apply our patches on top. Given 5,464 commits of drift, **pinning a recent upstream release tag and applying the 2 patches on top is lower-risk than a full rebase** — prefer it unless a patch conflicts.
- [ ] Re-verify the load-bearing `api_server.py` lines (SSE emit sites, `_handle_runs` body keys, `run.completed.usage`) and `hermes_state.apply_wal_with_fallback` still exist at the pinned tag; update the spec's citations if upstream moved them (upstream is actively building the usage/cost surface — the patch may already be partly upstream).
- [ ] Tag `v<version>-isol8.1`. Checkpoint: report the chosen tag + any citation drift before H2 builds against it.

### Task H1.2: The two fork patches

**Files (fork):** `gateway/platforms/api_server.py`, `hermes_state.py` + in-fork tests.
- [ ] **Usage cache-fields (~3 lines):** in `_run_sync`'s `usage` dict (`api_server.py:~3046`), surface `cache_read_tokens`/`cache_write_tokens` and the non-cached `input_tokens` from the already-returned conversation-loop result dict (`conversation_loop.py:~3931`) — so the adapter can meter correctly. In-fork test asserting the payload shape.
- [ ] **WAL→DELETE knob (~5 lines):** in `apply_wal_with_fallback` (`hermes_state.py:128`), honor an env `HERMES_SQLITE_JOURNAL_MODE=delete` at the top to force the NFS-safe rollback journal on EFS. In-fork test.
- [ ] Commit on an `isol8/*` branch; these ride the fork's `docker-release.yml`.

### Task H1.3: Extended Hermes image + version pin

**Files (isol8 repo):**
- Create: `apps/infra/hermes/Dockerfile` (mirror `apps/infra/openclaw/` — `FROM` the fork image, bake `aiohttp` [not a core dep — api_server silently no-ops without it] + any runtime deps)
- Create: `.github/workflows/build-hermes-image.yml` (mirror `build-openclaw-image.yml`; push to ECR `isol8/hermes-extended`)
- Modify: `openclaw-version.json` → add a `hermes` block (`upstream`, `extendedImage`, per-env `dev.tag`/`prod.tag`) OR a sibling `hermes-version.json`; wire the CDK image resolver.

- [ ] Build for the target arch, push to ECR, pin `dev.tag`. Checkpoint: image pullable by a per-user ECS task (anonymous or with the task role's ECR grant — confirm the pull path matches how OpenClaw's extended image is pulled).

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

  No `id:` framing / no resume: on a dropped SSE connection, **poll `GET /v1/runs/{id}` for terminal `output`/`usage`** (loses deltas, never the final answer). `approval.request`/`approval.responded` exist but map to in-stream approvals — surface minimally or defer (capability `device_exec_approval=False`).
- **Do not add speculative event filtering** (memory `feedback_no_speculative_event_filtering`) — translate the known events; pass through unknowns as they are handled for OpenClaw.

- [ ] TDD against a mock SSE server (feed each event type, assert the translated `ChatEvent`s); a drop-mid-run test asserting the poll-fallback yields the terminal result.

### Task H2.3: Provisioner facet for Hermes

**Files:** `core/runtime/hermes/provisioner.py`; test.

**Interface:** implement `container_spec`/`write_config`/`write_auth` for Hermes:
- `container_spec`: the Hermes image (H1 pin), container port `8642`, env `API_SERVER_KEY` (per-container secret, H3), `API_SERVER_HOST=0.0.0.0` (else silent loopback bind — verified), `HERMES_SQLITE_JOURNAL_MODE=delete` (the H1 knob), `HERMES_UID=1000` (EFS-AP match), the EFS mount for `HERMES_HOME`.
- `write_config`: emit `config.yaml` (model/provider block keyed to the owner's `provider_choice`; no secrets in config — see the scrub discipline).
- `write_auth`: stage `auth.json` / provider creds onto EFS (Bedrock via task-role IAM = no secret; BYO key / OAuth per the existing pre-stage).

- [ ] TDD asserting the emitted `config.yaml`/env for each provider path; a schema check that the config is one Hermes will accept (guard against the config-brick class of bug — `reference_openclaw_config_brick`).

### Task H2.4: IdentitySeeder for Hermes (resolve the persona design fork)

**Files:** `core/runtime/hermes/seeder.py`; test.

**Interface:** `seed_owner_agents` for Hermes. **Decision required (the deferred persona fork):** seed each agent's persona either as a `personalities[]` registry entry + `display.personality` pointer, OR by passing `instructions` per `/v1/runs` call. Recommend the **per-run `instructions`** path for the spike (simpler, uncapped, no config-registry surface) and revisit if identity must persist in config. Share `friday_identity._resolve_owner_profile` if H0.3 confirmed it's separable.

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
- [ ] Add `runtime_kind` to the `containers`/`billing_accounts` write path (default `"openclaw"`). `user_provision` + `ecs_manager` already consume the runtime via H0's facets, so the branch is *automatic* once `get_runtime_for_owner` (H0.4) reads the field — verify the Hermes runtime flows through `container_spec`/`write_config`/`seed_owner_agents`. Commit `feat(hermes): runtime_kind on the owner record + provision routing`.

### Task H3.2: per-container `API_SERVER_KEY` + single-writer
- [ ] Mint a per-container `API_SERVER_KEY` (Secrets Manager, mirroring `gateway_token` storage) injected via `container_spec` env. Set **`maxPercent=100`** (single-writer) for Hermes services — WAL-on-EFS + volatile in-flight `/v1/runs` both demand it (`project_efs_sqlite_wal_two_task_corruption`). CDK changes in `container-stack.ts`/`service-stack.ts`; regenerate `deploy.yml` if stack structure changes (`feedback_cdk_pipelines_github_deployyml_drift_check`). Commit.

### Task H3.3: dev-only switch
- [ ] A dev-gated way to set one owner `runtime_kind="hermes"` (debug endpoint or a beta flag on the billing row; 403 in prod like the other debug endpoints). Commit `feat(hermes): dev-only runtime_kind switch (403 in prod)`.

**Phase H3 gate:** full suite green; `ACP_ENABLED`-style, still dark for real users. Open **PR H3** stacked on H2.

---

## Phase H4 — Phase-0 dev spike (the working milestone)

**Not a PR — a live validation run in dev, after H0–H3 merge and the H1 image is pinned.** This is where "Hermes working" is proven and the four live unknowns close.

- [ ] **H4.1 Boot:** flip the dev switch for a throwaway dev owner (own Clerk account, NOT the E2E account), provision, and confirm: the Hermes container reaches RUNNING, `api_server` binds on `0.0.0.0:8642`, `/health` responds, single-writer held. Capture the boot wall-time and **idle daemon RSS in Fargate** (validates the macOS-proxy measurement against real Debian/Fargate).
- [ ] **H4.2 Chat:** send a chat turn through the normal `/chat` path; confirm the SSE→ChatEvent translation renders (chunks, tool markers, done) identically to OpenClaw in the UI. This exercises the whole port end-to-end through our WS→backend→container networking.
- [ ] **H4.3 Close the live unknowns:**
  - **Wake-text context:** give Friday a task with embedded Paperclip issue/thread context in the wake text; confirm the agent *acts on it* (the mechanism is confirmed; reliable action is the open question).
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
