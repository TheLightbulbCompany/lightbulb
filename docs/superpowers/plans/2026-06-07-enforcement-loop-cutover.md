# Bedrock Enforcement Loop + Single-Deduct Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the AWS invocation-log meter the **sole** deduct path and wire the enforcement loop around it: re-point both balance gates (chat + provision) through the swappable `LedgerBackend` interface, fire a two-pronged kill-switch (IAM revoke + ECS `desired=0`) when a `deplete()` crosses balance ≤ 0, shadow-then-delete the legacy `connection_pool` deduct so exactly one biller remains, and add a per-env daily drift monitor reconciling Cost Explorer against the new `credit-transactions` deduct rows.

**Architecture:** The pre-chat gate (`connection_pool.gate_chat`) and the provision gate (`provision_gate.evaluate_provision_gate`) stop calling `credit_ledger.get_balance` directly and instead read `get_ledger().get_balance(owner, consistent=…)` — one money source, the strong-read on the chat path. The meter (Plan 3) calls `get_ledger().deplete(event)` and then `enforce_after_deplete(owner_id, result)` — a Plan-4-owned helper that, when the event was newly applied AND the resulting balance ≤ 0, fires `EcsManager.kill_owner_bedrock(owner_id)` (IAM `revoke_owner_bedrock` from Plan 2 + ECS `stop_user_service`). The kill is durable because the now-≤0 balance keeps the provision gate closed, so the next `/container/status` poll cannot re-launch. The legacy deduct (`_maybe_deduct_credits`) is first instrumented with a shadow log, then deleted together with its call site when the meter goes live — gated behind the `METER_IS_LIVE` setting so the legacy-delete and the Plan-3 `deplete()` flip are order-independent rather than relying on two PRs landing in the same release (see "The exactly-once correctness boundary" below). A `scripts/drift_monitor.py` wrapper runs the existing reconciler against a new `fetch_ledger_deduct_spend_usd` (sums `credit-transactions` `type=="deduct"` rows) and emits a per-env CloudWatch metric — NOT an account-global Cost Anomaly monitor, because such a monitor collides across the per-env dev/prod stacks (the design doc §3.7 per-env-resource lesson).

**Tech Stack:** Python 3.12, `boto3` (ECS `update_service`, DynamoDB scan, Cost Explorer, CloudWatch `put_metric_data`), `pytest` + `pytest-asyncio` + `moto[dynamodb]>=5.1.22` for tests, `unittest.mock` (`AsyncMock`/`MagicMock`/`patch`). One `core/config.py` setting added (`METER_IS_LIVE`); no CDK stack edit in this plan (the drift monitor uses an in-process `put_metric_data` emit, not an EventBridge / Cost-Anomaly resource). Consumes Plan 1's `core.billing.ledger_interface` and Plan 2's `core.billing.bedrock_aip.revoke_owner_bedrock`.

---

## Context for the implementer (read before Task 1)

This is **Plan 4 of 4** for Phase 1 of the Bedrock metering remediation (spec: `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`). The sibling plans you must NOT touch but DO compose with:
- **Plan 1** (`docs/superpowers/plans/2026-06-07-ledger-interface-ddb-idempotency.md`, already written) — defines `core/billing/usage_event.py::UsageEvent`, `core/billing/ledger_interface.py::{LedgerBackend, DepleteResult, get_ledger}`, and `core/billing/ddb_ledger_backend.py::DdbLedgerBackend`. `DepleteResult` is a `NamedTuple(balance_microcents: int, applied: bool)`. `get_balance(owner_id, *, consistent: bool = False) -> int`. `deplete(event) -> DepleteResult` is exactly-once on `event.request_id`. **CONSUME these; never redefine.**
- **Plan 2** (AIP + IAM enforcement) — creates `core/billing/bedrock_aip.py`, which OWNS the kill-switch PRIMITIVES `revoke_owner_bedrock(owner_id)` / `restore_owner_bedrock(owner_id)` plus the AIP resolvers `resolve_owner_from_aip_arn(aip_arn)` / `resolve_tier_from_aip_arn(aip_arn)` / `resolve_owner_from_identity_arn(identity_arn)`. **This plan CALLS `revoke_owner_bedrock`/`restore_owner_bedrock`; it does NOT implement them.** Plan 4 creates `core/services/bedrock_enforcement.py` with the ORCHESTRATOR functions `enforce_after_deplete` + `kill_owner_bedrock`, which IMPORT the primitives `revoke_owner_bedrock`/`restore_owner_bedrock` from `core.billing.bedrock_aip`. The two modules are separately owned: Plan 2 → `core/billing/bedrock_aip.py` (primitives + resolvers); Plan 4 → `core/services/bedrock_enforcement.py` (orchestrator).
- **Plan 3** (AWS invocation-log meter) — OWNS the Lambda transport + `POST /api/v1/internal/usage/from-bedrock-log`. Its endpoint calls Plan 1's `get_ledger().deplete(event)` then Plan 4's `enforce_after_deplete(owner_id, result)` after a LIVE deplete. **This plan DEFINES `enforce_after_deplete`; Plan 3 calls it.** The 3↔4 seam is bidirectional — Plan 3 lazy/function-level imports `enforce_after_deplete` from `core.services.bedrock_enforcement`, and Plan 4's helpers lazy-import the Plan-2 primitives — so neither direction creates an import cycle.

**Why this plan exists.** Today the only deduct happens in `connection_pool` on the chat lifecycle `phase=="end"` event (`_record_usage_from_session` → `_fetch_and_record_usage` → `_maybe_deduct_credits` → `credit_ledger.deduct`). That path is web-coupled (needs a live pool connection + a broadcast terminal event) and never fires on `phase=="error"` — autonomous/heartbeat/cron runs go unbilled. Plan 3's AWS meter captures every run regardless of transport. Plan 4 makes the meter the sole biller and adds the enforcement reaction.

**The exactly-once correctness boundary (read carefully).** Plan 1's `DdbLedgerBackend.deplete` keys its audit row on `tx_id="deduct#{request_id}"`. The legacy `credit_ledger.deduct` keys on a **random** `tx_id` and ALSO decrements the same `credits.balance_microcents` row. The two write **different `tx_id` namespaces**, so they do NOT dedup against each other — if both run while the meter is live, the same balance row double-deducts (and if neither runs, it zero-deducts). This is the single highest-risk seam in the cutover.

Rather than rely on the legacy-delete PR (Task 6) and the meter's log-only→`deplete()` flip (Plan 3) "landing together" — fragile given `feedback_deploy_workflow_no_concurrency_guard` (simultaneous merges race) — both paths are gated on a single new setting **`METER_IS_LIVE`** (added in Task 6, default `False`):
- When `METER_IS_LIVE` is `False`: the legacy `credit_ledger.deduct` is the live biller; Plan 3's endpoint is log-only (it builds the `UsageEvent` and logs the would-be deplete, but does NOT call `deplete()`).
- When `METER_IS_LIVE` is `True`: Plan 3's endpoint calls `deplete()` (the sole biller); the legacy `_maybe_deduct_credits` call site is a no-op early-return.

This makes the two code paths **order-independent**: whichever PR merges first, exactly one biller is live at every point in time, and the cutover is a single env-var flip (`METER_IS_LIVE=true`) AFTER both PRs are deployed. Task 6 keeps the `_maybe_deduct_credits` method-deletion as the final step (post-flip cleanup), so the flip itself is reversible by toggling the flag back. Plan 3 must read the SAME `METER_IS_LIVE` setting to gate its `deplete()` call — coordinate the setting name with Plan 3.

Until the flip, Task 5's shadow is **log-only** — it does NOT add a second decrement; the legacy `credit_ledger.deduct` keeps being the live deduct during the shadow window.

**Sequencing.** Plan 4 deploys AFTER Plan 2 (the gates and kill-switch assume per-owner AIP ARNs are already in `openclaw.json` and the IAM role is scoped — otherwise the gate semantics are inconsistent with the fail-closed invoke boundary). Within this plan: Task 1 → 2 (gates) can ship independently; Tasks 3–4 (kill-switch + helper) depend on Plan 2's `revoke_owner_bedrock`; Tasks 5 → 6 (shadow → delete) are the cutover spine and the `METER_IS_LIVE` flip co-ordinates with Plan 3's `deplete` flip (no co-deploy race — see the correctness-boundary note above); Tasks 7–8 (drift monitor) are independent.

**Correctness gated on Plan 2 + Plan 3 live checks (read before trusting the kill-switch).** Plan 4's enforcement loop is purely balance-driven — `enforce_after_deplete` fires whenever a newly-applied deplete crosses ≤ 0, regardless of `run_status`. That correctness therefore *depends on two things this plan does NOT verify and cannot test in isolation*:
1. **Plan 2's AIP-ARN attribution must be live (spec §3.2.1 / §3.6 build gate).** The kill is only meaningful if the depleted owner is the owner who actually spent. That attribution runs through Plan 2's per-owner AIP ARNs being the container's `openclaw.json` model ids (so `modelId` in the invocation log is the AIP id and `identity.arn` is the per-owner role) and Plan 2's `resolve_owner_from_aip_arn` (defined in `core.billing.bedrock_aip`, called by Plan 3's meter to attribute each log record). If Plan 2's §3.6 live-AWS check (does `modelId` log as the AIP id, `identity.arn` as the per-owner role?) has NOT passed, the meter mis-attributes and the kill fires on the wrong owner — or never fires for the real spender. **Do not enable `METER_IS_LIVE=true` until Plan 2's §3.6 live check is green.**
2. **Plan 3's `run_status` field-path parse must capture error/aborted runs (spec §3.2.2).** The literal runaway this whole effort targets — the failing Friday heartbeat loop firing `phase=="error"` ~every 22 min, 24/7, all unbilled (memory `project_bedrock_cost_runaway_and_metering_blindness`) — only gets billed (and thus only triggers the kill) if Plan 3's defensive parse correctly extracts `run_status` for error/aborted invocation-log records. If Plan 3's EARLY real-log-capture task has not locked that field path, the kill-switch never fires for the exact failure mode it exists to stop. Plan 4 deletes the legacy deduct (which never billed on error anyway), so error/aborted capture is wholly Plan 3's responsibility — flag this dependency explicitly rather than assuming it.

**Key existing facts (verified in code):**
- `connection_pool.gate_chat` (`core/gateway/connection_pool.py:1849`) reads `credit_ledger.get_balance(owner_id, consistent=True)` at line 1905; imports are lazy/in-function at lines 1879–1881. Signature: `async def gate_chat(self, *, user_id: str, owner_id: str) -> dict`.
- `provision_gate.evaluate_provision_gate` reads `credit_ledger.get_balance(owner_id)` (eventually-consistent default) at `core/services/provision_gate.py:261`; module import at line 23.
- `connection_pool._maybe_deduct_credits` (`core/gateway/connection_pool.py:1139–1231`) applies markup `raw * 14 // 10` (line 1216) and calls `credit_ledger.deduct` (line 1217). Its only call site lives in `_fetch_and_record_usage`: the explanatory comment block starts at line 1112 and the `try:` wrapping the call is at line 1118, so the full deletable span (comment + try/except) is lines 1112–1133.
- `EcsManager.stop_user_service` (`core/containers/ecs_manager.py:631`) already does `update_service(cluster, service, desiredCount=0)` (lines 645–649) + sets DDB status "stopped". Reuse it.
- `record_usage` (`core/services/usage_service.py`) is analytics-only — **KEEP it**; only the deduct goes.
- `scripts/reconcile_bedrock_vs_ledger.py` has pure helpers (`DollarResult`, `format_dollar_report`, `_period_strings`) and `async def main(args) -> int`. Its `fetch_ledger_spend_usd` reads `usage-counters.total_spend_microdollars` (analytics) — post-cutover the real money is in `credit-transactions`, so a NEW fetch is required.
- `core.dynamodb` has `get_table(short_name)` and `async run_in_thread(func, *args, **kwargs)`.
- Tests mock DDB with `moto`'s `mock_aws()`, mock ECS with `MagicMock`, and patch with `AsyncMock`/`patch`. Gate tests live in `tests/unit/gateway/test_connection_pool_credits.py`; ECS tests in `tests/unit/containers/test_ecs_manager.py`.

**Run all commands from `apps/backend/` unless noted. Use `uv run` for Python. Do NOT use `rg` (shim swallows stdout); use `grep -rn --include="*.py"` or the Grep tool.**

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/core/gateway/connection_pool.py` (Modify, line 1880 + 1905) | Re-point `gate_chat` balance read to `get_ledger().get_balance`. |
| `apps/backend/core/services/provision_gate.py` (Modify, line 23 + 261) | Re-point `evaluate_provision_gate` balance read to `get_ledger().get_balance`. |
| `apps/backend/core/services/bedrock_enforcement.py` (Create — owned by Plan 4; the orchestrator. Imports the `revoke_owner_bedrock`/`restore_owner_bedrock` primitives from `core.billing.bedrock_aip`, owned by Plan 2) | The post-deplete enforcement policy: `enforce_after_deplete` (+ `kill_owner_bedrock` lives on `EcsManager`); kill on balance ≤ 0. |
| `apps/backend/core/containers/ecs_manager.py` (Modify, after line 663) | Add `kill_owner_bedrock(owner_id)` = IAM revoke + ECS `stop_user_service`. |
| `apps/backend/core/gateway/connection_pool.py` (Modify, line 1216 — Task 5) | Shadow-log the legacy deduct (no behavior change). |
| `apps/backend/core/gateway/connection_pool.py` (Modify, lines 1112–1133 + 1139–1231 — Task 6) | Gate the legacy deduct call on `not METER_IS_LIVE`, then delete the call + `_maybe_deduct_credits` method post-flip. |
| `apps/backend/core/config.py` (Modify — Task 6, Step 0a–0e) | Add `METER_IS_LIVE: bool = False` (the cutover flag OWNED by Plan 4; Plan 3's meter endpoint CONSUMES it to run shadow-only when `False`, live `deplete()` when `True`). |
| `apps/backend/scripts/reconcile_bedrock_vs_ledger.py` (Modify, after line 372) | Add `fetch_ledger_deduct_spend_usd(periods)` reading `credit-transactions`. |
| `apps/backend/scripts/drift_monitor.py` (Create) | Daily wrapper: run the reconciler, emit a per-env CloudWatch metric. |
| `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` (Modify — Task 1 + 5 + 6) | Migrate gate-mock target; add shadow test; assert no-deduct-call. |
| `apps/backend/tests/unit/services/test_provision_gate_ledger.py` (Create — Task 2) | Provision gate reads through `get_ledger`. |
| `apps/backend/tests/unit/services/test_bedrock_enforcement.py` (Create — Task 3) | `enforce_after_deplete` fires/skips correctly. |
| `apps/backend/tests/unit/containers/test_ecs_manager_killswitch.py` (Create — Task 4) | `kill_owner_bedrock` fires both halves. |
| `apps/backend/tests/unit/scripts/test_drift_monitor.py` (Create — Task 7 + 8) | `fetch_ledger_deduct_spend_usd` sum + drift-alert threshold. |

---

### Task 1: Re-point the pre-chat balance gate to the ledger interface

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (import block at 1879–1881; balance read at 1905)
- Test: `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` (migrate the gate-mock target on the 4 card-3 gate tests)

- [ ] **Step 1: Migrate the failing gate tests to the new patch target**

The existing tests patch `core.services.credit_ledger.get_balance`. After the cutover the gate calls `core.billing.ledger_interface.get_ledger`, so the patch target must change. Edit `apps/backend/tests/unit/gateway/test_connection_pool_credits.py`.

Replace `test_card3_with_zero_balance_blocked` (lines 29–50) with:

```python
@pytest.mark.asyncio
async def test_card3_with_zero_balance_blocked():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=0)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "Top up" in result["message"]
```

Replace `test_card3_with_positive_balance_allowed` (lines 53–72) with:

```python
@pytest.mark.asyncio
async def test_card3_with_positive_balance_allowed():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=5_000_000)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
```

Replace `test_card3_uses_consistent_read` (lines 75–95) with:

```python
@pytest.mark.asyncio
async def test_card3_uses_consistent_read():
    """Top-ups landed via webhook must unblock the next chat without
    eventual-consistency lag — gate_chat reads with consistent=True."""
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=10_000_000)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        await pool.gate_chat(user_id="u_1", owner_id="o_1")
    _, kwargs = ledger.get_balance.call_args
    assert kwargs.get("consistent") is True
```

Replace `test_card3_gate_keyed_on_owner_for_org_credits_pool` (lines 98–122) with:

```python
@pytest.mark.asyncio
async def test_card3_gate_keyed_on_owner_for_org_credits_pool():
    """After the org-pooled-credits fix, both provider_choice (from
    billing_repo) AND credit balance (from the ledger) are keyed on
    owner_id. Org members all draw from one pool funded by the admin —
    without this, members see balance=0 and get out_of_credits.
    """
    pool = GatewayConnectionPool(management_api=None)
    billing_mock = AsyncMock(
        return_value={
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }
    )
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=1_000_000)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="member_X", owner_id="org_Y")

    assert result == {"blocked": False}
    billing_mock.assert_awaited_once_with("org_Y")
    balance_args, _ = ledger.get_balance.call_args
    assert balance_args[0] == "org_Y"
```

Replace the card-1 test `test_card1_oauth_user_never_gated_by_credits` (lines 125–143) with:

```python
@pytest.mark.asyncio
async def test_card1_oauth_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "chatgpt_oauth",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    ledger.get_balance.assert_not_called()
```

Replace the card-2 test `test_card2_byo_user_never_gated_by_credits` (lines 146–164) with:

```python
@pytest.mark.asyncio
async def test_card2_byo_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "byo_key",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    ledger.get_balance.assert_not_called()
```

Add `from unittest.mock import AsyncMock, MagicMock, patch` if `MagicMock` is not already imported — the current import (line 19) is `from unittest.mock import AsyncMock, patch`. Replace it with:

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -k "card3 or card1_oauth or card2_byo" -v`
Expected: FAIL — the gate still calls `credit_ledger.get_balance` (not the patched `get_ledger`), so `ledger.get_balance.assert_not_called()` / the blocked assertions fail (the real `credit_ledger.get_balance` runs against no DDB and errors, OR the unmocked path returns the wrong result).

- [ ] **Step 3: Re-point the gate to the ledger interface**

In `apps/backend/core/gateway/connection_pool.py`, replace the lazy import block at lines 1879–1881:

```python
        from core.repositories import billing_repo
        from core.services import credit_ledger
        from core.services.provision_gate import is_subscription_active
```

with (drop `credit_ledger`, add `get_ledger`):

```python
        from core.billing.ledger_interface import get_ledger
        from core.repositories import billing_repo
        from core.services.provision_gate import is_subscription_active
```

Then replace the balance read at line 1905:

```python
        balance = await credit_ledger.get_balance(owner_id, consistent=True)
```

with:

```python
        balance = await get_ledger().get_balance(owner_id, consistent=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -k "card3 or card1_oauth or card2_byo" -v`
Expected: PASS (the 6 migrated gate tests)

- [ ] **Step 5: Run the whole gate-test module to confirm no collateral breakage**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -v`
Expected: PASS for all gate tests. (The `_maybe_deduct_credits` tests still patch `credit_ledger.deduct` and still pass — Task 1 did not touch the deduct path.)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_credits.py
git commit -m "refactor(billing): read pre-chat balance gate through the ledger interface"
```

---

### Task 2: Re-point the provision-time balance gate to the ledger interface

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py` (import at line 23; balance read at line 261)
- Test: `apps/backend/tests/unit/services/test_provision_gate_ledger.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_provision_gate_ledger.py`:

```python
"""The provision-time balance gate must read through the ledger interface,
not core.services.credit_ledger directly — so the chat gate and the
provision gate read one money source. A top-up that unblocks chat must
also let the container re-provision (and a runaway that drained the
balance must keep it down)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.provision_gate import evaluate_provision_gate


def _billing_account(**overrides) -> dict:
    base = {
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_zero_balance_returns_credits_required_gate_via_ledger():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=0)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        gate = await evaluate_provision_gate(
            owner_id="org_Y", clerk_user_id="user_X", is_admin=True
        )
    assert gate is not None
    assert gate.code == "credits_required"
    balance_args, _ = ledger.get_balance.call_args
    assert balance_args[0] == "org_Y"


@pytest.mark.asyncio
async def test_positive_balance_passes_balance_layer_via_ledger():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=5_000_000)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        gate = await evaluate_provision_gate(
            owner_id="org_Y", clerk_user_id="user_X", is_admin=True
        )
    # bedrock_claude with positive balance clears the balance layer; any
    # gate returned here is NOT credits_required.
    assert gate is None or gate.code != "credits_required"


@pytest.mark.asyncio
async def test_non_bedrock_provider_never_reads_balance():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account(provider_choice="byo_key")),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        await evaluate_provision_gate(
            owner_id="org_Y", clerk_user_id="user_X", is_admin=True
        )
    ledger.get_balance.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_provision_gate_ledger.py -v`
Expected: FAIL — the gate still calls `credit_ledger.get_balance`, so the patched `ledger.get_balance` is never invoked: `test_zero_balance_returns_credits_required_gate_via_ledger` fails at `ledger.get_balance.call_args` (None) and `test_non_bedrock_provider_never_reads_balance` may pass only by accident; the zero-balance test is the load-bearing failure.

- [ ] **Step 3: Re-point the provision gate to the ledger interface**

In `apps/backend/core/services/provision_gate.py`, replace the import at line 23:

```python
from core.services import credit_ledger
```

with:

```python
from core.billing.ledger_interface import get_ledger
```

Then replace the balance read at line 261:

```python
        balance = await credit_ledger.get_balance(owner_id)
```

with:

```python
        balance = await get_ledger().get_balance(owner_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_provision_gate_ledger.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the existing provision_gate suite to confirm no regression**

Run: `uv run pytest tests/unit/services/ -k provision_gate -v`
Expected: PASS — pre-existing provision_gate tests that did NOT patch the balance read still pass (they exercise the subscription/provider layers, which are unchanged). If a pre-existing test patched `core.services.credit_ledger.get_balance` against `provision_gate`, it will now fail because the gate no longer imports that symbol; migrate that test's patch target to `core.billing.ledger_interface.get_ledger` the same way Task 1 did, then re-run.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/provision_gate.py apps/backend/tests/unit/services/test_provision_gate_ledger.py
git commit -m "refactor(billing): read provision-time balance gate through the ledger interface"
```

---

### Task 3: `enforce_after_deplete(owner_id, result)` — the post-deplete kill policy

**Files:**
- Create: `apps/backend/core/services/bedrock_enforcement.py` (owned by Plan 4 — the orchestrator; defines `enforce_after_deplete`). The `revoke_owner_bedrock`/`restore_owner_bedrock` primitives it relies on live in `core/billing/bedrock_aip.py` (owned by Plan 2) and are imported lazily, not redefined here.
- Test: `apps/backend/tests/unit/services/test_bedrock_enforcement.py` (Create)

**Design (read before coding):**
Plan 3's meter endpoint calls `result = await get_ledger().deplete(event)` then `await enforce_after_deplete(event.owner_id, result)`. The policy: fire the kill-switch **only** when this event was *newly applied* AND the balance is now ≤ 0. A deduplicated replay (`result.applied is False`) must NOT re-fire the kill (it would needlessly thrash ECS on every DLQ re-delivery). A positive balance must NOT fire. The kill itself is `EcsManager.kill_owner_bedrock` (Task 4). `enforce_after_deplete` lives in `core/services/bedrock_enforcement.py` (this plan's own module) so Plan 3's transport stays dumb (symmetric with Plan 1 owning `deplete` and Plan 3 calling it).

`core/services/bedrock_enforcement.py` is created by THIS plan (Plan 4). `enforce_after_deplete` does NOT import `revoke_owner_bedrock` directly — it imports `EcsManager` lazily and calls `kill_owner_bedrock`, which itself lazy-imports the `revoke_owner_bedrock` primitive from `core.billing.bedrock_aip` (Plan 2's module). So the only place that touches the Plan-2 primitive is `EcsManager.kill_owner_bedrock` (Task 4). This keeps the ownership clean: the orchestrator (`enforce_after_deplete`) lives here in `core/services/bedrock_enforcement.py`; the IAM primitives (`revoke_owner_bedrock`/`restore_owner_bedrock`) live in Plan 2's `core/billing/bedrock_aip.py` and are imported, never redefined.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_bedrock_enforcement.py`:

```python
"""enforce_after_deplete: fire the kill-switch only when a NEWLY-applied
deplete crosses balance <= 0. Replays and positive balances do nothing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing.ledger_interface import DepleteResult
from core.services.bedrock_enforcement import enforce_after_deplete


def _killer(*, already_stopped: bool = False) -> MagicMock:
    """An EcsManager stand-in: not-yet-stopped by default so the kill fires."""
    killer = MagicMock()
    killer.kill_owner_bedrock = AsyncMock()
    killer.is_owner_service_stopped = AsyncMock(return_value=already_stopped)
    return killer


@pytest.mark.asyncio
async def test_fires_kill_when_applied_and_balance_non_positive():
    killer = _killer()
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=-100, applied=True)
        )
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_fires_kill_at_exactly_zero():
    killer = _killer()
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=0, applied=True)
        )
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_does_not_fire_on_dedup_replay():
    killer = _killer()
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=-100, applied=False)
        )
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_fire_when_balance_positive():
    killer = _killer()
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=42_000, applied=True)
        )
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_refire_when_service_already_stopped():
    """During the ~5-15s IAM-propagation window after the first kill, a NEW
    requestId's applied deplete also sees balance<=0 — but the service is
    already stopped, so the redundant kill (and its IAM revoke + metric) is
    skipped."""
    killer = _killer(already_stopped=True)
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=-9_999, applied=True)
        )
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_kill_failure_does_not_propagate():
    """A flaky ECS/IAM call must not crash the meter ingest path — the
    balance is already depleted; the kill is best-effort and the next
    deplete (or the provision gate) re-asserts the stop."""
    killer = _killer()
    killer.kill_owner_bedrock = AsyncMock(side_effect=RuntimeError("ecs down"))
    with patch(
        "core.containers.get_ecs_manager", return_value=killer
    ):
        # Must not raise.
        await enforce_after_deplete(
            "org_Y", DepleteResult(balance_microcents=-1, applied=True)
        )
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")
```

Note the patch target is `core.containers.get_ecs_manager` (the source module), NOT `core.services.bedrock_enforcement.get_ecs_manager` — because `enforce_after_deplete` does an in-function `from core.containers import get_ecs_manager`, the name is looked up on `core.containers` at call time, so that is where `patch` must replace it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_bedrock_enforcement.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.services.bedrock_enforcement'` (this plan creates the module in Step 3), or `ImportError: cannot import name 'enforce_after_deplete' from 'core.services.bedrock_enforcement'` if only a stub header exists.

- [ ] **Step 3: Add `enforce_after_deplete`**

`apps/backend/core/services/bedrock_enforcement.py` is created by THIS plan (Plan 4). Create it with this header:

```python
"""Bedrock per-owner enforcement orchestrator: the post-deplete policy.

Plan 4 owns this module — the post-deplete enforcement policy
(`enforce_after_deplete`). The IAM kill-switch PRIMITIVES it relies on
(`revoke_owner_bedrock` / `restore_owner_bedrock`) plus the AIP resolvers
(`resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn` /
`resolve_owner_from_identity_arn`) live in `core/billing/bedrock_aip.py`
(owned by Plan 2) and are imported lazily on the kill path — never
redefined here. This module is the orchestrator; `bedrock_aip` is the
primitive layer.
"""

from __future__ import annotations

import logging

from core.billing.ledger_interface import DepleteResult
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)
```

If a stub of the file already exists (e.g. a scaffold was committed earlier), add only the imports it lacks (`DepleteResult`, `put_metric`, `logging`/`logger`) to the existing header — do not duplicate the module docstring. This plan does NOT define the Plan-2 primitives in this file; they stay in `core/billing/bedrock_aip.py`.

**Import-cycle note (why `get_ecs_manager` is NOT a module-level import):** `get_ecs_manager` lives in `core/containers/__init__.py`, which does `from core.gateway.connection_pool import GatewayConnectionPool` at module top (verified: `core/containers/__init__.py:13`). Importing `get_ecs_manager` at the top of `bedrock_enforcement.py` would therefore eagerly pull `connection_pool` + `ecs_manager` + the whole container-orchestration tree into this module's import — a heavy, fragile chain (and Task 4 makes `ecs_manager.kill_owner_bedrock` lazy-import the `revoke_owner_bedrock` primitive from `core.billing.bedrock_aip`, so the modules sit on a partially-initialised-import knife-edge if either side eagerly imports the other). So `enforce_after_deplete` imports `get_ecs_manager` **lazily, inside the function** (after the early-return guard, so the heavy import only happens on the rare kill path). The Task 3 tests patch `core.containers.get_ecs_manager` (the source module), which works because the in-function `from core.containers import get_ecs_manager` binds the name into this module's namespace at call time and `patch` replaces it on `core.containers` where the lookup resolves.

Then append the function:

```python
async def enforce_after_deplete(owner_id: str, result: DepleteResult) -> None:
    """React to one ledger deplete: fire the kill-switch when this event was
    newly applied AND the resulting balance is non-positive.

    Called by Plan 3's `POST /internal/usage/from-bedrock-log` endpoint right
    after `get_ledger().deplete(event)`. A deduplicated replay
    (``applied is False``) is a no-op — it must not thrash ECS on every DLQ
    re-delivery — and a still-positive balance is a no-op too. The kill is
    best-effort: an ECS/IAM failure is logged, not raised, because the spend
    is already incurred and the next deplete (or the provision gate's
    balance read) re-asserts the stop.

    `get_ecs_manager` is imported lazily inside the function (after the guard)
    to avoid eagerly pulling the container-orchestration import tree into this
    module — see the import-cycle note above.
    """
    if not result.applied or result.balance_microcents > 0:
        return

    from core.containers import get_ecs_manager

    manager = get_ecs_manager()
    # Short-circuit repeated fires during the IAM-propagation window. The
    # FIRST applied deplete crossing <= 0 fires the kill; subsequent applied
    # deplete()s for NEW requestIds arriving in the ~5-15s before IAM/ECS
    # propagate would ALSO have balance <= 0 and re-fire. ECS desired=0 is
    # idempotent, but re-firing means repeated IAM revoke calls + metric
    # noise. If the service is already stopped, skip the redundant kill.
    if await manager.is_owner_service_stopped(owner_id):
        logger.info(
            "Owner %s already stopped — skipping redundant kill-switch fire",
            owner_id,
        )
        return

    put_metric(
        "billing.killswitch.fire",
        dimensions={"reason": "balance_non_positive"},
    )
    logger.warning(
        "Owner %s crossed balance<=0 (%d microcents) — firing Bedrock kill-switch",
        owner_id,
        result.balance_microcents,
    )
    try:
        await manager.kill_owner_bedrock(owner_id)
    except Exception:
        put_metric("billing.killswitch.error")
        logger.exception(
            "Kill-switch failed for owner %s — spend already incurred; the "
            "provision gate keeps the container down until balance recovers",
            owner_id,
        )
```

`get_ecs_manager` is the existing singleton accessor in `core/containers/__init__.py` (the kill-switch reuses the same `EcsManager` instance the rest of the backend uses). Verify it is exported there before relying on it:

Run: `grep -n "def get_ecs_manager" apps/backend/core/containers/__init__.py`
Expected: one hit — `def get_ecs_manager()`. If it is named differently, import the actual accessor and patch the matching name in the test.

`is_owner_service_stopped(owner_id) -> bool` is a small helper added to `EcsManager` in Task 4 (it reads the container DDB row's status; returns `True` when the row's status is already `"stopped"`). It exists so the short-circuit does not depend on container-state internals leaking into `bedrock_enforcement`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_bedrock_enforcement.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/bedrock_enforcement.py apps/backend/tests/unit/services/test_bedrock_enforcement.py
git commit -m "feat(billing): enforce_after_deplete fires the kill-switch on balance<=0"
```

---

### Task 4: `EcsManager.kill_owner_bedrock` — IAM revoke + ECS desired=0

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (add method after `stop_user_service`, line 663)
- Test: `apps/backend/tests/unit/containers/test_ecs_manager_killswitch.py` (Create)

**Design (read before coding):**
`kill_owner_bedrock(owner_id)` is the two-pronged guaranteed stop: IAM `revoke_owner_bedrock` (the Plan-2 primitive in `core.billing.bedrock_aip` — ~5–15s eventually-consistent) AND `stop_user_service` (ECS `desired=0` — instant). It REUSES `stop_user_service` (line 631) rather than issuing a duplicate `update_service`. The IAM revoke is fired first so that even during the ECS drain window the role can no longer invoke Bedrock; the ECS stop is the durable instant cutoff. Both must fire; if the IAM revoke fails, the ECS stop must still happen (the container is the only spend source while running). `revoke_owner_bedrock` is imported lazily (`from core.billing.bedrock_aip import revoke_owner_bedrock`) inside the method to avoid the import cycle — see the Step 3 note.

This task also adds a tiny read helper `is_owner_service_stopped(owner_id) -> bool` that `enforce_after_deplete` (Task 3) calls to short-circuit redundant kills during the IAM-propagation window. It reads the container DDB row via the existing `container_repo.get_by_owner_id(owner_id)` and returns `True` when the row's `status == "stopped"` — exactly the status `stop_user_service` writes (verified: `ecs_manager.stop_user_service` calls `container_repo.update_status(user_id, "stopped")`). A missing row also returns `True` (nothing to kill).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_ecs_manager_killswitch.py`:

```python
"""EcsManager.kill_owner_bedrock fires BOTH halves of the kill-switch:
IAM revoke (Plan 2) + ECS desired=0 (reusing stop_user_service)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.fixture
def mock_settings():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        mgr = EcsManager()
    return mgr


@pytest.mark.asyncio
async def test_kill_fires_iam_revoke_and_ecs_stop(manager):
    revoke_mock = AsyncMock()
    with (
        patch(
            "core.billing.bedrock_aip.revoke_owner_bedrock",
            new=revoke_mock,
        ),
        patch.object(manager, "stop_user_service", new=AsyncMock()) as stop_mock,
    ):
        await manager.kill_owner_bedrock("org_Y")
    revoke_mock.assert_awaited_once_with("org_Y")
    stop_mock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_ecs_stop_runs_even_if_iam_revoke_raises(manager):
    """IAM revoke is eventually-consistent and may flake; the ECS desired=0
    cut MUST still fire — it is the guaranteed instant stop."""
    revoke_mock = AsyncMock(side_effect=RuntimeError("iam throttled"))
    with (
        patch(
            "core.billing.bedrock_aip.revoke_owner_bedrock",
            new=revoke_mock,
        ),
        patch.object(manager, "stop_user_service", new=AsyncMock()) as stop_mock,
    ):
        await manager.kill_owner_bedrock("org_Y")
    revoke_mock.assert_awaited_once_with("org_Y")
    stop_mock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_is_owner_service_stopped_true_when_status_stopped(manager):
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "stopped"}),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is True


@pytest.mark.asyncio
async def test_is_owner_service_stopped_false_when_running(manager):
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "running"}),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is False


@pytest.mark.asyncio
async def test_is_owner_service_stopped_true_when_no_row(manager):
    """No container row → nothing to kill → treat as already stopped."""
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is True
```

The patch target `core.containers.ecs_manager.container_repo.get_by_owner_id` matches how `stop_user_service` already calls `container_repo` (module-level `container_repo` import in `ecs_manager.py` — verify with `grep -n "import container_repo\|from core.repositories import" apps/backend/core/containers/ecs_manager.py`; if it is imported as a bare module, patch the attribute on it as shown).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_killswitch.py -v`
Expected: FAIL — `AttributeError: 'EcsManager' object has no attribute 'kill_owner_bedrock'` (and `is_owner_service_stopped`).

- [ ] **Step 3: Add `kill_owner_bedrock` to `EcsManager`**

In `apps/backend/core/containers/ecs_manager.py`, add this method immediately after `stop_user_service` ends (after line 663, before `start_user_service` at line 665):

```python
    async def kill_owner_bedrock(self, owner_id: str) -> None:
        """Two-pronged Bedrock kill-switch for an owner who ran out of credits.

        Fires BOTH:
          1. IAM revoke (`revoke_owner_bedrock` from
             `core.billing.bedrock_aip`, Plan 2's primitive) — detaches/denies
             the per-owner Bedrock policy. Eventually-consistent (~5-15s), so it
             is fired first to start the propagation clock.
          2. ECS `desiredCount=0` (via `stop_user_service`) — the guaranteed
             instant stop; the running container is the only live spend source.

        The IAM revoke is best-effort: if it raises, the ECS stop MUST still
        run (the container keeps invoking Bedrock until desired=0 lands). The
        stop's DDB status flip to "stopped" is durable only because the now-<=0
        balance keeps `evaluate_provision_gate` returning `credits_required`,
        so the next `/container/status` poll cannot re-launch the service.
        """
        from core.billing.bedrock_aip import revoke_owner_bedrock
        from core.observability.metrics import put_metric

        put_metric("container.killswitch.invoke")
        try:
            await revoke_owner_bedrock(owner_id)
        except Exception:
            put_metric("container.killswitch.iam_revoke_error")
            logger.exception(
                "IAM revoke failed for owner %s during kill-switch — "
                "proceeding to ECS desired=0 (the guaranteed stop)",
                owner_id,
            )
        await self.stop_user_service(owner_id)
        logger.warning("Bedrock kill-switch completed for owner %s", owner_id)

    async def is_owner_service_stopped(self, owner_id: str) -> bool:
        """True when the owner's container is already stopped (or has no row).

        Read by `enforce_after_deplete` to skip a redundant kill during the
        ~5-15s IAM-propagation window. Mirrors the status `stop_user_service`
        writes (`container_repo.update_status(owner_id, "stopped")`); a missing
        row means there is nothing to kill, so it is treated as stopped.
        """
        container = await container_repo.get_by_owner_id(owner_id)
        if container is None:
            return True
        return container.get("status") == "stopped"
```

The `revoke_owner_bedrock` import (`from core.billing.bedrock_aip import revoke_owner_bedrock`, Plan 2's primitive module) is lazy/in-function to avoid an import cycle (`bedrock_enforcement` imports `core.containers` for `get_ecs_manager`, which imports `ecs_manager`, which would otherwise import the primitive eagerly). `logger` and `container_repo` already exist at module scope in `ecs_manager.py` (`from core.repositories import container_repo` at line 32).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/containers/test_ecs_manager_killswitch.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the existing ECS manager suite to confirm no regression**

Run: `uv run pytest tests/unit/containers/test_ecs_manager.py -v`
Expected: PASS — the new method does not change `stop_user_service` or `__init__`, so existing tests are untouched.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager_killswitch.py
git commit -m "feat(containers): kill_owner_bedrock = IAM revoke + ECS desired=0"
```

---

### Task 5: Shadow the legacy deduct (log-only, no behavior change)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (after the `marked_up` computation at line 1216, before the `credit_ledger.deduct` call at line 1217)
- Test: `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` (add a shadow-log assertion)

**Design (read before coding):**
The shadow is **log-only** — it does NOT add a second decrement. Its purpose is to record what the legacy path would deduct (`raw` + `marked_up` + `model`) so the operator can diff the legacy amount against what Plan 3's AWS meter writes to `credit-transactions` for the same window. The legacy `credit_ledger.deduct` at line 1217 stays live and is the only path that decrements during the shadow window (the meter is still log-only on Plan 3's side). The shadow's integer markup `raw * 14 // 10` is the SAME formula the meter must use — so a non-zero diff later means a real discrepancy, not IEEE-754 float drift.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/gateway/test_connection_pool_credits.py`:

```python
# --------- Task 5: shadow-log the legacy deduct ---------


@pytest.mark.asyncio
async def test_legacy_deduct_emits_shadow_metric(caplog):
    """Before deleting the legacy biller, _maybe_deduct_credits emits a
    low-cardinality shadow metric (model only) PLUS a structured log line
    carrying the would-be raw + marked_up amounts so the operator can diff
    the legacy path against the AWS meter's deductions. The raw/marked-up
    amounts are unbounded-cardinality, so they live in the log, NOT as
    CloudWatch dimensions."""
    import logging

    conn = _make_connection(user_id="org_Y")
    deduct_mock = AsyncMock(return_value=0)
    metric_calls = []

    def _capture_metric(name, **kwargs):
        metric_calls.append((name, kwargs))

    with (
        caplog.at_level(logging.INFO, logger="core.gateway.connection_pool"),
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
        patch("core.gateway.connection_pool.put_metric", new=_capture_metric),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_shadow",
            model="amazon-bedrock/anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )

    # Legacy deduct still fires (shadow does not replace it yet).
    deduct_mock.assert_awaited_once()
    shadow = [c for c in metric_calls if c[0] == "billing.legacy_deduct.shadow"]
    assert len(shadow) == 1
    dims = shadow[0][1]["dimensions"]
    assert dims["model"] == "anthropic.claude-sonnet-4-6"
    # The unbounded-cardinality amounts must NOT be metric dimensions.
    assert "raw_microcents" not in dims
    assert "marked_up_microcents" not in dims
    # They are in the structured log line instead. raw 11_550 -> 1.4x ->
    # 16_170 (matches the AWS meter's raw*14//10).
    shadow_log = next(
        r.getMessage() for r in caplog.records if "SHADOW legacy deduct" in r.getMessage()
    )
    assert "raw=11550" in shadow_log
    assert "marked_up=16170" in shadow_log
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py::test_legacy_deduct_emits_shadow_metric -v`
Expected: FAIL — `assert len(shadow) == 1` fails (0) because no shadow metric is emitted yet.

- [ ] **Step 3: Add the shadow log**

In `apps/backend/core/gateway/connection_pool.py`, the current code at lines 1216–1217 is:

```python
        marked_up = raw * 14 // 10
        await credit_ledger.deduct(
```

Insert the shadow emit between them:

```python
        marked_up = raw * 14 // 10
        # Shadow phase: log what THIS legacy path would deduct so the
        # operator can diff it against the AWS invocation-log meter's
        # deductions for the same window before the meter becomes the sole
        # biller. raw*14//10 matches the meter's markup exactly, so a
        # non-zero diff is a real discrepancy, not float drift. This does
        # NOT add a second decrement — the deduct below is still the live one.
        #
        # The metric carries ONLY the low-cardinality `model` dimension; the
        # raw/marked-up amounts are unbounded-cardinality (every distinct cost
        # is a new dimension value), so they go in the structured log line, not
        # as CloudWatch dimensions (per core/observability/metrics.py:put_metric
        # — high-cardinality values must be log fields, not metric dimensions).
        put_metric(
            "billing.legacy_deduct.shadow",
            dimensions={"model": bare_model},
        )
        logger.info(
            "SHADOW legacy deduct owner=%s session=%s raw=%d marked_up=%d model=%s",
            self.user_id,
            chat_session_id,
            raw,
            marked_up,
            bare_model,
        )
        await credit_ledger.deduct(
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py::test_legacy_deduct_emits_shadow_metric -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the deduct-path tests to confirm no behavior change**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -k "deduct" -v`
Expected: PASS — every existing `_maybe_deduct_credits` test still passes (the shadow is additive; `credit_ledger.deduct` is still called with identical args).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_credits.py
git commit -m "feat(billing): shadow-log the legacy deduct ahead of the meter cutover"
```

---

### Task 6: Gate then delete the legacy deduct path (meter becomes the sole biller)

**Files:**
- Modify: `apps/backend/core/config.py` (add `METER_IS_LIVE: bool = False`)
- Modify: `apps/backend/core/gateway/connection_pool.py` (gate the `_maybe_deduct_credits` call at lines 1112–1133 on `not settings.METER_IS_LIVE`, then delete the call + the `_maybe_deduct_credits` method at lines 1139–1231 post-flip)
- Test: `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` (replace the `_maybe_deduct_credits`-targeted tests with a "no deduct from chat path" assertion; keep the analytics assertions)

**Design (read before coding):**
**Order-independent cutover via `METER_IS_LIVE` (NOT a co-deploy race):** The legacy deduct and Plan 3's `deplete()` must never be both-live (double-deduct) or both-dead (zero-deduct). Because they write **different `tx_id` namespaces** they do NOT dedup against each other (see "The exactly-once correctness boundary" in the context section), so the safe seam is a single shared `METER_IS_LIVE` setting, not two PRs landing together (which `feedback_deploy_workflow_no_concurrency_guard` warns is fragile):

1. **This task adds `METER_IS_LIVE: bool = False`** to `core/config.py` and gates the legacy call so it only runs when `not settings.METER_IS_LIVE`. Plan 3's endpoint gates its `deplete()` on `settings.METER_IS_LIVE` being `True` (coordinate the name with Plan 3). With the flag `False` (the default at merge time), the legacy path stays the sole live biller no matter which PR merged first.
2. **The cutover is then a single env-var flip** `METER_IS_LIVE=true` AFTER both PRs are deployed AND Plan 2's §3.6 AIP-ARN attribution check + Plan 3's `run_status` parse are verified live (see the "Correctness gated on Plan 2 + Plan 3 live checks" note). The flip is reversible by toggling the flag back.
3. **The `_maybe_deduct_credits` method deletion is the FINAL, post-flip cleanup** (Steps 4+). Once `METER_IS_LIVE=true` has been validated in prod, the gated-off call site and the now-dead method are removed. The deletion is sequenced after the flag flip so the flag-gated path is the safety net during validation; deleting the method removes the ability to toggle back, so do it only after the meter is confirmed correct.

What stays: `record_usage` (analytics counters, line 1091) and the `_resolve_then_record` / `_fetch_and_record_usage` skeleton that drives it, including the `self._billing_tasks` await on `close()` (line 1715) — it now only awaits the analytics write, which is harmless to keep. What goes (in the post-flip cleanup): the `_maybe_deduct_credits` call (1112–1133, comment block + try/except) and the method (1139–1231). `credit_ledger.deduct` / `credit_ledger.get_balance` become dead production code (deletion deferred to Phase 3 with the whole module) — note this in a comment so the next reader isn't confused.

**This task as written ships the END state (flag added + call gated + method deleted + tests rewritten) in one PR** — the flag default `False` makes that PR safe to merge in any order vs Plan 3. The deletion steps below assume the flip has been validated; if you are merging before validation, do Steps 1–3 (add flag, gate the call) and DEFER Steps 4 (method delete) to a follow-up after the flip is green. Either way the test changes in Step 1 below target the gated/deleted end state.

- [ ] **Step 0a: Write the failing test for the `METER_IS_LIVE` setting**

`METER_IS_LIVE` is OWNED by THIS plan (Plan 4) — it is the single cutover flag both billers read. Plan 3's meter endpoint CONSUMES it: when `METER_IS_LIVE` is `False`, Plan 3 runs shadow-only (it builds the `UsageEvent` and logs the would-be deplete, but does NOT call `deplete()` and does NOT call `enforce_after_deplete`); when `True`, Plan 3 calls `deplete()` (the sole biller) and then `enforce_after_deplete`. Defining it here, defaulting `False`, makes the cutover order-independent (see "The exactly-once correctness boundary").

Create `apps/backend/tests/unit/test_meter_is_live_setting.py`:

```python
"""METER_IS_LIVE is the Plan-4-owned cutover flag. It must exist on the
settings object, be a bool, and default to False so the legacy deduct stays
the sole live biller until the explicit env-var flip. Plan 3's meter endpoint
reads this SAME flag to decide shadow-only (False) vs live deplete (True)."""

from core.config import Settings


def test_meter_is_live_defaults_false():
    settings = Settings()
    assert hasattr(settings, "METER_IS_LIVE")
    assert settings.METER_IS_LIVE is False


def test_meter_is_live_reads_env(monkeypatch):
    monkeypatch.setenv("METER_IS_LIVE", "true")
    settings = Settings()
    assert settings.METER_IS_LIVE is True
```

- [ ] **Step 0b: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_meter_is_live_setting.py -v`
Expected: FAIL — `AssertionError` on `hasattr(settings, "METER_IS_LIVE")` (the field does not exist yet), or `AttributeError` accessing `settings.METER_IS_LIVE`.

- [ ] **Step 0c: Add the `METER_IS_LIVE` setting**

In `apps/backend/core/config.py`, near the other billing/ledger settings, add:

```python
    METER_IS_LIVE: bool = False  # cutover flag OWNED by Plan 4: when True, Plan 3's AWS meter deplete() is the sole biller (and Plan 3 then calls enforce_after_deplete) and the legacy connection_pool deduct is gated off; when False, Plan 3 is shadow-only (logs, no deplete, no enforce) and the legacy deduct is the live biller. Both paths read this ONE flag so the cutover is order-independent.
```

- [ ] **Step 0d: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_meter_is_live_setting.py -v`
Expected: PASS (2 passed)

- [ ] **Step 0e: Commit the setting**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/test_meter_is_live_setting.py
git commit -m "feat(billing): add METER_IS_LIVE cutover flag (Plan 4 owns; Plan 3 consumes for shadow-vs-live)"
```

- [ ] **Step 1: Replace the deduct-targeted tests with a no-deduct assertion**

In `apps/backend/tests/unit/gateway/test_connection_pool_credits.py`, delete every test function that targets `_maybe_deduct_credits` or the legacy deduct path — **delete by symbol, not by absolute line range** (the line numbers have shifted: Task 5 appended `test_legacy_deduct_emits_shadow_metric` to the END of the file, so it is NOT inside the old 263–668 block). The functions to remove are the entire `# --------- Task 5: connection._maybe_deduct_credits ---------` block (every test that calls `conn._maybe_deduct_credits(...)` — including `test_deduct_for_card3_sonnet_with_markup`, the unknown-model/skip cases, and `test_deduct_none_override_falls_back_to_token_pricing`), the `D1: _fetch_and_record_usage threads estimatedCostUsd through` block, the `_make_connection` helper if no surviving test uses it, AND the `test_legacy_deduct_emits_shadow_metric` shadow test appended in Task 5 (it references the deleted method too). To find them all reliably:

Run: `grep -n "_maybe_deduct_credits\|def test_.*deduct\|estimatedCostUsd\|legacy_deduct_emits_shadow" apps/backend/tests/unit/gateway/test_connection_pool_credits.py`

Delete each test function that the grep surfaces as touching `_maybe_deduct_credits` / the deduct path (the gate tests from Task 1, which call `pool.gate_chat`, must NOT be deleted — they patch `get_ledger` and are still live). After deleting, append the analytics-only contract:

```python
# --------- Task 6: meter is the sole deduct path ---------


def _make_connection_with_rpc(session: dict, user_id: str = "u_1") -> GatewayConnection:
    """A GatewayConnection whose sessions.list RPC returns one *session* row."""
    conn = object.__new__(GatewayConnection)
    conn.user_id = user_id
    conn.send_rpc = AsyncMock()
    conn.wait_for_response = AsyncMock(return_value={"sessions": [session]})
    return conn


@pytest.mark.asyncio
async def test_fetch_and_record_still_records_analytics():
    """After the cutover, _fetch_and_record_usage records analytics usage
    counters but performs NO credit deduction — the AWS invocation-log
    meter is the sole biller (exactly-once on requestId)."""
    import uuid

    session_key = f"agent:main:{uuid.uuid4().hex}"
    session = {
        "key": session_key,
        "inputTokens": 10,
        "outputTokens": 5,
        "model": "amazon-bedrock/anthropic.claude-sonnet-4-6",
        "estimatedCostUsd": 1.5,
    }
    conn = _make_connection_with_rpc(session)
    record_mock = AsyncMock()
    deduct_mock = AsyncMock()

    with (
        patch("core.services.usage_service.record_usage", new=record_mock),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._fetch_and_record_usage(session_key, member_user_id="member_1")

    record_mock.assert_awaited_once()
    deduct_mock.assert_not_called()


def test_maybe_deduct_credits_method_is_removed():
    """The legacy in-container biller no longer exists — the AWS meter
    replaced it. Guards against an accidental re-introduction."""
    assert not hasattr(GatewayConnection, "_maybe_deduct_credits")
```

`uuid` is imported inside the test to avoid relying on the module-level import that the deleted block used.

> **If you are merging BEFORE the `METER_IS_LIVE=true` flip is validated** (the order-independent path), do NOT delete the method yet — instead replace `test_maybe_deduct_credits_method_is_removed` with a flag-gating test that asserts the legacy call is skipped when `METER_IS_LIVE` is `True` and still fires when it is `False`. The end-state PR below (method deleted) is the post-validation cleanup. Pick ONE: gated-but-present (deferred-merge) OR deleted (post-flip). The Steps that follow ship the deleted end state.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -k "still_records_analytics or method_is_removed" -v`
Expected: FAIL — `test_maybe_deduct_credits_method_is_removed` fails (the method still exists) and `test_fetch_and_record_still_records_analytics` fails (`deduct_mock.assert_not_called()` fails because `_fetch_and_record_usage` still calls `_maybe_deduct_credits` → `credit_ledger.deduct`).

- [ ] **Step 3: Delete the deduct call from `_fetch_and_record_usage`**

In `apps/backend/core/gateway/connection_pool.py`, delete lines 1112–1133 (the comment block + the `try/except` wrapping `_maybe_deduct_credits`). The current text to remove is:

> **(Deferred-merge variant:** instead of deleting, wrap the existing `try/except` in `if not settings.METER_IS_LIVE:` so it short-circuits once the flag flips. Add `from core.config import settings` if not already imported. The end-state deletion below supersedes this once the flip is validated.)

```python
            # Card-3 (bedrock_claude) deducts credits in addition to the
            # legacy usage_service path. Other cards skip.
            # Synchronous so the next chat sees the updated balance. The
            # ledger is owner-scoped — _maybe_deduct_credits keys on
            # self.user_id (the container owner), so org members share
            # one pooled balance.
            try:
                await self._maybe_deduct_credits(
                    chat_session_id=session_key,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    raw_cost_microcents=raw_cost_microcents,
                )
            except Exception:
                logger.exception(
                    "Failed to deduct credits for user %s session %s",
                    self.user_id,
                    session_key,
                )
```

Replace it with a single explanatory comment so the next reader knows billing moved out:

```python
            # Credit deduction moved OUT of the container path: the AWS
            # Bedrock invocation-log meter is now the sole biller (every-run,
            # cache-correct, exactly-once on requestId). This path keeps only
            # the analytics `record_usage` write above. The legacy
            # credit_ledger.deduct / get_balance are now dead production code
            # (deletion deferred to Phase 3 with the whole module).
```

- [ ] **Step 4: Delete the `_maybe_deduct_credits` method**

In the same file, delete the entire `_maybe_deduct_credits` method — from its `async def _maybe_deduct_credits(` (line 1139 before this edit) through the end of its body (the `logger.info(... bare_model)` call that ends the method, line 1231 before this edit) including the shadow block added in Task 5. The method begins at:

```python
    async def _maybe_deduct_credits(
        self,
        *,
        chat_session_id: str,
```

and ends at:

```python
        logger.info(
            "Deducted credits for owner %s session %s: raw=%d marked_up=%d model=%s",
            self.user_id,
            chat_session_id,
            raw,
            marked_up,
            bare_model,
        )
```

Remove every line of that method. The method directly above it (`_fetch_and_record_usage`) and below it (`_handle_message`, currently at line 1233) remain.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -k "still_records_analytics or method_is_removed" -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the full gateway test module to confirm no dangling references**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -v`
Expected: PASS for the whole file. (No remaining test references `_maybe_deduct_credits` — they were deleted in Step 1.)

- [ ] **Step 7: Confirm no other code references the deleted method**

Run: `grep -rn --include="*.py" "_maybe_deduct_credits" apps/backend`
Expected: no output (zero hits). If anything remains, it is a stale reference to fix before committing.

- [ ] **Step 8: Commit**

```bash
# core/config.py was already committed in Step 0e; this commit covers the
# connection_pool gate/delete + the test rewrite (plus the deferred-merge
# variant's `from core.config import settings` import if that path is taken).
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_credits.py
git commit -m "feat(billing): retire the legacy connection_pool deduct; AWS meter is the sole biller"
```

---

### Task 7: Drift monitor — `fetch_ledger_deduct_spend_usd` over `credit-transactions`

**Files:**
- Modify: `apps/backend/scripts/reconcile_bedrock_vs_ledger.py` (add `fetch_ledger_deduct_spend_usd` after `fetch_ledger_spend_usd`, line 372)
- Test: `apps/backend/tests/unit/scripts/test_drift_monitor.py` (Create)

**Design (read before coding):**
Post-cutover the authoritative spend is the NEW `credit-transactions` rows written by `DdbLedgerBackend.deplete` (`type=="deduct"`, `raw_cost_microcents` in microcents). The existing `fetch_ledger_spend_usd` reads `usage-counters.total_spend_microdollars` — that column is analytics-only after the cutover and is NOT what the meter deducts from. So the drift monitor needs a new fetch that sums the `credit-transactions` deduct rows. The window filter uses `created_at` (`YYYY-MM-DD...` ISO prefix, written by `deplete` as the audit row's `created_at`) against the period strings the reconciler already computes. Sum **`raw_cost_microcents`** (the un-marked-up cost), NOT `amount_microcents` — AWS bills raw, the ledger deducts the marked-up `amount`, so the drift comparison against the AWS bill must use raw.

**Test pattern (read carefully — do NOT use moto+`DYNAMODB_TABLE_PREFIX`):** `fetch_ledger_deduct_spend_usd` reaches the table via `core.dynamodb.get_table("credit-transactions")`, and `core.dynamodb._table_prefix` is read **once at module import** (`core/dynamodb.py:12`), seeded from `settings.DYNAMODB_TABLE_PREFIX` which is itself read at config-import time (default `"isol8-dev-"`). So `monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "isol8-test-")` is a **no-op** — by the time the test runs, both `_table_prefix` and `settings` are already imported, and `get_table("credit-transactions")` resolves to `isol8-dev-credit-transactions`, which a moto table named `isol8-test-credit-transactions` never created → `ResourceNotFoundException`. This is exactly why the existing, working `test_reconcile_bedrock_vs_ledger.py` tests do NOT use moto for the ledger fetch: they patch `core.dynamodb.get_table` with a `_FakeTable` stub and `core.dynamodb.run_in_thread` with a passthrough. Mirror that pattern here (the `_FakeTable` + `_passthrough_run_in_thread` stubs are defined in `test_reconcile_bedrock_vs_ledger.py` — copy small local equivalents into the new test file so it stands alone).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/scripts/test_drift_monitor.py`:

```python
"""Drift monitor: sum credit-transactions deduct rows (the real money
post-cutover) to USD, and the alert-threshold predicate.

Mirrors the WORKING fetch-test pattern in
tests/unit/scripts/test_reconcile_bedrock_vs_ledger.py: patch
core.dynamodb.get_table with a _FakeTable stub + core.dynamodb.run_in_thread
with a passthrough. Do NOT use moto + DYNAMODB_TABLE_PREFIX here — the prefix
is read once at import (core/dynamodb.py:12), so setenv is a no-op and
get_table("credit-transactions") resolves to the default isol8-dev- prefix,
which a moto table never created.
"""

import pytest


class _FakeTable:
    """Minimal DDB Table stub: yields preset scan pages in order."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.scan_calls = 0

    def scan(self, **kwargs):  # noqa: ARG002 - signature parity with boto3
        page = self._pages[self.scan_calls]
        self.scan_calls += 1
        return page


async def _passthrough_run_in_thread(func, *args, **kwargs):
    """Call the function directly instead of offloading to a thread."""
    return func(*args, **kwargs)


def _deduct_row(owner_id, tx_id, raw_microcents, created_at, *, row_type="deduct"):
    """A credit-transactions row as boto3 resource-level (un-marshalled) dict
    — i.e. plain Python values, matching what table.scan() returns when the
    DDB *resource* (not the low-level client) is used by get_table."""
    return {
        "user_id": owner_id,
        "tx_id": tx_id,
        "type": row_type,
        "raw_cost_microcents": raw_microcents,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_sums_only_deduct_rows_in_window():
    from unittest.mock import patch

    from scripts.reconcile_bedrock_vs_ledger import fetch_ledger_deduct_spend_usd

    items = [
        # In-window deducts (June 2026): 1_000_000 + 500_000 microcents = $1.50.
        _deduct_row("org_A", "deduct#r1", 1_000_000, "2026-06-03T10:00:00Z"),
        _deduct_row("org_B", "deduct#r2", 500_000, "2026-06-09T11:00:00Z"),
        # Out-of-window deduct (May) — excluded by the created_at prefix filter.
        _deduct_row("org_A", "deduct#r0", 9_000_000, "2026-05-31T23:00:00Z"),
        # Non-deduct row (a top-up) — excluded by the type filter.
        _deduct_row("org_A", "topup#t1", 9_000_000, "2026-06-05T10:00:00Z", row_type="top_up"),
    ]
    fake_table = _FakeTable(pages=[{"Items": items}])

    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        total = await fetch_ledger_deduct_spend_usd(["2026-06"])

    assert total == pytest.approx(1.50)


@pytest.mark.asyncio
async def test_empty_table_is_zero():
    from unittest.mock import patch

    from scripts.reconcile_bedrock_vs_ledger import fetch_ledger_deduct_spend_usd

    fake_table = _FakeTable(pages=[{"Items": []}])
    with (
        patch("core.dynamodb.get_table", return_value=fake_table),
        patch("core.dynamodb.run_in_thread", new=_passthrough_run_in_thread),
    ):
        assert await fetch_ledger_deduct_spend_usd(["2026-06"]) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_drift_monitor.py -k "sums_only_deduct or empty_table" -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_ledger_deduct_spend_usd' from 'scripts.reconcile_bedrock_vs_ledger'`. (If `tests/unit/scripts/` lacks an `__init__.py`, create an empty one first: `touch tests/unit/scripts/__init__.py`.)

- [ ] **Step 3: Add `fetch_ledger_deduct_spend_usd`**

In `apps/backend/scripts/reconcile_bedrock_vs_ledger.py`, immediately after `fetch_ledger_spend_usd` ends (line 372), add:

```python
async def fetch_ledger_deduct_spend_usd(periods: list[str]) -> float:
    """Sum the NEW credit-transactions deduct rows (RAW spend) → USD.

    Post-cutover the authoritative money is the ``credit-transactions``
    rows written by ``DdbLedgerBackend.deplete`` (``type == "deduct"``,
    ``raw_cost_microcents`` in microcents, ``created_at`` ISO8601). This is
    what the AWS invocation-log meter actually deducts — NOT the
    ``usage-counters.total_spend_microdollars`` analytics column that
    ``fetch_ledger_spend_usd`` reads. The drift monitor compares Cost
    Explorer against THIS number once the meter is the sole biller.

    Filters to rows whose ``created_at`` falls in one of ``periods``
    (``YYYY-MM`` prefixes). RAW (un-marked-up) cost, comparable to the AWS
    bill. microcents / 1e6 = USD.
    """
    from core.dynamodb import get_table, run_in_thread

    wanted = tuple(periods)
    total_microcents = 0

    table = get_table("credit-transactions")
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            if item.get("type") != "deduct":
                continue
            created_at = str(item.get("created_at", ""))
            if not created_at.startswith(wanted):
                continue
            total_microcents += int(item.get("raw_cost_microcents", 0))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return total_microcents / _MICROCENTS_PER_USD
```

`str.startswith` accepts a tuple of prefixes, so `created_at.startswith(("2026-06",))` matches any of the requested period months. `Any` is already imported at the top of the module (`from typing import Any`); confirm with `grep -n "from typing import" apps/backend/scripts/reconcile_bedrock_vs_ledger.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_drift_monitor.py -k "sums_only_deduct or empty_table" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/scripts/reconcile_bedrock_vs_ledger.py apps/backend/tests/unit/scripts/test_drift_monitor.py apps/backend/tests/unit/scripts/__init__.py
git commit -m "feat(billing): drift monitor reads credit-transactions deduct rows post-cutover"
```

---

### Task 8: Drift monitor daily runner + per-env CloudWatch metric (no account-global resource)

**Files:**
- Create: `apps/backend/scripts/drift_monitor.py`
- Test: `apps/backend/tests/unit/scripts/test_drift_monitor.py` (add the alert-threshold + runner tests)

**Design (read before coding):**
The "lightweight drift monitor" is a thin wrapper around the existing reconciler's pure dollar machinery (`DollarResult`, `format_dollar_report`) that, instead of the token side, compares Cost Explorer's Bedrock $ against `fetch_ledger_deduct_spend_usd` (Task 7), then emits a **per-env** CloudWatch metric (`Isol8/Billing` namespace, `BedrockLedgerDriftPct`) — NOT an account-global Cost Anomaly monitor (the per-env-resource lesson: every Bedrock cost alarm in `observability-stack.ts` is named `isol8-${envName}-…`, never account-global). The runner is invoked daily via a per-env schedule (the existing in-process scheduled-worker pattern, or an env-scoped EventBridge rule added later); this plan ships the script + the pure threshold predicate. The script reuses `fetch_bedrock_actual_cost_usd` and `_period_strings` from the reconciler, so there is no duplicated CE plumbing.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/scripts/test_drift_monitor.py`:

```python
def test_drift_exceeds_threshold_predicate():
    from scripts.drift_monitor import drift_alerted

    # 30% drift on a $100 AWS bill vs $70 ledger.
    assert drift_alerted(aws_usd=100.0, ledger_usd=70.0, threshold_pct=20.0) is True


def test_drift_within_threshold_predicate():
    from scripts.drift_monitor import drift_alerted

    # 5% drift is under the 20% threshold.
    assert drift_alerted(aws_usd=100.0, ledger_usd=95.0, threshold_pct=20.0) is False


def test_drift_zero_aws_zero_ledger_not_alerted():
    from scripts.drift_monitor import drift_alerted

    assert drift_alerted(aws_usd=0.0, ledger_usd=0.0, threshold_pct=20.0) is False


def test_drift_zero_aws_nonzero_ledger_alerted():
    from scripts.drift_monitor import drift_alerted

    # AWS reported $0 but the ledger deducted — a real discrepancy.
    assert drift_alerted(aws_usd=0.0, ledger_usd=5.0, threshold_pct=20.0) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/scripts/test_drift_monitor.py -k "drift_" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.drift_monitor'`.

- [ ] **Step 3: Write the drift monitor script**

Create `apps/backend/scripts/drift_monitor.py`:

```python
"""Daily Bedrock drift monitor — Cost Explorer $ vs the ledger's deduct rows.

The standing reconciliation reduced to its lightweight Phase-1 form: a daily
comparison of Cost Explorer's account-wide Bedrock cost against the SUM of the
NEW ``credit-transactions`` deduct rows (the meter's actual deductions, via
``fetch_ledger_deduct_spend_usd``). Reuses the reconciler's pure dollar
machinery (``DollarResult`` / ``format_dollar_report``) and CE plumbing
(``fetch_bedrock_actual_cost_usd`` / ``_period_strings``) — no duplicated
boto3.

On drift beyond ``--threshold-pct`` it emits a PER-ENVIRONMENT CloudWatch
metric (``Isol8/Billing`` / ``BedrockLedgerDriftPct``) and exits non-zero.
It deliberately does NOT create an AWS Cost Anomaly Detection monitor or any
other account-global resource — such a resource collides across the per-env
dev/prod stacks (the per-env-resource lesson; every Bedrock cost alarm in
``apps/infra/lib/stacks/observability-stack.ts`` — e.g. the ``W50``
bedrock-spend-anomaly alarm — is scoped per env via ``isol8-${envName}-`` in
its name, never account-global). A per-env CloudWatch alarm can be attached to
this metric without that collision.

Run via::

    cd apps/backend
    uv run python -m scripts.drift_monitor --env prod --days 2

READ-ONLY against the ledger + Cost Explorer; the only write is the
CloudWatch metric emit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

from scripts.reconcile_bedrock_vs_ledger import (
    DollarResult,
    fetch_bedrock_actual_cost_usd,
    fetch_ledger_deduct_spend_usd,
    format_dollar_report,
    _period_strings,
)


def drift_alerted(*, aws_usd: float, ledger_usd: float, threshold_pct: float) -> bool:
    """True when the AWS-vs-ledger divergence exceeds ``threshold_pct``.

    Uses the same divergence math as ``DollarResult.divergence_pct``: 0% when
    both are zero, 100% when AWS is zero but the ledger isn't.
    """
    return DollarResult(aws_usd=aws_usd, ledger_usd=ledger_usd).divergence_pct > threshold_pct


def _emit_drift_metric(env: str, divergence_pct: float) -> None:
    """Emit the per-env drift gauge to CloudWatch (no account-global resource)."""
    import boto3

    from core.config import settings

    cw = boto3.client("cloudwatch", region_name=getattr(settings, "AWS_REGION", "us-east-1"))
    cw.put_metric_data(
        Namespace="Isol8/Billing",
        MetricData=[
            {
                "MetricName": "BedrockLedgerDriftPct",
                "Dimensions": [{"Name": "env", "Value": env}],
                "Value": float(divergence_pct),
                "Unit": "Percent",
            }
        ],
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Daily Bedrock drift monitor: Cost Explorer Bedrock $ vs the "
            "credit-transactions deduct-row sum. Emits a per-env CloudWatch "
            "metric and exits non-zero on drift."
        ),
    )
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Window: last N days ending today UTC (default 2; CE lags ~24h).",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=20.0,
        help="Divergence percent that triggers an ALERT + non-zero exit (default 20).",
    )
    return parser


async def main(args: argparse.Namespace) -> int:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days)
    periods = _period_strings(start, end)

    from core.config import settings

    region = getattr(settings, "AWS_REGION", "us-east-1")

    aws_usd = await fetch_bedrock_actual_cost_usd(start=start, end=end, region=region)
    ledger_usd = await fetch_ledger_deduct_spend_usd(periods)
    result = DollarResult(aws_usd=aws_usd, ledger_usd=ledger_usd)

    report, alerted = format_dollar_report(result, threshold_pct=args.threshold_pct)
    print(f"[{args.env}] Bedrock drift monitor")
    print(f"  window:  {start.isoformat()} → {end.isoformat()} (end exclusive)")
    print(f"  periods: {', '.join(periods) if periods else '(none)'}")
    print(report)

    _emit_drift_metric(args.env, result.divergence_pct)

    if alerted:
        print(
            f"ALERT: Bedrock ledger drift {result.divergence_pct:.1f}% exceeds "
            f"{args.threshold_pct:.1f}% — the meter may be missing or "
            "over-counting deductions vs the AWS bill."
        )
        return 1
    print("OK: ledger deductions within threshold of the AWS bill.")
    return 0


if __name__ == "__main__":
    parsed = build_arg_parser().parse_args()
    sys.exit(asyncio.run(main(parsed)))
```

The `date` import is referenced via the type the reconciler functions expect; keep it imported for clarity even though `start`/`end` are produced inline.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/scripts/test_drift_monitor.py -k "drift_" -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the whole drift-monitor test module**

Run: `uv run pytest tests/unit/scripts/test_drift_monitor.py -v`
Expected: PASS (Task 7's fetch tests + Task 8's predicate tests, 6 passed)

- [ ] **Step 6: Confirm the script imports cleanly (no account-global resource, no syntax error)**

Run: `uv run python -c "import scripts.drift_monitor as d; assert hasattr(d, 'drift_alerted') and hasattr(d, 'main')"`
Expected: no output, exit 0. (Confirms the wrapper imports the reconciler symbols without executing any AWS call.)

- [ ] **Step 7: Commit**

```bash
git add apps/backend/scripts/drift_monitor.py apps/backend/tests/unit/scripts/test_drift_monitor.py
git commit -m "feat(billing): daily Bedrock drift monitor emitting a per-env CloudWatch metric"
```

---

### Task 9: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run every test module this plan touched or created**

Run: `uv run pytest tests/unit/billing tests/unit/gateway tests/unit/services tests/unit/containers tests/unit/scripts -v`
Expected: PASS — Plan 1's `tests/unit/billing` (untouched here), the migrated gate tests, the new provision-gate / enforcement / kill-switch / drift-monitor tests, and the existing ECS + provision-gate suites all green.

- [ ] **Step 2: Confirm the legacy deduct is fully gone from production code**

Run: `grep -rn --include="*.py" "_maybe_deduct_credits" apps/backend/core apps/backend/routers`
Expected: no output (zero hits). The method and its single call site are deleted.

- [ ] **Step 3: Confirm both balance gates read through the interface (no direct credit_ledger.get_balance on the gate paths)**

Run: `grep -n "credit_ledger.get_balance\|get_ledger().get_balance" apps/backend/core/gateway/connection_pool.py apps/backend/core/services/provision_gate.py`
Expected: two `get_ledger().get_balance` hits (one per file), and ZERO `credit_ledger.get_balance` hits in these two files.

- [ ] **Step 4: Lint**

Run: `uv run ruff check core/gateway/connection_pool.py core/services/provision_gate.py core/services/bedrock_enforcement.py core/containers/ecs_manager.py scripts/reconcile_bedrock_vs_ledger.py scripts/drift_monitor.py tests/unit/services/test_provision_gate_ledger.py tests/unit/services/test_bedrock_enforcement.py tests/unit/containers/test_ecs_manager_killswitch.py tests/unit/scripts/test_drift_monitor.py tests/unit/gateway/test_connection_pool_credits.py`
Expected: no errors. (If ruff flags `date` as unused in `drift_monitor.py`, remove it from the import line — it is illustrative only.)

- [ ] **Step 5: Commit any lint fixes**

```bash
git add -A
git commit -m "chore(billing): lint fixes for the enforcement-loop cutover"
```

(If Step 4 produced no changes, skip this commit.)

---

## Self-Review

**1. Spec coverage (this plan covers the §3.2.3 enforcement loop + §3.5 single-deduct cutover + §3.7 lightweight drift monitor):**
- §3.2.3 "pre-chat gate reads balance and blocks at ≤ 0, behind the ledger interface" → Task 1 (`gate_chat` → `get_ledger().get_balance(consistent=True)`). ✅
- §3.2.3 "provision gate behind the same interface so chat + provision read one source" → Task 2 (`evaluate_provision_gate` → `get_ledger().get_balance`). ✅ (Landmine #5/#8: both cut over together so a top-up/drain is consistent across gates.)
- §3.2.3 "on a deplete crossing ≤ 0, fire the kill-switch (IAM revoke + ECS desired=0)" → Task 3 (`enforce_after_deplete`) + Task 4 (`kill_owner_bedrock`). ✅ (`enforce_after_deplete` is status-agnostic — it gates only on `applied` + `balance ≤ 0`, never on `run_status` — so it fires for error/aborted runs too, IF Plan 3's parse captured them. The short-circuit on `is_owner_service_stopped` prevents repeated kills during the IAM-propagation window.)
- §3.2.1 / §3.6 fail-closed AIP-ARN boundary (Plan 2): **dependency, not owned here.** The kill-switch is only correct if the depleted owner is the one who actually spent — i.e. Plan 2's per-owner AIP ARNs are the container `openclaw.json` model ids and Plan 2's §3.6 live check (`modelId`==AIP id, `identity.arn`==per-owner role) has passed. Stated explicitly in the "Correctness gated on Plan 2 + Plan 3 live checks" note; the `METER_IS_LIVE` flip must not be enabled until that check is green. ⚠️ (gated)
- §3.2.2 "bill on error/aborted too" → Plan 4 deletes the legacy deduct (which NEVER billed on error). The runaway this whole effort targets — the failing Friday heartbeat firing `phase=="error"` ~every 22 min, unbilled — is only billed (and only triggers the kill) once Plan 3's `run_status` field-path parse captures error/aborted records. **This is wholly a Plan-3 dependency**; stated explicitly in the context section so it is not silently assumed. ⚠️ (gated on Plan 3)
- §3.5 "retire the legacy biller so the meter is the sole deduct path; shadow then delete" → Task 5 (shadow log-only) + Task 6 (add `METER_IS_LIVE` flag via the Step 0a–0e TDD micro-cycle, gate then delete the legacy call + method). ✅ (Shadow is log-only; the legacy deduct and Plan 3's `deplete()` are gated on ONE shared `METER_IS_LIVE` flag — OWNED here by Plan 4, CONSUMED by Plan 3 (shadow-only/log-no-deplete when `False`, live `deplete()` + `enforce_after_deplete` when `True`) — so exactly one biller is live at every point regardless of PR merge order, NOT a co-deploy race. The flag default `False` keeps the legacy path live until the cutover env-var flip; the two write different `tx_id` namespaces and do NOT dedup against each other, which is precisely why the flag, not "ship together", is the safety mechanism.)
- §3.7 "a lightweight daily drift monitor building on the reconcile script, NOT account-global" → Task 7 (`fetch_ledger_deduct_spend_usd` over `credit-transactions`) + Task 8 (`drift_monitor.py`, per-env CloudWatch metric). ✅ (Per-env-resource lesson: no Cost Anomaly monitor, no account-scoped resource — the metric and any alarm on it are scoped per env, matching the `isol8-${envName}-…` naming of every Bedrock cost alarm in `observability-stack.ts`.)
- **CONSUMED, not redefined (Plan 1):** `get_ledger`, `LedgerBackend.get_balance(consistent=…)`, `DepleteResult(balance_microcents, applied)`. **CONSUMED (Plan 2):** `revoke_owner_bedrock` / `restore_owner_bedrock` (the primitives in `core.billing.bedrock_aip` — lazy-imported in `kill_owner_bedrock`; this plan does NOT redefine them, and they are NOT in this plan's own `core/services/bedrock_enforcement.py`). **DEFINED here (Plan 4):** `enforce_after_deplete(owner_id, result)` + `EcsManager.kill_owner_bedrock` in `core/services/bedrock_enforcement.py` / `core/containers/ecs_manager.py`. **DEFINED for Plan 3:** `enforce_after_deplete(owner_id, result)`.
- **What stays (correct, per the deprecation inventory):** `record_usage` analytics (Task 6 keeps it), `_record_usage_from_session`/`_fetch_and_record_usage` skeleton + `self._billing_tasks` await on `close()` (harmless), `bedrock_pricing.cost_microcents` (the meter uses it), the existing token-side reconciler (untouched). `credit_ledger.deduct`/`get_balance` become dead production code — noted in an inline comment, deletion deferred to Phase 3.
- **Not in this plan (correct — other plans/phase):** the AWS Lambda + invocation logging + the meter endpoint (Plan 3), the AIP/IAM role + the `revoke_owner_bedrock`/`restore_owner_bedrock` primitives + the AIP resolvers in `core/billing/bedrock_aip.py` (Plan 2), the Metronome adapter + dropping the DDB tables + deleting `credit_ledger.py` (Phase 3).
- **Not in this plan (Stripe top-up funding):** Stripe top-up Checkout (spec §3.2.5) is out of Phase-1 scope — the DDB ledger's existing `top_up`/admin-grant funds balances in the interim; Metronome owns payment-gated funding in Phase 3.

**2. Placeholder scan:** No TBD/TODO/FIXME/"implement later"/"handle edge cases"/"similar to Task N". Every code block is complete; every command is a real, runnable command with an explicit Expected line.

**3. Type consistency:**
- `DepleteResult(balance_microcents: int, applied: bool)` is used identically in `enforce_after_deplete` (reads `.applied` + `.balance_microcents`), in the Task 3 tests, and matches Plan 1's `NamedTuple`. ✅
- `enforce_after_deplete(owner_id: str, result: DepleteResult) -> None` is the exact signature Plan 3 calls. ✅
- `kill_owner_bedrock(self, owner_id: str) -> None` on `EcsManager`; `revoke_owner_bedrock(owner_id)` (the Plan-2 primitive in `core.billing.bedrock_aip`, lazy-imported in `kill_owner_bedrock`) is awaited with the owner_id positional — matches the Task 4 test patch `core.billing.bedrock_aip.revoke_owner_bedrock`. ✅
- `enforce_after_deplete` imports `get_ecs_manager` **lazily in-function** (`from core.containers import get_ecs_manager`) to avoid eagerly pulling the container-orchestration import tree (`core.containers.__init__:13` imports `connection_pool`) into `bedrock_enforcement` at module load; the Task 3 tests therefore patch `core.containers.get_ecs_manager` (the source module, where the in-function lookup resolves), NOT `core.services.bedrock_enforcement.get_ecs_manager`. ✅
- `is_owner_service_stopped(self, owner_id: str) -> bool` on `EcsManager` reads the container DDB row via the module-level `container_repo` (`from core.repositories import container_repo` at `ecs_manager.py:32`); returns `True` for status `"stopped"` or a missing row. `enforce_after_deplete` calls it to short-circuit redundant kills. ✅
- `get_ledger().get_balance(owner_id, consistent=True)` (chat) and `get_ledger().get_balance(owner_id)` (provision) match Plan 1's `get_balance(owner_id, *, consistent=False) -> int`. ✅
- `fetch_ledger_deduct_spend_usd(periods: list[str]) -> float` mirrors the existing `fetch_ledger_spend_usd(periods)` shape; `_MICROCENTS_PER_USD` (1_000_000) is reused, returning USD. ✅
- The shadow metric carries ONLY the low-cardinality `model` dimension (string); the unbounded-cardinality `raw`/`marked_up` amounts are in the structured log line, not as CloudWatch dimensions (per `core/observability/metrics.py:put_metric` — high-cardinality values must be log fields). The markup `raw * 14 // 10` matches both the legacy formula (`connection_pool.py:1216`) and the meter's required markup (`raw * 14 // 10`), so the shadow diff is integer-exact, not float-drifted. ✅
- The `credit-transactions` audit row fields the drift monitor reads (`type=="deduct"`, `raw_cost_microcents`, `created_at`) match exactly what Plan 1's `DdbLedgerBackend.deplete` writes. ✅

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-enforcement-loop-cutover.md`. Deploy AFTER Plan 2 (AIP/IAM). The legacy-deduct retirement (Task 6) and Plan 3's meter `deplete()` are both gated on ONE shared `METER_IS_LIVE` setting (default `False`), so the two PRs are order-independent — there is no co-deploy race. The actual cutover is a single env-var flip `METER_IS_LIVE=true`, performed ONLY after both PRs are deployed AND Plan 2's §3.6 AIP-ARN attribution check + Plan 3's `run_status`/error-aborted parse are verified live (see the "Correctness gated on Plan 2 + Plan 3 live checks" note — the kill-switch and error-run billing correctness depend on those). Recommended order: Task 1 → 2 (gates, independently shippable) → 3 → 4 (kill-switch, needs Plan 2's `revoke_owner_bedrock`) → 5 (shadow, ship early to gather diff data) → 6 (flag + gate + delete) → 7 → 8 (drift monitor, independent) → 9 (verify). Commit each task.
