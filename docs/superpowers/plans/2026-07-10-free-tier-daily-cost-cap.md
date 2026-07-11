# Free-Tier $3/Day Cost Cap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. Spec:
> `docs/superpowers/specs/2026-07-10-free-tier-daily-cost-cap-design.md`.

**Goal:** Cap a `trial_local` owner's Meta inference spend at **$3 per UTC day**,
enforced fail-closed at the chat chokepoint, cost taken from OpenClaw's
cache-inclusive `estimatedCostUsd` (made correct by setting the meta rate
in-container), stored on an isolated date-keyed counter on `billing_accounts`.

**Architecture:** A new `core/services/meta_cap.py` owns the cap logic
(constants, atomic reserve-as-gate, reconcile, an in-process reserve map keyed
by `session_key`). `billing_repo` gains atomic nested-map counter helpers.
`gate_chat` gains the pre-send block+reserve (trial_local only);
`_fetch_and_record_usage` settles the reserve post-turn. `config.py` writes the
real meta cost rate + clamps so `estimatedCostUsd` is accurate; `record_usage`
is extended to honor `raw_cost_microcents` for meta.

**Tech stack:** Python 3.12, FastAPI, boto3 **resource**-level DynamoDB (Decimal
numbers, `run_in_thread`), pytest.

## Global Constraints

- **Unit = microcents.** 1 USD = 1_000_000 microcents. `CAP_MICROCENTS =
  3_000_000`. Never store floats in DynamoDB — `Decimal(str(int))`.
- **Only `trial_local` owners** are metered/capped. Paid owners: no reserve, no
  counter write, no gate — untouched.
- **UTC date** key (`YYYY-MM-DD`). New UTC day → fresh $3, no scheduler.
- **Fail-closed:** a turn whose reconciled cost is 0/missing keeps its reserve.
- **Meta rate:** `$1.25`/1M input, `$4.25`/1M output; `cacheRead`/`cacheWrite`
  TBC (default `cacheRead = input rate`, `cacheWrite = input rate` as
  conservative upper bounds until confirmed).
- **Single backend instance** (desiredCount=1) — an in-process reserve map is
  acceptable (same posture as `_org_provision_locks`); a reserve lost to a
  backend restart just isn't refunded (fail-closed).
- **Reserve↔reconcile key = `session_key`** (`run_id_for_send` is computed
  after the gate, so it cannot key the reserve).
- Branch `feat/free-tier-daily-cap`; PR; no push without approval.

---

## File Structure

- **Create** `apps/backend/core/billing/meta_pricing.py` — rate constants, cost
  helper, worst-case reserve, `utc_today()`.
- **Create** `apps/backend/core/services/meta_cap.py` — reserve-as-gate +
  reconcile + in-process reserve map.
- **Modify** `apps/backend/core/repositories/billing_repo.py` — atomic
  nested-map counter helpers.
- **Modify** `apps/backend/core/services/usage_service.py` — honor
  `raw_cost_microcents` for meta.
- **Modify** `apps/backend/core/containers/config.py` — meta in-container cost
  rate + clamps.
- **Modify** `apps/backend/core/gateway/connection_pool.py` — `gate_chat`
  block+reserve; `_fetch_and_record_usage` reconcile.
- **Modify** `apps/backend/routers/websocket_chat.py` — pass `session_key` into
  `gate_chat`.
- **Modify** `apps/frontend/src/components/chat/...` — render the
  `meta_daily_cap` error (message + placeholder upgrade CTA).
- Tests alongside each.

---

## Task 1 — Meta pricing + cap constants

**Files:** Create `core/billing/meta_pricing.py`; Test
`tests/unit/billing/test_meta_pricing.py`

**Produces:** `META_MODEL_REF = "meta/muse-spark-1.1"`,
`CAP_MICROCENTS = 3_000_000`, `RESERVE_MICROCENTS: int`,
`is_meta_model(model: str) -> bool`, `meta_cost_microcents(input_tokens,
output_tokens, cache_read=0, cache_write=0) -> int`, `utc_today() -> str`,
plus the in-container cost dict `META_COST_USD_PER_MILLION`.

- [ ] **Step 1: failing test**

```python
from core.billing import meta_pricing as mp

def test_cost_input_output_microcents():
    # USD-per-million == microcents-per-token numerically.
    # 1_000_000 input * 1.25/1e6 * 1e6 microcents = 1_250_000 microcents
    assert mp.meta_cost_microcents(1_000_000, 0) == 1_250_000
    assert mp.meta_cost_microcents(0, 1_000_000) == 4_250_000

def test_is_meta_model():
    assert mp.is_meta_model("meta/muse-spark-1.1")
    assert not mp.is_meta_model("amazon-bedrock/anthropic.claude")

def test_reserve_is_positive_and_bounded():
    # worst case one turn: full context as cache-read + max output
    assert 0 < mp.RESERVE_MICROCENTS <= 2_000_000  # < $2

def test_utc_today_is_iso_date():
    d = mp.utc_today()
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"
```

- [ ] **Step 2: run → fail** (`module not found`).
- [ ] **Step 3: implement**

```python
"""Meta Muse Spark pricing + free-tier cap constants (microcents).

1 USD = 1_000_000 microcents. A rate quoted as USD-per-million-tokens is
numerically identical to microcents-per-token, so cost = tokens * rate.
"""
from datetime import datetime, timezone

META_MODEL_REF = "meta/muse-spark-1.1"

# USD per 1M tokens. cacheRead/cacheWrite default to the input rate as a
# conservative upper bound until Meta's real cache rates are confirmed.
_INPUT = 1.25
_OUTPUT = 4.25
_CACHE_READ = 1.25
_CACHE_WRITE = 1.25

# For the in-container config (cosmetic + makes estimatedCostUsd correct).
META_COST_USD_PER_MILLION = {
    "input": _INPUT,
    "output": _OUTPUT,
    "cacheRead": _CACHE_READ,
    "cacheWrite": _CACHE_WRITE,
}

CAP_MICROCENTS = 3_000_000  # $3.00

# Phase-A-deferred clamps the cap relies on.
CONTEXT_TOKENS = 200_000
MAX_TOKENS = 32_000
EFFORT = "medium"


def is_meta_model(model: str) -> bool:
    return (model or "").startswith("meta/")


def meta_cost_microcents(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> int:
    return int(
        input_tokens * _INPUT
        + output_tokens * _OUTPUT
        + cache_read * _CACHE_READ
        + cache_write * _CACHE_WRITE
    )


# Worst-case one-turn cost for the pre-send reserve: the full context window
# re-read as cache plus a max-length output. Bounds parallel-burst overshoot.
RESERVE_MICROCENTS = meta_cost_microcents(
    input_tokens=0,
    output_tokens=MAX_TOKENS,
    cache_read=CONTEXT_TOKENS,
)


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
```

- [ ] **Step 4: run → pass. Commit.**

---

## Task 2 — `record_usage` honors `raw_cost_microcents` for meta

**Files:** Modify `core/services/usage_service.py`; Test
`tests/unit/services/test_usage_service.py`

**Context:** `record_usage` (`usage_service.py:45`) currently uses
`raw_cost_microcents` verbatim only when `_is_bedrock_model(model)`; meta falls
to the token path → `UnknownModelError` → spend 0 (`:118`). Extend the
raw-cost gate to also accept meta refs (their `estimatedCostUsd`, made correct
by Task 3, is the authoritative cache-inclusive cost).

- [ ] **Step 1: failing test**

```python
@pytest.mark.asyncio
async def test_meta_uses_raw_cost_microcents():
    with patch("core.services.usage_service.usage_repo") as repo:
        repo.record_usage = AsyncMock()
        await record_usage(
            owner_id="user_x", user_id="user_x",
            model="meta/muse-spark-1.1",
            input_tokens=10, output_tokens=5, cache_read=0, cache_write=0,
            raw_cost_microcents=4242,
        )
        # spend recorded == the raw cost, NOT 0 and NOT recomputed from tokens
        _, kwargs = repo.record_usage.await_args
        assert kwargs["spend_microcents"] == 4242
```

- [ ] **Step 2: run → fail** (meta records 0).
- [ ] **Step 3: implement** — at the raw-cost gate (`usage_service.py:87`),
  broaden the condition from bedrock-only to "bedrock **or** meta":

```python
from core.billing.meta_pricing import is_meta_model  # top of file
...
# raw cost override is authoritative for the Isol8-billed paths: Bedrock
# (metered) and meta (free-tier, estimatedCostUsd made correct in-container).
use_raw = (
    raw_cost_microcents is not None
    and raw_cost_microcents > 0
    and (_is_bedrock_model(model) or is_meta_model(model))
)
if use_raw:
    spend_microcents = raw_cost_microcents
else:
    ... existing token path (bedrock cost_microcents; else 0) ...
```

- [ ] **Step 4: run → pass. Commit.** (Verify the existing bedrock + non-meta
  spend-0 tests still pass.)

---

## Task 3 — `config.py`: meta in-container cost rate + clamps

**Files:** Modify `core/containers/config.py`; Test
`tests/unit/containers/test_config_provider_routing.py`

**Context:** The meta branch (`config.py:482-487`) returns
`({}, {"primary": META, "fallbacks":[META]}, {})` — empty providers_config, so
no cost/clamps are emitted and `estimatedCostUsd` is $0. Emit a
`models.providers.meta.models[0]` entry carrying `cost` + `maxTokens`, and set
`agents.defaults.contextTokens` + the `params` (`effort`, `maxTokens`).

- [ ] **Step 1: failing test**

```python
def test_meta_provider_block_sets_cost_and_clamps():
    providers, models, _ = build_provider_block(provider_choice="meta", ...)
    entry = providers["meta"]["models"][0]
    assert entry["id"] == "meta/muse-spark-1.1"
    assert entry["cost"]["input"] == 1.25 and entry["cost"]["output"] == 4.25
    assert entry["cost"]["cacheRead"] == 1.25  # conservative default
    assert entry["maxTokens"] == 32000

def test_meta_config_sets_agent_defaults_clamps():
    cfg = build_openclaw_config_dict(provider_choice="meta", ...)  # trial owner
    d = cfg["agents"]["defaults"]
    assert d["contextTokens"] == 200000
    assert d["params"]["effort"] == "medium"
    assert d["params"]["maxTokens"] == 32000
```

- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement**
  - In `build_provider_block`'s meta branch, return a non-empty providers_config:

```python
from core.billing.meta_pricing import (
    META_MODEL_REF, META_COST_USD_PER_MILLION, MAX_TOKENS, CONTEXT_TOKENS,
)
if provider_choice == "meta":
    providers_config = {
        "meta": {
            "baseUrl": "https://api.ai.meta.com/v1",
            "api": "openai-responses",
            "models": [{
                "id": META_MODEL_REF,
                "cost": META_COST_USD_PER_MILLION,
                "maxTokens": MAX_TOKENS,
            }],
        }
    }
    return (providers_config, {"primary": META_MODEL_REF, "fallbacks": [META_MODEL_REF]}, {})
```

  - Where `agents.defaults` is assembled (near `config.py:997/1008/1011`), for
    the meta/trial path set `contextTokens` (first-class key) and thread
    `effort` + `maxTokens` into the `params` dict:

```python
if provider_choice == "meta":
    agent_defaults["contextTokens"] = CONTEXT_TOKENS
    agent_defaults.setdefault("params", {}).update(
        {"effort": EFFORT, "maxTokens": MAX_TOKENS}
    )
```

- [ ] **Step 4: run → pass. Commit.**

> Confirm exact key nesting against `AgentDefaultsSchema` (contextTokens is
> first-class; effort/maxTokens live in the free-form `params`) — per grounding
> (`config-clamps` finding). If the meta provider `api`/`baseUrl` differs from
> the extension manifest, mirror the manifest (`api.ai.meta.com/v1`,
> `openai-responses`).

---

## Task 4 — `billing_repo`: atomic daily-counter helpers

**Files:** Modify `core/repositories/billing_repo.py`; Test
`tests/unit/repositories/test_billing_repo.py`

**Produces:** `async def add_meta_daily_spend(owner_id, date, delta_microcents)
-> int` (returns the new day total) and `async def get_meta_daily_spend(owner_id,
date) -> int`.

**Gotchas (from grounding):** ADD on a nested path requires the parent map to
exist. Initialize the map with `SET meta_daily_spend_microcents =
if_not_exists(meta_daily_spend_microcents, :empty)` in the same UpdateExpression
as the `ADD ...#d :delta`. Route the date through `ExpressionAttributeNames`
(`#d`). Guard with `ConditionExpression="attribute_exists(owner_id)"` (no
phantom rows). `Decimal(str(int))` in, `int(...)` out, via `run_in_thread`.

- [ ] **Step 1: failing test** (moto or mocked table)

```python
@pytest.mark.asyncio
async def test_add_meta_daily_spend_accumulates_per_date():
    # first add creates the map + leaf; second add accumulates same day
    t1 = await billing_repo.add_meta_daily_spend("user_x", "2026-07-10", 1000)
    t2 = await billing_repo.add_meta_daily_spend("user_x", "2026-07-10", 500)
    assert t1 == 1000 and t2 == 1500
    # a different date is independent
    t3 = await billing_repo.add_meta_daily_spend("user_x", "2026-07-11", 200)
    assert t3 == 200
    assert await billing_repo.get_meta_daily_spend("user_x", "2026-07-10") == 1500
```

- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** (mirror `usage_repo.py:29-63` atomic idiom):

```python
async def add_meta_daily_spend(owner_id: str, date: str, delta_microcents: int) -> int:
    table = get_table(TABLE_NAME)
    resp = await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=(
            "SET meta_daily_spend_microcents = "
            "if_not_exists(meta_daily_spend_microcents, :empty) "
            "ADD meta_daily_spend_microcents.#d :delta"
        ),
        ExpressionAttributeNames={"#d": date},
        ExpressionAttributeValues={":delta": Decimal(str(int(delta_microcents))), ":empty": {}},
        ConditionExpression="attribute_exists(owner_id)",
        ReturnValues="ALL_NEW",
    )
    attrs = resp.get("Attributes", {})
    return int(attrs.get("meta_daily_spend_microcents", {}).get(date, 0))


async def get_meta_daily_spend(owner_id: str, date: str) -> int:
    item = await get_by_owner_id(owner_id)
    if not item:
        return 0
    return int(item.get("meta_daily_spend_microcents", {}).get(date, 0))
```

> Verify at implementation: whether DynamoDB allows `SET if_not_exists(map)` and
> `ADD map.#d` in one expression (they touch overlapping paths). If the engine
> rejects the combined expression, split into a guarded two-step (init map if
> absent, then ADD) — the test above still governs the observable behavior.

- [ ] **Step 4: run → pass. Commit.**

---

## Task 5 — `meta_cap` service (reserve-as-gate + reconcile)

**Files:** Create `core/services/meta_cap.py`; Test
`tests/unit/services/test_meta_cap.py`

**Produces:**
- `async def reserve_or_block(owner_id, session_key) -> bool` (True = blocked).
- `async def reconcile(owner_id, session_key, actual_microcents) -> None`.

**Design:** in-process `_reserves: dict[str, tuple[str, int]]` keyed by
`session_key` → `(date, reserve)`. Reserve-as-gate is atomic via
`add_meta_daily_spend` returning the new total: `spent_before = new_total -
reserve`; block iff `spent_before >= CAP` (and roll the reserve back). This
serializes concurrent turns through the atomic counter with no TOCTOU.

- [ ] **Step 1: failing tests**

```python
@pytest.mark.asyncio
async def test_first_turn_reserves_and_allows(monkeypatch):
    totals = {}
    async def fake_add(o, d, delta): totals[d] = totals.get(d,0)+delta; return totals[d]
    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    assert await meta_cap.reserve_or_block("user_x", "sk1") is False  # allowed

@pytest.mark.asyncio
async def test_reconcile_trues_down_to_actual(monkeypatch):
    totals = {}
    async def fake_add(o, d, delta): totals[d]=totals.get(d,0)+delta; return totals[d]
    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    await meta_cap.reserve_or_block("user_x", "sk1")          # +RESERVE
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=1000)
    assert sum(totals.values()) == 1000                       # reserve replaced by actual

@pytest.mark.asyncio
async def test_block_when_already_at_cap(monkeypatch):
    async def fake_add(o, d, delta): return CAP_MICROCENTS + delta  # spent_before >= CAP
    rolled = []
    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend",
                        lambda o,d,delta: _rec(fake_add, rolled, o,d,delta))
    assert await meta_cap.reserve_or_block("user_x", "sk1") is True   # blocked
    assert rolled and rolled[-1] < 0                                  # reserve rolled back

@pytest.mark.asyncio
async def test_reconcile_missing_cost_is_failclosed(monkeypatch):
    totals = {}
    async def fake_add(o,d,delta): totals[d]=totals.get(d,0)+delta; return totals[d]
    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    await meta_cap.reserve_or_block("user_x", "sk1")
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=0)    # cost unknown
    assert sum(totals.values()) == RESERVE_MICROCENTS                # reserve kept
```

- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement**

```python
from core.billing.meta_pricing import CAP_MICROCENTS, RESERVE_MICROCENTS, utc_today
from core.repositories import billing_repo

# session_key -> (date, reserve_microcents). Single-backend-instance scope;
# a reserve lost to a restart is simply never refunded (fail-closed).
_reserves: dict[str, tuple[str, int]] = {}


async def reserve_or_block(owner_id: str, session_key: str) -> bool:
    date = utc_today()
    new_total = await billing_repo.add_meta_daily_spend(owner_id, date, RESERVE_MICROCENTS)
    spent_before = new_total - RESERVE_MICROCENTS
    if spent_before >= CAP_MICROCENTS:
        # already over budget — roll back this reserve and block
        await billing_repo.add_meta_daily_spend(owner_id, date, -RESERVE_MICROCENTS)
        return True
    _reserves[session_key] = (date, RESERVE_MICROCENTS)
    return False


async def reconcile(owner_id: str, session_key: str, actual_microcents: int) -> None:
    held = _reserves.pop(session_key, None)
    if held is None:
        return  # not a capped turn (or already settled)
    date, reserve = held
    if actual_microcents and actual_microcents > 0:
        await billing_repo.add_meta_daily_spend(owner_id, date, actual_microcents - reserve)
    # else: FAIL-CLOSED — keep the reserve (do nothing)
```

- [ ] **Step 4: run → pass. Commit.**

---

## Task 6 — Wire the cap into `gate_chat` + `_fetch_and_record_usage`

**Files:** Modify `core/gateway/connection_pool.py`,
`routers/websocket_chat.py`; Test
`tests/unit/gateway/test_gate_chat_meta_cap.py`

**Context:** `gate_chat(self, *, user_id, owner_id)` (`connection_pool.py:1859`)
loads `account` for the sub check; `trial_local` currently passes. Add: pass
`session_key`, and if `account.subscription_status == "trial_local"`, call
`reserve_or_block` → return the block payload. `_fetch_and_record_usage`
(`:978`, has `session_key` + `self.user_id`) already computes
`raw_cost_microcents`; after `record_usage`, call `reconcile`.

- [ ] **Step 1: failing tests**

```python
@pytest.mark.asyncio
async def test_gate_blocks_trial_local_at_cap(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(billing_repo, "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "trial_local"}))
    monkeypatch.setattr("core.gateway.connection_pool.meta_cap.reserve_or_block",
        AsyncMock(return_value=True))
    gate = await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")
    assert gate == {"blocked": True, "code": "meta_daily_cap", "message": ANY_STR}

@pytest.mark.asyncio
async def test_gate_allows_trial_local_under_cap(monkeypatch):
    ... reserve_or_block -> False ...
    gate = await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")
    assert gate == {"blocked": False}

@pytest.mark.asyncio
async def test_paid_owner_never_reserves(monkeypatch):
    ... subscription_status "active" ...
    spy = AsyncMock(return_value=False)
    monkeypatch.setattr("...meta_cap.reserve_or_block", spy)
    await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")
    spy.assert_not_awaited()
```

- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement**
  - `gate_chat` signature → add keyword `session_key: str`. After the existing
    subscription/credit checks pass (before the final `{"blocked": False}`):

```python
from core.services import meta_cap  # lazy import, matching the file's style
if account and account.get("subscription_status") == "trial_local":
    if await meta_cap.reserve_or_block(owner_id, session_key):
        return {
            "blocked": True,
            "code": "meta_daily_cap",
            "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
        }
```

  - `websocket_chat.py:1909` call site → pass `session_key=session_key` (already
    in scope at `:1828`).
  - `_fetch_and_record_usage` (after the `record_usage` call, `~:1081`):

```python
from core.services import meta_cap
await meta_cap.reconcile(self.user_id, session_key, raw_cost_microcents or 0)
```

- [ ] **Step 4: run → pass. Commit.**

> The block-return keeps the existing caller short-circuit
> (`websocket_chat.py:1910-1930`): metric + `management_api.send_message(...,
> {"type":"error","code":"meta_daily_cap","message":...})` + early `return`, so
> a capped turn never reaches OpenClaw. No caller change needed beyond passing
> `session_key`.

---

## Task 7 — Frontend: render the `meta_daily_cap` block

**Files:** Modify the chat error rendering
(`apps/frontend/src/hooks/useAgentChat.ts` / `MessageList.tsx` — confirm which
renders `{type:"error", code, message}`); Test the relevant single vitest +
`tsc --noEmit`

**Context:** `management_api.send_message` delivers `{type:"error", code,
message}`; the client already renders the `message`. For `meta_daily_cap`,
render it as a distinct "daily free limit" state (message + a placeholder
"Upgrade" button; the upgrade **action** is Phase C — the button can link to
`/settings/billing` or be disabled-with-tooltip for now).

- [ ] **Step 1: failing single-file vitest** — an error event with
  `code === "meta_daily_cap"` renders the message + an Upgrade affordance.
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** the minimal branch on `code === "meta_daily_cap"`.
- [ ] **Step 4:** vitest single-file + `tsc --noEmit` → pass. **Commit.**

---

## Task 8 — Config-schema validation (crash-loop guard)

**Files:** run `tests/chaos/invariants/test_config_schema.py`

- [ ] Confirm the meta config with the new `models.providers.meta.models[0]`
  (cost + maxTokens) and `agents.defaults` clamps validates against the pinned
  7.1 `.strict()` schema — the crash-loop guard. Fix key placement (Task 3) if
  it rejects. **Commit** any strategy tweak.

---

## Sequencing & self-review

- **Order:** T1 (constants) → T2 (record_usage) / T3 (config) / T4 (repo) in
  parallel-safe order → T5 (service, needs T1+T4) → T6 (wiring, needs T5) → T7
  (frontend) → T8 (schema).
- **Placeholder scan:** none — every task carries code + insertion point. Three
  items are explicitly flagged to **confirm at implementation** (the combined
  `SET if_not_exists + ADD` expression in T4; the exact `agents.defaults` key
  nesting in T3; which frontend file renders chat errors in T7) — each has a
  governing test so behavior is pinned regardless.
- **Consistency:** microcents throughout; `session_key` is the reserve key;
  `trial_local` is the only metered status; `meta/` prefix identifies meta.
- **Deferred (other sub-specs / Phase C):** entitlement config/exec lock (which
  closes the in-container-rate tamper vector), scale-to-zero, the upgrade
  action behind the CTA.
- **Explicit risk:** the in-process reserve map is single-backend-instance; a
  backend restart mid-turn drops the reserve (fail-closed, not refunded).
