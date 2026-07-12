# Phase B Remainder — Entitlement + Scale-to-Zero + Cap Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Phase B of the no-card free tier: free-tier owners get NO always-on autonomy (sub-spec 2), their idle containers scale to zero event-driven (sub-spec 3), and the remaining $3/day-cap tail holes (conversion, steer, timeout, dedup, expiry, global bound) are closed — making the already-merged Phase A + cap core safe for real users on dev AND prod.

**Architecture:** Three interlocking legs, each simple on its own. **Leg 1 (trial run-boundary):** Paperclip autonomy is *creation-free, execution-capped* — a single pre-run allowance check in the openclaw_gateway adapter's `execute()` (the funnel every Paperclip run passes through: routines, monitors, heartbeats, interactive wakes) blocks runs once the owner is over the daily limit, with BFF cap-402s as the UX layer on click-to-run endpoints; webhooks and channels stay paid-only via `has_always_on_entitlement` (hooks_proxy 402, channels config-write gate); the container's own self-run surfaces (cron service, heartbeat) are config-off for trial. **Leg 2 (scale-to-zero):** event-driven only — extend the existing `_grace_close` (fires on last-client-disconnect) with a trial-only container stop, a one-time boot reconcile for leaked containers, and a ping-deadline for lost `$disconnect`s; the wake path already exists end-to-end (`GET /container/status` auto-retries `stopped` rows, cap-gated). **Leg 3 (cap tail):** conversion forces a cold reprovision off meta (webhook-driven), the cap predicate keys on the container's actual provider, and the steer/timeout/duplicate-event/expiry/global-bound edges are closed. The cap counts and gates; sub-spec 2 guarantees an over-cap container has nothing autonomous to run; sub-spec 3 takes the idle container down. No sweepers, no new tables, no bespoke stop functions.

**Tech Stack:** Python 3 / FastAPI (backend), pytest (+moto for DDB), React 19 / TypeScript, vitest. DynamoDB via existing repos. No new infra.

## Global Constraints

- **Money unit is MICROCENTS** (1 USD = 1,000,000 microcents); `CAP_MICROCENTS = 3_000_000` ($3/day). Constants live in `core/billing/meta_pricing.py` — never re-derive rates inline.
- **`trial_local` is a `subscription_status` on `billing_accounts`, never a `provider_choice`.** `meta` is a config-path provider mode, never stored as provider_choice. `_ACTIVE_STATUSES = {"active", "trialing", "trial_local"}` (provision_gate.py:34) is load-bearing — trial_local must keep passing `is_subscription_active` everywhere it is called today.
- **`has_always_on_entitlement` is a NEW predicate** — do NOT widen or reuse `is_subscription_active` for it.
- **Fail-closed on spend:** unknown cost keeps the worst-case reserve; never refund on ambiguity. Fail-closed on entitlement: missing billing row = not entitled.
- **Event-driven only — no periodic sweepers, no long-running per-owner pollers.** Boot reconcile runs ONCE per lifespan startup. (Memory: feedback_no_long_running_workers_prefer_event_based.)
- **Session keys are lowercased by OpenClaw** (upstream `.toLowerCase()`); every new map keyed by session_key must `.lower()` on both write and read.
- **In-process state is single-backend-instance scope** (fleet runs desiredCount=1); document this on any new in-process dict, same posture as `meta_cap._reserves`.
- **Config emitted for meta must validate against the pinned 7.1 schema** — the chaos test `tests/chaos/invariants/test_config_schema.py` samples `provider_choice="meta"` automatically; run it after any `config.py` change. NEVER emit `commands.btw` (not in schema — crash-loops the container).
- **The 402 error contract** for blocked trial actions is `{"blocked": True, "code": <code>, "message": <human copy>}` (established by the Teams cap guards, issues.py:264-272). New codes introduced here: `always_on_requires_subscription`, `trial_expired`.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Frontend tests are RAM-heavy — run SINGLE FILES only: `cd apps/frontend && node_modules/.bin/vitest run <path>`; after adding/altering frontend tests run `node_modules/.bin/tsc --noEmit`.
- Branch: continue on `feat/free-tier-daily-cap`. Commit per task. NO pushes without user approval.
- **Deliberately OUT of scope** (decided with user, do not add): entitlement guards on Paperclip routine/trigger/monitor CREATION (user 2026-07-11: creation is free; the adapter allowance check bounds execution); Paperclip-side scheduler patches + company trial markers (superseded by the adapter gate); gate_chat autonomous-session-key refusal (cron is config-off; Paperclip is adapter-gated via Task 2b); cron RPC-proxy method guard (redundant with `cron.enabled=false`); `hooks.enabled=false` in config (the proxy 402 is the single line; containers are network-private); config_reconciler revival (spec defers tamper hardening post-v1); desktop wake path + reads-wake-aware + reconnect-budget extension (WS7 desktop polish deferred); cap minors #8 (crash-orphan reserve) and #9 (rollback-strand) — fail-closed, deferred.

---

## Verified wire/anchor reference (grounding workflow wf_c0812aa5, 2026-07-11)

- `gate_chat` — connection_pool.py:1915; Layer 1 subscription :1972-1984, Layer 2 trial cap :1987-1993, Layer 3 credits :1995-2009. Single call site: websocket_chat.py:1909.
- `hooks_proxy.proxy_hook` — routers/hooks_proxy.py; token compare :145-148, rate limit :151. Only external path to container `/hooks` (private subnets).
- Channels config-write gate — routers/config.py:149-155 (`channels_require_subscription`, currently `is_subscription_active` which trial_local passes).
- Teams routine endpoints — routers/teams/work.py: create_routine :44, create_routine_trigger :57, patch_routine :75, run_routine :98. `resume_agent` (re-enables heartbeats) routers/teams/agents.py:720; `wake_agent` :731; `monitor_check_now` issues.py:204.
- Meta config emission — core/containers/config.py: contextTokens :1052, params effort/maxTokens :1070-1073, heartbeat `{"every": "30m", ...}` :1138, `tools.deny=["canvas","nodes","cron"]` :1227, channels `{}` :1331, channel_accounts re-injection :1451-1471.
- Schema-verified lockdown keys (executed against pinned 2026.7.1-beta.3-isol8.1 bundle): `{"cron":{"enabled":false}}` VALID, `{"hooks":{"enabled":false}}` VALID, `{"commands":{"btw":false}}` **INVALID** (crash-loop).
- `/btw` = frontend regex (useAgentChat.ts:1773) → `agent_chat` with `side:true` (websocket_chat.py:885) → passes gate_chat; raw-text path killable per-request via chat.send param `suppressCommandInterpretation: true` (fork chat.ts:3736-3740, :3789, :4521 — suppresses ALL text commands for that send).
- `stop_user_service` ecs_manager.py:739 (desiredCount=0 + status='stopped'); `start_user_service` :810. Wake already live: container.py:183-209 (`retryable_states=('error','stopped')`, cap-402 first :185-197, then auto-retry via background_provision).
- `_grace_close` connection_pool.py:2172-2180 (30s, `_GRACE_PERIOD` :37), armed by `remove_frontend_connection` :2156-2170, cancelled by `add_frontend_connection` :2151-2154. `has_active_runs` :268-270 (webchat-only; veto signal, not idle assertion).
- Node connections count as frontend connections today (websocket_chat.py:387-394 excludes only `operator`); `NODE_HOST_CLIENT_IDS` :272.
- ws-connections DDB: `store_connection` writes NO `ttl` despite table TTL attr (connection_service.py:84-91 vs api-stack.ts:277 — latent zombie-row bug); `query_by_owner` :232-278 returns (connectionId, member_userId, connectionType) via by-owner-id GSI.
- Ping: frontend 30s (packages/shared/src/gateway/useGatewayConnection.ts:11, :273-277); backend pong at websocket_chat.py:632-635 with connection row already in hand :622-629; records nothing today.
- Boot-reconcile precedent: `_resume_provisioning_transitions` main.py:255-276, awaited at :366; `startup_containers()` is a no-op (core/containers/__init__.py:43-45).
- Conversion: `handle_subscription_state` stripe_webhook_handlers.py:196; old_status captured :231, set_subscription :233-238, provider persist :250-260. NOTHING reprovisions on conversion today; hot-swap decision billing_service.py:720-729 (trial→chatgpt takes HOT path); hot swap drops `models.providers` but LEAVES agents.defaults clamps (config_patcher.py:258-263). `resolve_provider_choice` returns meta whenever status==trial_local (container_provision.py:28).
- Usage trigger: lifecycle/end at connection_pool.py:1298-1303 → `_record_usage_from_session` :910; payload carries top-level `runId`; bounded-dict pattern to copy: `_side_run_ids` + `_SIDE_RUN_IDS_CAP` :186-189, :332-354.
- Steer: `_process_agent_steer_background` websocket_chat.py:2193; raw chat.send :2249-2265; same `build_chat_session_key` as parent :2224 (reserve would collide — use read-only check).
- Error tail: `meta_cap.release` call websocket_chat.py:2064-2069; `TimeoutError` except :2017 — but send_rpc transmits BEFORE waiting (connection_pool.py:2057-2059), so timeout ≠ not-delivered.
- Trial expiry: `trial_end = now + 3*24*3600` already stamped (billing_repo.py:195, :215); `trial_expired` enforced NOWHERE (grep=0 hits).
- Stepper: `reachedReady` latch ProvisioningStepper.tsx:280-307 — post-ready never re-polls in-mount; reload remounts and re-polls (wake works on reload). Chat against a stopped container today says "No container provisioned. Please subscribe." (websocket_chat.py:1798-1806) — misleading.

---

## DECISION POINTS (confirm at plan review before execution)

1. **3-day trial expiry — in or out?** `trial_end` is already stamped; Task 10 enforces it (read-time, no worker, no persisted status). With it: worst case ≈ $9/trial. Without: $3/day forever ≈ $90/mo per freeloading owner. Spec says in. **Plan includes it — strike Task 10 to drop.**
2. **Trial webchat loses ALL slash-text-commands** (`suppressCommandInterpretation` suppresses /btw but also /compact etc. for trial sends). Paid unaffected. **Plan assumes acceptable.**
3. **`cron.enabled=false` also stops memory-core dreaming sweeps** for trial (3am memory maintenance). Cost win, small quality loss. **Plan assumes acceptable.**
4. **Heartbeat dropped for trial containers** (closes the *uncounted* 30-min self-run hole — heartbeats run on the webchat-source main session where settle's reconcile no-ops). Trial loses heartbeat-driven memory upkeep. **Plan assumes yes.**

---

# PART 1 — Sub-spec 2: no always-on autonomy for free tier

### Task 1: `has_always_on_entitlement` predicate + hooks_proxy 402 + channels gate swap

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py` (new predicate next to `is_subscription_active`)
- Modify: `apps/backend/routers/hooks_proxy.py` (402 between token compare :148 and rate limit :151)
- Modify: `apps/backend/routers/config.py:151` (swap predicate on the channels gate)
- Test: `apps/backend/tests/unit/services/test_provision_gate.py` (extend), `apps/backend/tests/unit/routers/test_hooks_proxy.py` (extend existing), `apps/backend/tests/unit/routers/test_config_router.py` (extend existing — find actual filenames with `ls tests/unit/routers/`)

**Interfaces:**
- Produces: `has_always_on_entitlement(account: dict | None) -> bool` in `core/services/provision_gate.py` — pure function of the billing row: True iff `account` is not None and `account.get("subscription_status") in {"active", "trialing"}` or (legacy fallback) `account.get("stripe_subscription_id")` is truthy while status is not `trial_local`. False for None, `trial_local`, canceled, past_due. Later tasks import it from `core.services.provision_gate`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_provision_gate.py (append)
from core.services.provision_gate import has_always_on_entitlement

class TestHasAlwaysOnEntitlement:
    def test_active_paid_row_is_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "active"}) is True

    def test_stripe_trialing_row_is_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "trialing"}) is True

    def test_trial_local_is_not_entitled(self):
        # trial_local passes is_subscription_active (chat is free) but must
        # NEVER be always-on entitled — the two predicates are different axes.
        assert has_always_on_entitlement({"subscription_status": "trial_local"}) is False

    def test_missing_row_is_not_entitled(self):
        assert has_always_on_entitlement(None) is False

    def test_canceled_is_not_entitled(self):
        assert has_always_on_entitlement({"subscription_status": "canceled"}) is False

    def test_legacy_row_with_subscription_id_is_entitled(self):
        # mid-cutover rows: no clean status but a live Stripe sub id —
        # mirror is_subscription_active's legacy fallback.
        assert has_always_on_entitlement({"stripe_subscription_id": "sub_123"}) is True
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/services/test_provision_gate.py -v -k AlwaysOn` → FAIL (ImportError).

- [ ] **Step 3: Implement the predicate** in `provision_gate.py`, directly below `is_subscription_active` (match its docstring style — read `is_subscription_active`'s legacy-fallback branch first and mirror its exact condition):

```python
_ENTITLED_STATUSES = {"active", "trialing"}


def has_always_on_entitlement(account: dict | None) -> bool:
    """Paid-only predicate for ALWAYS-ON features (webhooks, channels,
    Paperclip routines, autonomous heartbeats).

    Distinct axis from ``is_subscription_active``: ``trial_local`` owners
    ARE subscription-active (interactive chat and interactive Teams are
    part of the free tier) but are NEVER always-on entitled — autonomy is
    what the $50/mo subscription buys. Fail-closed: no row = not entitled.
    """
    if not account:
        return False
    status = account.get("subscription_status")
    if status in _ENTITLED_STATUSES:
        return True
    # Legacy mid-cutover rows (same fallback is_subscription_active uses),
    # but never for trial_local — that status is definitionally unpaid.
    return status != "trial_local" and bool(account.get("stripe_subscription_id"))
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Failing test — hooks_proxy 402.** In the existing hooks_proxy test module, add: a request with a valid hook token for an owner whose billing row is `trial_local` → 402 with `detail.code == "always_on_requires_subscription"`; an `active` owner → passes through (assert it reaches the container-IP resolution step, i.e. the existing happy-path mock). Mock `billing_repo.get_by_owner_id` the same way neighboring tests mock `container_repo`.

- [ ] **Step 6: Implement.** In `routers/hooks_proxy.py`, after the token compare (:148) and BEFORE the rate limit (:151):

```python
    # 3b. Always-on entitlement — webhooks are a paid feature. Trial owners'
    # tokens are real (every container mints one) but must not trigger runs.
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "always_on_requires_subscription",
                "message": "Webhooks are available on the paid plan. Upgrade to enable always-on automation.",
            },
        )
```

with imports `from core.repositories import billing_repo` and `from core.services.provision_gate import has_always_on_entitlement` (match the module's existing import style).

- [ ] **Step 7: Failing test — channels gate.** Extend the existing PATCH /config channels-gate test: a `trial_local` owner PATCHing a channels block → 403 `channels_require_subscription` (today it PASSES — the new test encodes the fix); an `active` owner still passes.

- [ ] **Step 8: Implement.** In `routers/config.py:151` replace `if not is_subscription_active(account):` with `if not has_always_on_entitlement(account):` (import it; leave `is_subscription_active` import if still used elsewhere in the file — check). Update the comment above (:145-149) to say channels are always-on/paid, not merely "usable subscription".

- [ ] **Step 9: Run all three test files** → PASS. `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 10: Commit** — `feat(billing): add always-on entitlement predicate + webhook/channel paid-only guards`

### Task 2: Teams cap guards on immediate-dispatch endpoints (creation stays free)

**Files:**
- Modify: `apps/backend/routers/teams/work.py` (:98 run_routine)
- Modify: `apps/backend/routers/teams/agents.py` (:731 wake_agent)
- Modify: `apps/backend/routers/teams/issues.py` (:204 monitor_check_now; refactor the existing :264 guard)
- Modify: `apps/backend/routers/teams/interactions.py` (refactor the existing :106 guard)
- Create: `apps/backend/routers/teams/_guards.py`
- Test: `apps/backend/tests/unit/routers/teams/` (extend existing work/agents/issues/interactions test modules — find with `ls`)

**Interfaces:**
- Consumes: `meta_cap.is_over_daily_cap` (existing).
- Produces: `require_under_daily_cap(owner_id)` in `routers/teams/_guards.py`; reused by all four routers.

**Product rule (user, 2026-07-11):** creating routines/triggers/monitors is FREE for trial — no entitlement guards on creation/enable endpoints (`create_routine`, `create_routine_trigger`, `patch_routine`, `resume_agent` all stay open). Execution is what's bounded: Task 2b's adapter gate blocks every Paperclip run once the owner is over the daily cap. The ONLY BFF guards are cap-402s on endpoints that dispatch a run *immediately* on click — so the user gets a clean "daily limit" error at the button instead of a cryptic failed-run card from the adapter:
- `run_routine` (manual fire), `wake_agent` (the failed-run Retry), `monitor_check_now` (immediate re-check) — new guards.
- `add_issue_comment` (issues.py:264-272) and `respond_interaction` (interactions.py:106-114) — refactor the existing inline guards to the shared helper, behavior identical, their tests stay green unchanged.

- [ ] **Step 1: Write failing tests.** For each newly guarded endpoint, two cases: over-cap trial_local owner → 402 `meta_daily_cap`; under-cap (or paid) owner → upstream client called (mock `_agents._admin()` like the existing tests do). Copy the mocking shape from the existing add_issue_comment guard tests.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** New `routers/teams/_guards.py`:

```python
"""Shared over-cap guard for Teams BFF endpoints that dispatch a run NOW.

UX layer only: the authoritative stop is the adapter's pre-run allowance
check (every Paperclip run passes through it, whatever created it). These
guards exist so a click on Run/Retry/Check-now gets a clean 402 instead
of a failed-run card.
"""
from fastapi import HTTPException

from core.services import meta_cap


async def require_under_daily_cap(owner_id: str) -> None:
    if await meta_cap.is_over_daily_cap(owner_id):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "meta_daily_cap",
                "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
            },
        )
```

Wire `await require_under_daily_cap(ctx.owner_id)` as the first await in `run_routine`, `wake_agent`, `monitor_check_now`; replace the two existing inline guards with the helper call.

- [ ] **Step 4: Run the teams test modules** → PASS. Ruff clean.
- [ ] **Step 5: Commit** — `feat(teams): cap guards on immediate-dispatch endpoints; share the over-cap 402 helper`


### Task 2b: Adapter pre-run cap gate — every Paperclip run checks the daily limit (CROSS-REPO)

**Repo:** `~/Desktop/paperclip-fork` (Isol8AI/paperclip — we control it; adapter lives at `packages/adapters/openclaw-gateway/`). Plus one small Isol8-backend endpoint.

**Product rule (user, 2026-07-11):** trial users MAY create routines/triggers/monitors — creation is free. The runs just must not EXECUTE past the daily limit. The `openclaw_gateway` adapter's `execute()` (`src/server/execute.ts:1055`) is the single funnel every Paperclip-initiated run passes through — routine fires, monitor rechecks, heartbeat wakes, interactive comment wakes alike — so ONE pre-run check there covers all sources uniformly, including the paths that never touch our BFF (agent-created triggers via the Paperclip API, the public trigger fire endpoint `routines.ts:440`, board-user-set monitors). No scheduler patches, no company trial marker, no conversion-time cleanup.

**Grounded (2026-07-11, fork + Explore agent):** fresh trial companies have ZERO autonomy day one (per-agent `heartbeat.enabled` defaults false — heartbeat.ts:6680-6690; `provision_org` seeds no runtimeConfig/routines/monitors/plugins), so this gate only ever fires on things the user (or their agent) created later. Adapter config already carries `url` (our wss gateway), an auth token (`resolveAuthToken`, execute.ts:1101), and the `paperclipApiUrl` override precedent (execute.ts:345) for adding a config-supplied endpoint URL. Agent `paused` does NOT stop routines (assertAssignableAgent allows paused, agent-assignability.ts:104-172) — never rely on pause. `HEARTBEAT_SCHEDULER_ENABLED` is global-only — unusable per-tier.

**Files (Isol8 backend):**
- Create: `apps/backend/routers/paperclip_runcheck.py` (or fold into an existing service-token-authed router if one fits — check how `paperclip_proxy.py` / `service_token.py` verify the paperclip_service JWT and reuse that dependency)
- Modify: `apps/backend/core/services/paperclip_adapter_config.py` (adapterConfig gains the check URL, same pattern as `paperclipApiUrl` at :146)
- Test: `tests/unit/routers/test_paperclip_runcheck.py` (new), adapter-config test module (extend)

**Files (fork):**
- Modify: `packages/adapters/openclaw-gateway/src/server/execute.ts` (pre-run check at the top of `execute()`, after config parsing ~:1105, BEFORE dialing the WS)
- Test: `packages/adapters/openclaw-gateway/src/server/execute.test.ts` (extend)

**Interfaces:**
- Produces (backend): `GET /api/v1/paperclip/run-allowance` — authed by the paperclip_service JWT (sub = owner user_id; derive owner_id from it, same identity model as the WS authorizer). Returns `{"allowed": bool, "code": str | null, "message": str | null}` — `allowed:false` with `code:"meta_daily_cap"` when `meta_cap.is_over_daily_cap(owner_id)`; once Task 10 lands, also `code:"trial_expired"` past `trial_end`. Non-trial owners always `allowed:true` (is_over_daily_cap self-gates on trial_local). This endpoint is the semantic "may this owner's agent run right now?" — reuse it, don't fork per-source variants.
- Produces (fork): `execute()` reads `ctx.config.runAllowanceUrl` (optional — absent means no check, so vanilla/paid-without-URL configs are untouched); when present, GETs it with the adapter's auth token (5s timeout). On `allowed:false` → return `{exitCode: 1, errorMessage: message, errorCode: "isol8_run_blocked"}` without connecting. On endpoint error/timeout → FAIL-OPEN with a logged warning (availability for paid companies beats a leaked trial run; the run is still counted by settle and the container may be stopped anyway).
- Produces (backend, seed): `paperclip_adapter_config` sets `runAllowanceUrl` on every seeded agent's adapterConfig (derive from the same env that builds the wss URL — e.g. `https://api-<env>.isol8.co/api/v1/paperclip/run-allowance`). Set it for ALL owners, not just trial — the endpoint says yes for paid, and conversion then needs no adapterConfig edit.

- [ ] **Step 1: Failing backend tests (endpoint):** valid service JWT + over-cap trial owner → `{"allowed": false, "code": "meta_daily_cap", ...}`; under-cap trial → allowed; paid owner → allowed; missing/garbage token → 401.
- [ ] **Step 2: Implement the endpoint** (thin: verify token → owner_id → `meta_cap.is_over_daily_cap` → payload). Mount in main.py next to the other paperclip routers.
- [ ] **Step 3: Failing backend test (seed):** seeded adapterConfig contains `runAllowanceUrl` pointing at the env's API base.
- [ ] **Step 4: Implement** in `paperclip_adapter_config.py`; run → PASS. Ruff clean. **Commit (backend)** — `feat(teams): run-allowance endpoint + adapter config wiring for the Paperclip cap gate`
- [ ] **Step 5: Failing fork tests (adapter):** config with `runAllowanceUrl` + endpoint responding `allowed:false` → execute returns errorCode `isol8_run_blocked` and never opens the WS; `allowed:true` → proceeds; endpoint 500/timeout → proceeds (fail-open) with warning; config WITHOUT the URL → no fetch at all.
- [ ] **Step 6: Implement** the pre-run check in `execute()`; run the adapter test suite → PASS.
- [ ] **Step 7: Commit (fork, `isol8/*` branch per fork convention)** — `feat(adapter): optional pre-run allowance check for openclaw-gateway runs`. **Do NOT deploy the fork without user approval** — flag at the end-of-phase checkpoint. Existing seeded agents lack `runAllowanceUrl` until reprovision/re-seed — dev-only trial rows today, note in ledger.

### Task 3: Config-side trial lockdown (cron off, no heartbeat, no channel re-injection)

**Files:**
- Modify: `apps/backend/core/containers/config.py` (meta branch: :1138 heartbeat, :1451 channel_accounts, new cron block)
- Test: `apps/backend/tests/unit/containers/test_config.py` (extend — confirm filename), then run `uv run pytest tests/chaos/invariants/test_config_schema.py -v` (schema gate)

**Interfaces:** consumes only `provider_choice == "meta"` (already threaded through `build_openclaw_config_dict`).

- [ ] **Step 1: Failing tests** (unit, in the config test module):

```python
def test_meta_config_disables_cron_service():
    config = build_openclaw_config_dict(provider_choice="meta", ...)  # match existing test call shape
    assert config["cron"] == {"enabled": False}

def test_meta_config_omits_heartbeat():
    config = build_openclaw_config_dict(provider_choice="meta", ...)
    assert "heartbeat" not in config["agents"]["defaults"]

def test_paid_config_keeps_cron_and_heartbeat():
    config = build_openclaw_config_dict(provider_choice="bedrock_claude", ...)
    assert "cron" not in config          # unchanged: paid emits no cron block today
    assert config["agents"]["defaults"]["heartbeat"]["every"] == "30m"

def test_meta_config_skips_channel_account_reinjection():
    config = build_openclaw_config_dict(provider_choice="meta", channel_accounts={"telegram": {...}}, ...)
    assert config["channels"] == {}      # saved accounts NOT re-injected for trial
```

(Read the existing meta-branch tests first and reuse their fixture/arg shape verbatim.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** in `build_openclaw_config_dict`:
  - Heartbeat (:1138): wrap the existing dict entry — emit `"heartbeat"` only when `provider_choice != "meta"`. Comment: heartbeat is a recurring self-run on the webchat-source main session, which the free-tier cap's settle path cannot attribute to a reserve — trial containers must not self-run.
  - Cron: in the meta branch add `config["cron"] = {"enabled": False}` (schema-verified key). Comment: kills the in-container scheduler (incl. memory-dreaming registrations); the cron TOOL is already denied fleet-wide via tools.deny.
  - Channel re-injection (:1451): guard the whole `if channel_accounts:` block with `and provider_choice != "meta"` (comment: channels are always-on/paid; a trial reprovision must not resurrect saved accounts — routers/config.py refuses new bindings for trial, this closes the carried-forward path).
- [ ] **Step 4: Run unit tests** → PASS. **Run the chaos schema test** → PASS (proves the keys can't crash-loop the container).
- [ ] **Step 5: Commit** — `feat(billing): lock down trial container config — no cron service, no heartbeat, no channel re-injection`

# PART 2 — Cap tail

### Task 4: Cap keys on the container's provider, not mutable status (fix #4) + gate_chat exposes the trial signal

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:614` (upsert_fields)
- Modify: `apps/backend/core/gateway/connection_pool.py:1987-1993` (Layer 2 predicate)
- Test: `tests/unit/containers/test_ecs_manager*.py`, `tests/unit/gateway/test_gate_chat*.py` (find exact names)

**Interfaces:**
- Produces: container row field `provider_choice: str` stamped at provision; `gate_chat`'s unblocked return becomes `{"blocked": False, "suppress_commands": bool}` where `suppress_commands=True` iff the cap branch identified a meta container (Task 5 consumes it; all other callers ignore extra keys — verify the single call site at websocket_chat.py:1909 destructures nothing else).

- [ ] **Step 1: Failing test (stamp):** `provision_user_container(provider_choice="meta", …)` upserts `provider_choice: "meta"` onto the container row; a `bedrock_claude` provision stamps its own value. (Mirror the existing upsert_fields assertions.)
- [ ] **Step 2: Implement:** add `"provider_choice": provider_choice,` to `upsert_fields` (ecs_manager.py:614 block — the method already receives `provider_choice`).
- [ ] **Step 3: Failing tests (gate):** with billing status flipped to `active` but container row `provider_choice == "meta"` (upgrade window), `gate_chat` still reserves/blocks via the cap; with container row meta and spend at cap → blocked `meta_daily_cap`; with a paid container row (`provider_choice == "bedrock_claude"`) → no reserve placed; unblocked returns include `"suppress_commands": True` for meta-container owners and `False` for paid.
- [ ] **Step 4: Implement** in `gate_chat` Layer 2: replace the `subscription_status == "trial_local"` predicate with a container-row read:

```python
        # --- Layer 2: free-tier Meta $3/day cost cap ------------------
        # Key on the CONTAINER's provisioned provider, not the mutable
        # subscription_status: during the upgrade window the webhook flips
        # status to active minutes before the cold reprovision lands, and
        # the still-meta container must stay counted and capped until then.
        container_row = await container_repo.get_by_owner_id(owner_id)
        is_meta_container = bool(container_row) and container_row.get("provider_choice") == "meta"
        if is_meta_container:
            if await meta_cap.reserve_or_block(owner_id, session_key):
                return {"blocked": True, "code": "meta_daily_cap", "message": ...}  # unchanged copy
        ...
        return {"blocked": False, "suppress_commands": is_meta_container}
```

(Both `return {"blocked": False}` exits get the field. Legacy meta rows provisioned before this task have no `provider_choice` field — they are pre-Phase-A rows (paid) or dev-only trial rows; dev trial rows get re-stamped on next auto-retry/reprovision. Note this in the code comment.)
- [ ] **Step 5: Run** → PASS (update any existing gate_chat tests that mock only billing_repo to also mock `container_repo.get_by_owner_id`). Ruff clean.
- [ ] **Step 6: Commit** — `fix(billing): key the meta cap on the container's provisioned provider`

### Task 5: Webchat command lockdown for trial (`/btw` + all text commands)

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (`_process_agent_chat_background`: side refusal after gate at :1909-1930; `chat_send_params` :1992)
- Test: extend the websocket gate-wiring test module from cap Task 6 (find with `grep -rl gate_chat tests/unit/`)

**Interfaces:** consumes `gate_chat`'s `suppress_commands` (Task 4).

- [ ] **Step 1: Failing tests:** (a) trial (meta-container) owner sending `agent_chat` with `side=True` → refused with error frame `{"type":"error", code:"side_runs_require_subscription"}` and NO chat.send dispatched; (b) trial owner's normal send → `chat_send_params` includes `"suppressCommandInterpretation": True`; (c) paid owner → no suppression key, side allowed.
- [ ] **Step 2: Implement.** After the gate result is known (gate returns at :1909-1930 region):

```python
        suppress_commands = bool(gate.get("suppress_commands"))
        if side and suppress_commands:
            # Free tier: /btw side runs are a concurrent-run surface the
            # single-reserve cap model deliberately doesn't cover. Refuse
            # rather than mis-count.
            management_api.send_message(connection_id, {
                "type": "error", "agent_id": agent_id,
                "code": "side_runs_require_subscription",
                "message": "Side chats (/btw) are available on the paid plan.",
            })
            return
```

and in `chat_send_params` (:1992): `**({"suppressCommandInterpretation": True} if suppress_commands else {})` with a comment that this kills raw-text `/btw`/`/side` (and all other text commands) server-side for trial sends — the fork honors it per-request (chat.ts:3736-3740), so a client that dodges the frontend regex can't open an unmetered side run.
- [ ] **Step 3: Run** → PASS. Ruff clean.
- [ ] **Step 4: Commit** — `feat(billing): refuse side runs + suppress text commands for free-tier webchat`

### Task 6: Conversion forces a cold reprovision off meta

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py` (`handle_subscription_state`, after provider persist :250-260)
- Modify: `apps/backend/core/services/billing_service.py:547` region (`change_provider` refuses trial_local)
- Test: `tests/unit/services/test_stripe_webhook*.py`, `tests/unit/services/test_billing_service*.py` (find exact names)

**Interfaces:** consumes `container_provision.background_provision(owner_id)` (existing; resolve_provider_choice reads the NOW-paid status + stored provider_choice → provisions the chosen provider, full config regen drops meta clamps/provider block/MODEL_API_KEY and re-stamps the Task-4 provider marker).

- [ ] **Step 1: Failing tests:** (a) `handle_subscription_state` with prior row `trial_local` and event status `trialing` → `background_provision` dispatched exactly once for the owner (patch it; assert called); (b) prior row `active` → NOT dispatched (no reprovision churn on ordinary renewal webhooks); (c) prior row `trial_local`, event status `canceled` → NOT dispatched; (d) `change_provider` on a `trial_local` row → HTTPException 409 with `detail.code == "use_trial_checkout"` before any Stripe/dispatch work.
- [ ] **Step 2: Implement (webhook):** in `handle_subscription_state`, after the provider persist try/except (:260):

```python
    if old_status == "trial_local" and new_status in ("trialing", "active"):
        # Trial→paid conversion: the running container is still the meta
        # trial box (meta provider + 200k/32k clamps + MODEL_API_KEY task
        # def). A hot swap can't lift task-def secrets or agents.defaults
        # clamps — only a cold reprovision regenerates all of it. Fire and
        # forget, same posture as billing_service's other dispatches;
        # background_provision is idempotent on the already-running case.
        asyncio.create_task(background_provision(account["owner_id"]))
```

(Import `background_provision` the way billing_service.py:773 does; confirm `background_provision`'s signature and its behavior on a RUNNING service — the ACTIVE-service branch at ecs_manager.py:1646-1794 rewrites configs + re-registers the task def + bounces desiredCount, which is exactly the cold roll we need. If it early-outs on desired==1 instead, call its restart path — resolve at implementation and note which branch runs in the test.)
- [ ] **Step 3: Implement (change_provider):** at the top of `change_provider` after the account load (:540):

```python
    if account.get("subscription_status") == "trial_local":
        raise HTTPException(status_code=409, detail={
            "code": "use_trial_checkout",
            "message": "Pick a plan first — provider selection happens at checkout for free-tier accounts.",
        })
```

- [ ] **Step 4: Run** → PASS. Ruff clean.
- [ ] **Step 5: Commit** — `fix(billing): cold-reprovision on trial→paid conversion; change_provider refuses trial rows`

### Task 7: Steer cap gate + timeout-refund narrowing (fixes #2 and #5)

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (steer :2249; error tail :2017-2069)
- Test: the same websocket wiring test module (steer + release tests exist from cap work — extend)

- [ ] **Step 1: Failing tests:** (a) over-cap trial owner's `agent_steer` → no chat.send dispatched, error frame `code:"meta_daily_cap"`; under-cap steer dispatches (read-only check — assert `reserve_or_block` NOT called: a second reserve on the parent's session_key would collide with the single-entry `_reserves` model); (b) dispatch failure with `ConnectionError` → `meta_cap.release` called (unchanged); dispatch failure with `asyncio.TimeoutError` → `release` NOT called (the frame was already transmitted — connection_pool.py:2057 sends before :2059 waits — so the run may exist and cost; keep the worst-case reserve, fail-closed; a later lifecycle/end reconciles it, else it expires with the stored date at midnight).
- [ ] **Step 2: Implement (steer):** before the send_rpc at :2249: `if await meta_cap.is_over_daily_cap(owner_id): send error frame + return` (copy the error-frame shape from the steer failure path at :2267-2270).
- [ ] **Step 3: Implement (timeout):** in the error tail, track which except branch fired (e.g. `definitely_not_dispatched = True` set in the ConnectionError/RuntimeError/pre-send branches, `False` in the TimeoutError branch) and gate the `meta_cap.release` call at :2064-2069 on it. Rewrite the justifying comment (:2046-2054) — its current claim is false for timeouts.
- [ ] **Step 4: Run** → PASS. Ruff clean.
- [ ] **Step 5: Commit** — `fix(billing): cap-gate the steer path; keep reserve on ambiguous chat.send timeout`

### Task 8: runId dedup on the usage settle trigger (fix #1)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_record_usage_from_session` :910)
- Test: `tests/unit/gateway/` usage-recording module (find with `grep -rl _record_usage_from_session tests/`)

- [ ] **Step 1: Failing test:** deliver the same lifecycle/end payload (same top-level `runId`) twice → `_fetch_and_record_usage` scheduled once; a different runId schedules again; a payload with NO runId always schedules (fail-open for recording — never drop usage on a missing id, the settle side is fail-closed anyway for webchat and the double-ADD risk only exists WITH duplicate delivery, which implies a runId-bearing event).
- [ ] **Step 2: Implement:** copy the `_side_run_ids` bounded-dict pattern (:186-189, :332-354) as `_settled_run_ids` (cap ~256); in `_record_usage_from_session` read `payload.get("runId")`, skip scheduling if already present, insert before scheduling. Comment: in-process scope, same single-instance posture as `meta_cap._reserves`; protects `meta_cap.settle`'s non-webchat ADD branch (no idempotency of its own) from re-delivered lifecycle/end events.
- [ ] **Step 3: Run** → PASS. Commit — `fix(billing): dedup usage settle on runId to prevent double-count on event redelivery`

### Task 9: Global free-tier kill-switch + runaway tripwire

**Files:**
- Modify: `apps/backend/core/services/meta_cap.py` (`settle` mirrors to a global row; new `is_global_cap_exceeded`), `apps/backend/core/billing/meta_pricing.py` (constant), `apps/backend/core/gateway/connection_pool.py` (gate_chat Layer 2 pre-check)
- Test: `tests/unit/services/test_meta_cap.py` (extend)

**Interfaces:** produces `meta_cap.is_global_cap_exceeded() -> bool` (read-only; DDB read of sentinel owner row `__meta_global__` via existing `get_meta_daily_spend`); `META_GLOBAL_DAILY_CAP_MICROCENTS = 500_000_000` ($5,000/day — generous; a blast-radius bound, not a budget) in meta_pricing.py.

- [ ] **Step 1: Failing tests:** (a) `settle` for a meta run ADDs the same actual to owner row AND `__meta_global__` row (both branches: webchat-reconcile mirrors the ACTUAL amount — mirror in settle, not reconcile, so reserve churn never touches the global row; non-webchat ADD mirrors too); (b) `is_global_cap_exceeded` True at/over the constant, False under, False on missing row; (c) gate_chat blocks with code `meta_daily_cap` when the global cap is exceeded (before placing any reserve).
- [ ] **Step 2: Implement.** In `settle`, after the per-owner branch, one best-effort mirror: `if is_meta and actual_microcents and actual_microcents > 0: await billing_repo.add_meta_daily_spend("__meta_global__", utc_today(), actual_microcents)` — for the webchat branch this needs the actual passed down regardless of reconcile's internal reserve math (settle already has it). Wrap in try/except-log (the mirror must never break usage recording). `is_global_cap_exceeded` reads `get_meta_daily_spend("__meta_global__", utc_today()) >= META_GLOBAL_DAILY_CAP_MICROCENTS`. In gate_chat Layer 2, before `reserve_or_block`: `if await meta_cap.is_global_cap_exceeded(): return blocked meta_daily_cap` (approximate/read-only by design — bounds a runaway fleet-wide, doesn't need reserve precision; note the one-turn-per-concurrent-user overshoot in the comment). Emit `put_metric("meta_cap.global_spend", value=...)` on each mirror ADD, and `put_metric("meta_cap.over_cap_spend")` in settle when an owner's recorded spend lands while the owner is already ≥ 2× CAP_MICROCENTS — the tamper tripwire (a config-tampered container burning past the cap becomes a visible metric to alarm on, instead of a silent runaway).
- [ ] **Step 3: Run** → PASS. Ruff clean. Commit — `feat(billing): global free-tier daily spend bound + over-cap runaway metric`

### Task 10: 3-day trial expiry enforcement (DECISION POINT 1 — strike if dropped)

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py` (trial_local branch :313-324 + a `trial_expired_gate` helper next to `meta_daily_cap_gate`), `apps/backend/core/gateway/connection_pool.py` (gate_chat Layer 2), `apps/frontend/src/components/chat/ChatErrorCard.tsx` (new case)
- Test: `tests/unit/services/test_provision_gate.py`, gate_chat tests, `ChatErrorCard` vitest file

- [ ] **Step 1: Failing tests (backend):** trial_local row with `trial_end` in the past → provision gate returns `trial_expired` gate (402 payload, title "Your free trial has ended", action → `/settings?panel=billing`); `trial_end` in the future → cap logic unchanged; missing `trial_end` on a trial_local row → treated as NOT expired (legacy dev rows; the cap still bounds them — comment this). gate_chat: expired trial → blocked `{"code": "trial_expired"}` before any reserve.
- [ ] **Step 2: Implement** — read-time comparison (`int(time.time()) > int(trial_end)`), no persisted status flip, no worker (trial_local's clock is ours by construction — the Stripe-owns-trial-clock rule applies to Stripe trials, not local ones; note this in the docstring). Add the check at the top of the provision gate's trial_local branch and gate_chat's Layer 2 (before the global/reserve checks).
- [ ] **Step 3: Frontend:** `case "trial_expired"` in `ChatErrorCard.describeError` (copy the `meta_daily_cap` case: title "Your free trial has ended", message from backend, Upgrade → `/settings?panel=billing`). Run that vitest file + `tsc --noEmit`.
- [ ] **Step 4: Run** → PASS. Commit — `feat(billing): enforce the 3-day free-trial window at gate and provision`

# PART 3 — Sub-spec 3: event-driven scale-to-zero

### Task 11: Liveness plumbing — ping stamps + chat-typed live count (fixes the dead-TTL bug)

**Files:**
- Modify: `apps/backend/core/services/connection_service.py` (`store_connection` :84-91 writes `ttl`; new `touch_connection`; new `count_live_chat_connections`)
- Modify: `apps/backend/routers/websocket_chat.py` ping handler (:632-635)
- Test: `tests/unit/services/test_connection_service*.py` (moto-backed, extend)

**Interfaces:**
- Produces: `connection_service.touch_connection(connection_id) -> None` (update_item setting `last_ping_at=now_epoch`, `ttl=now_epoch + 7200`; throttle: skip if called within 60s for the same id via a small in-process dict); `connection_service.count_live_chat_connections(owner_id) -> int` (query_by_owner, filter `connectionType == "chat"` AND `last_ping_at` fresher than 120s where present — rows without `last_ping_at` (pre-deploy or just-connected) count as live, comment why: fail-safe toward "someone is here", the stop side must over-preserve, never over-kill); `store_connection` stamps `ttl` and `last_ping_at` at connect.
- Consumes: existing `query_by_owner` (:232).

- [ ] **Step 1: Failing tests:** store_connection writes ttl + last_ping_at; touch_connection refreshes both; count_live_chat_connections counts a fresh chat row, excludes `node`/`operator` rows, excludes a chat row with last_ping_at older than 120s, includes a chat row missing last_ping_at.
- [ ] **Step 2: Implement** (plain update_item / query code in the module's existing style).
- [ ] **Step 3: Wire the ping handler** (:632-635): after sending pong, `asyncio.create_task(connection_service.touch_connection(x_connection_id))` — fire-and-forget, never blocks the pong. (This also finally gives the ws-connections table the TTL hygiene its schema declares — api-stack.ts:277.)
- [ ] **Step 4: Run** → PASS. Ruff clean. Commit — `feat(gateway): stamp ping liveness + ttl on ws-connections; chat-typed live count`

### Task 12: Layer 1 — trial containers stop after an extended idle grace on last disconnect

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_grace_close` :2172; new `_maybe_stop_trial_container`; constant `_TRIAL_STOP_GRACE_S = 150`)
- Test: `tests/unit/gateway/test_connection_pool_scale_to_zero.py` (new)

**Interfaces:**
- Consumes: `billing_repo.get_by_owner_id` (trial_local check — the STOP decision keys on subscription_status deliberately: a mid-upgrade owner (status active, container still meta) must NOT be stopped out from under their first paid session; the cap side (Task 4) keys on the container row for the opposite reason), `connection_service.count_live_chat_connections` (Task 11), `get_ecs_manager().stop_user_service` (existing), `self.has_active_runs(owner_id)` / parked state (existing, veto).
- Produces: `_maybe_stop_trial_container(owner_id)` — also called by Tasks 13/14.

Behavior (extends `_grace_close`, which today sleeps 30s → closes the gateway WS):

```python
    async def _grace_close(self, user_id: str) -> None:
        """Wait grace period, then close gateway connection if still idle.

        For free-tier (trial_local) owners, a second, longer grace follows:
        if the owner still has no live chat clients, no in-flight webchat
        run, and no parked connection, the container itself is stopped
        (desiredCount=0). Event-driven only — this task exists solely
        because a disconnect event armed it. The wake path is the existing
        GET /container/status auto-retry (cap-gated).
        """
        try:
            await asyncio.sleep(_GRACE_PERIOD)
            fcs = self._frontend_connections.get(user_id, set())
            if fcs:
                return
            # Decide trial-ness and run-state BEFORE closing the WS —
            # after close_user the pool has no visibility into the container.
            account = await billing_repo.get_by_owner_id(user_id)
            is_trial = bool(account) and account.get("subscription_status") == "trial_local"
            await self.close_user(user_id)
            if is_trial:
                await asyncio.sleep(_TRIAL_STOP_GRACE_S)
                await self._maybe_stop_trial_container(user_id)
        except asyncio.CancelledError:
            pass
```

with:

```python
    async def _maybe_stop_trial_container(self, owner_id: str) -> None:
        """Stop a trial container that provably has nobody using it.

        Every check must pass; any doubt = don't stop (over-preserve,
        never over-kill — the container costs ~$0.02/hr, a killed run
        costs trust). Re-checks trial_local at fire time (an owner who
        converted during the grace must not be stopped).
        """
        try:
            if self._frontend_connections.get(owner_id):
                return  # a client reconnected on this task
            account = await billing_repo.get_by_owner_id(owner_id)
            if not account or account.get("subscription_status") != "trial_local":
                return
            if self.has_active_runs(owner_id) or self.is_parked(owner_id):
                return  # in-flight webchat run — never kill mid-turn
            svc = await get_connection_service()
            if await svc.count_live_chat_connections(owner_id) > 0:
                return  # cross-task truth: another backend task holds a live tab
            await get_ecs_manager().stop_user_service(owner_id)
            put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "grace"})
        except Exception:
            logger.exception("trial scale-to-zero check failed owner=%s", owner_id)
```

(`is_parked`/`has_active_runs` — use the actual existing accessors; check names at :268-270 and the `_parked` handling around :1608-1661; add a small `is_parked` helper if none exists. `get_ecs_manager` import must be lazy/inside the method if `core.containers` imports the pool — check for circularity the way `gate_chat` imports `billing_repo`.)

Note: `has_active_runs` is webchat-only — a Paperclip/interactive-Teams run in flight is NOT vetoed by it. That's accepted: sub-spec 2 means trial Paperclip runs only happen while the user is actively driving Teams, and the Teams UI holds a chat-typed WS (the /teams surface connects through the same gateway socket — verify at implementation; if Teams uses a separate connection type, count it in `count_live_chat_connections`'s filter).

- [ ] **Step 1: Write failing tests:** stops only when ALL of (trial_local, no in-process frontends, no active/parked runs, zero cross-task chat conns); each veto individually prevents the stop (5 cases); paid owner never stopped; conversion-during-grace (account flips to active between arm and fire) not stopped; `stop_user_service` exception swallowed + logged.
- [ ] **Step 2: Implement** as above. **Step 3: Run** → PASS. Ruff clean.
- [ ] **Step 4: Commit** — `feat(billing): trial containers scale to zero on extended idle grace after last disconnect`

### Task 13: Layer 3 — per-owner ping deadline catches lost $disconnects

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (deadline timers, reset hook), `apps/backend/routers/websocket_chat.py` (ping handler calls the reset)
- Test: extend `test_connection_pool_scale_to_zero.py`

**Interfaces:** produces `pool.note_client_activity(owner_id)` — resets a per-owner deadline (`asyncio` timer, `_CLIENT_DEADLINE_S = 180` = 6 missed pings); on expiry, force-clears the owner's `_frontend_connections` set and arms the standard `_grace_close` (flowing into Task 12's stop). Timer exists only while the owner has ≥1 registered connection (armed in `add_frontend_connection`, cancelled when `remove_frontend_connection` empties the set — the normal disconnect path supersedes it). Called from the ping handler (same place as Task 11's touch) and from `add_frontend_connection`.

- [ ] **Step 1: Failing tests:** deadline expiry with no pings → frontends cleared + grace armed (stop flows via Task 12's checks); a ping within the window resets it (no clear); normal disconnect cancels the timer; timer never outlives the connection set.
- [ ] **Step 2: Implement** (single `asyncio.get_event_loop().call_later`-style handle per owner, stored in a dict, `.cancel()` + re-arm on reset — cheap; in-process scope documented).
- [ ] **Step 3: Run** → PASS. Commit — `feat(gateway): ping-deadline recovers lost disconnects into the idle-grace path`

### Task 14: Layer 2 — one-time boot reconcile sleeps leaked idle trial containers

**Files:**
- Modify: `apps/backend/main.py` (new `_reconcile_trial_idle_containers` awaited in lifespan next to `_resume_provisioning_transitions` :366)
- Test: `tests/unit/test_main_boot_reconcile.py` (new; or extend the module that tests `_resume_provisioning_transitions` — find it)

**Interfaces:** consumes `container_repo.get_by_status("running")`, `billing_repo.get_by_owner_id`, `connection_service.count_live_chat_connections`, `pool._maybe_stop_trial_container` (via `get_gateway_pool()`).

- [ ] **Step 1: Failing tests:** a running container whose owner is trial_local with zero live chat conns → `_maybe_stop_trial_container` invoked (which re-runs all its own vetoes); paid owner skipped; trial owner with a live chat conn skipped; repo read failure → logged, startup continues (copy `_resume_provisioning_transitions`'s try/except posture exactly).
- [ ] **Step 2: Implement** (same shape as :255-276: one paginated GSI read, per-row fire-and-forget `asyncio.create_task`; ONE-TIME — no loop, no interval; docstring says why: backend restarts drop the in-process grace timers, this is the only recovery for containers that were mid-grace).
- [ ] **Step 3: Run** → PASS. Commit — `feat(billing): boot reconcile sleeps trial containers leaked across backend restarts`

### Task 15: Stopped-container chat UX — honest error + self-healing wake

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (:1793-1806 no-container branch)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (handle the new code), `apps/frontend/src/hooks/useProvisioningState.ts` or `ProvisioningStepper.tsx` (refresh hook — smallest change wins)
- Test: backend wiring test + one vitest file for the frontend handler

- [ ] **Step 1: Failing test (backend):** dispatcher with a container row whose `status == "stopped"` → error frame `{"type":"error","code":"container_waking","message":"Waking your agent — this takes about a minute."}` (today: misleading "No container provisioned. Please subscribe."). `resolve_running_container` returns `(None, None)` for stopped rows, so the dispatcher must fetch the row itself on the None branch (`container_repo.get_by_owner_id`) to distinguish stopped from truly-absent; truly-absent keeps the existing copy.
- [ ] **Step 2: Implement backend** (in the :1793-1806 branch).
- [ ] **Step 3: Frontend:** on `code === "container_waking"`, trigger one `useProvisioningState` refresh (`refresh()` is already exposed — the status poll's auto-retry then wakes the container and the existing `isRegressingFromReady` machinery (ProvisioningStepper.tsx:351-378) shows the cold-start UI because fresh data now arrives). Show the message as a normal chat error meanwhile. Run the touched vitest file + `tsc --noEmit`.
- [ ] **Step 4: Run** → PASS. Commit — `feat(frontend): honest waking state when chatting into a scaled-to-zero container`

# PART 4 — Close-out

### Task 16: Trial telemetry events

**Files:**
- Modify: `apps/backend/core/observability/product_analytics.py` (+ call sites: meta_cap.settle / gate_chat block path / stripe webhook conversion / provision gate expiry)
- Test: extend the product_analytics test module (find it; match its capture pattern)

- [ ] Server-side PostHog events, each ONE line at an existing decision point (no new state): `trial_cap_reached` (first block of the day per owner — emit on gate_chat's blocked meta_daily_cap return; per-owner-per-day dedup via a small in-process set keyed (owner, date)), `trial_converted` (the Task 6 webhook branch), `trial_expired_block` (Task 10 gate), `trial_scale_to_zero` (already a CW metric in Task 12 — add the PostHog twin if product wants funnel visibility; else skip, CW suffices — decide with the reviewer). `trial_started` already exists from Phase A (verify; if absent, emit in `ensure_personal_trial_default`'s create branch).
- [ ] Tests: each event fires exactly once at its trigger; failures never propagate (best-effort posture of the module).
- [ ] Run → PASS. Commit — `feat(observability): trial funnel telemetry`

### Task 17: Full-suite gate + ledger

- [ ] `cd apps/backend && uv run pytest tests/unit -q` (full unit suite — the cap/gateway/teams/billing modules interact; memory: tests-full-at-end)
- [ ] `uv run pytest tests/chaos/invariants/test_config_schema.py -v` (schema gate, final)
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] Frontend: `node_modules/.bin/tsc --noEmit` + the individually-touched vitest files (NEVER the full frontend suite)
- [ ] Update `.superpowers/sdd/progress.md` per task as you go (this is the recovery map)
- [ ] Commit any stragglers. **Do not push** — final whole-branch review + user-mandated end-of-Phase-B `/code-review` come first, then PR #1219 update with user approval.

---

## Self-review notes (spec coverage)

- WS3.5 entitlement → REVISED per user (2026-07-11): Paperclip autonomy is creation-free / execution-capped, enforced by Task 2b's adapter allowance gate (the funnel every Paperclip run passes through — covers routines, monitors, heartbeats, public-fire, agent-created triggers uniformly) + Task 2's cap-402 UX guards on click-to-run endpoints. `has_always_on_entitlement` survives only for webhooks (hooks_proxy 402) + channels (config-write gate) — Task 1. Config lockdown (cron/heartbeat off) — Task 3. Fresh-company day-one autonomy verified ZERO (heartbeats default off, nothing seeded). gate_chat session-key refusal deliberately dropped — user decision. WS3.6 guards → Task 6 (`change_provider` trial refusal; target-meta impossible by type). WS3.7 kill-switch → Task 9. WS3 expiry → Task 10. WS6 layers 1/2/3 + wake → Tasks 11-14 (wake pre-exists). WS7-web → Task 15 (stepper re-arm satisfied by refresh-on-error + existing isRegressingFromReady; reconnect budget unchanged — a stopped container can only exist with zero live tabs, so no live session ever needs to outlast a cold start). WS7-desktop → deferred (chat-typed liveness filter in Task 11 removes the node-pins-presence bug for the STOP decision; tray-resident webview residual accepted). Cross-cutting telemetry → Task 16. Cap review tail #1/#2/#4/#5 → Tasks 8/7/4/7; #3/#7 → Task 5 (side refusal + suppression); #6 tamper → config lockdown (Task 3) + runaway tripwire (Task 9) + spec-deferred reconciler; #8/#9 → deferred (fail-closed).
- Consistency: `has_always_on_entitlement` / `always_on_requires_subscription` / `trial_expired` / `container_waking` / `suppress_commands` / `_TRIAL_STOP_GRACE_S=150` / `_CLIENT_DEADLINE_S=180` / `META_GLOBAL_DAILY_CAP_MICROCENTS=500_000_000` used identically across tasks.
- Go-live gates (unchanged, from master spec): Meta ToS review, Meta billing card, prod secret `isol8/prod/meta-model-api-key` (create ONLY after this plan merges — creating it before ships an uncapped prod free tier), 7.1 base bump landed (done).
