# Ledger Interface + DDB Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the swappable ledger seam — a normalized `UsageEvent`, a thin `LedgerBackend` interface, and a `DdbLedgerBackend` whose `deplete()` is exactly-once on the Bedrock `requestId` — so the new every-run AWS meter (Plan 3) can bill the existing DDB credit ledger without double-charging, and the backend can later be swapped to Metronome (Phase 3) without touching callers.

**Architecture:** A frozen `UsageEvent` dataclass is the contract between the meter and the ledger. A `LedgerBackend` `Protocol` (`deplete(event)` / `get_balance(owner_id)`) plus a `get_ledger()` factory is the swap seam. `DdbLedgerBackend.deplete()` applies the balance decrement and the audit-row write **atomically in one `TransactWriteItems`**, where the audit row's sort key is `deduct#{request_id}` guarded by `attribute_not_exists(tx_id)` — so a replay of the same invocation cancels the whole transaction and deducts nothing. The balance is allowed to go negative (the meter is post-hoc; the kill-switch in Plan 4 prevents future spend), which also sidesteps the legacy `deduct()` lossy overdraft fallback.

**Tech Stack:** Python 3.12, `boto3` (DynamoDB resource + low-level client for `transact_write_items`), `pytest` + `pytest-asyncio` + `moto[dynamodb]>=5.1.22` for tests. No CDK / infra change in this plan — it reuses the existing `credits` and `credit-transactions` tables; idempotency is a strongly-consistent conditional write, so no GSI is needed.

---

## Context for the implementer (read before Task 1)

This is **Plan 1 of 4** for Phase 1 of the Bedrock metering remediation (spec: `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`). The other three plans are not yours to touch:
- **Plan 2** mints per-owner Bedrock application inference profiles (AIPs) + scoped IAM roles (the enforcement boundary + kill-switch).
- **Plan 3** is the AWS invocation-log → Lambda meter that *produces* a `UsageEvent` and calls `deplete()`. **This plan defines the `UsageEvent` and `deplete()` it will use.**
- **Plan 4** wires the enforcement loop and retires the legacy `connection_pool` deduct so there is exactly one deduct path.

**Why a new `deplete()` instead of reusing `credit_ledger.deduct()`:** the legacy `deduct()` (`apps/backend/core/services/credit_ledger.py:143`) accepts a `bedrock_invocation_id` argument but **no caller passes it** and there is **no dedup guard** — replaying the same invocation deducts twice. It also has a lossy "force balance toward zero" overdraft fallback. Rather than retrofit money-correctness onto a function the connection_pool path still uses, this plan adds a clean, exactly-once `deplete()` on the new interface. The legacy `deduct()` is left untouched until Plan 4 retires its single caller.

**Key existing facts (verified in code):**
- Ledger tables: `credits` (PK `user_id`, holds `balance_microcents` as a Number) and `credit-transactions` (PK `user_id`, SK `tx_id`). The DDB attribute is named `user_id` but the **value is the owner_id** (org_id for orgs, user_id for personal). Table names come from env `CREDITS_TABLE` / `CREDIT_TRANSACTIONS_TABLE` (falling back to `settings`).
- Money unit is **microcents (int)** throughout. 1 cent = 10,000 microcents.
- Markup is applied by the **caller** as integer math `raw * 14 // 10` (`BILLING_MARKUP=1.4`); see `connection_pool.py:1213`. In this plan the *meter* owns pricing, so the `UsageEvent` already carries the final `billed_microcents` — the ledger just deducts it and records `raw_cost_microcents` + `markup_multiplier` for audit.
- Tests mock DDB with `moto`'s `mock_aws()`, create tables via `boto3.client("dynamodb")`, and `monkeypatch.setenv` the table-name env vars. See `apps/backend/tests/unit/services/test_credit_ledger.py` for the exact fixture shape — **reuse that fixture pattern.**

**Run all commands from `apps/backend/` unless noted. Use `uv run` for Python.**

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/core/billing/usage_event.py` (Create) | The normalized `UsageEvent` frozen dataclass — the meter↔ledger contract. |
| `apps/backend/core/billing/ledger_interface.py` (Create) | `DepleteResult`, the `LedgerBackend` `Protocol`, and the `get_ledger()` factory (the swap seam). |
| `apps/backend/core/billing/ddb_ledger_backend.py` (Create) | `DdbLedgerBackend` — implements `LedgerBackend` against the existing DDB tables; exactly-once `deplete()` via `TransactWriteItems`. |
| `apps/backend/core/config.py` (Modify, near line 101) | Add `LEDGER_BACKEND: str = "ddb"` setting (selects the backend). |
| `apps/backend/tests/unit/billing/test_usage_event.py` (Create) | Tests for `UsageEvent`. |
| `apps/backend/tests/unit/billing/test_ledger_interface.py` (Create) | Tests for `DepleteResult` + `get_ledger()`. |
| `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py` (Create) | Tests for `DdbLedgerBackend` (moto). |

---

### Task 1: `UsageEvent` normalized event

**Files:**
- Create: `apps/backend/core/billing/usage_event.py`
- Test: `apps/backend/tests/unit/billing/test_usage_event.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_usage_event.py`:

```python
"""Unit tests for the normalized UsageEvent (meter -> ledger contract)."""

import dataclasses

import pytest

from core.billing.usage_event import UsageEvent


def _make_event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-123",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_500,
        billed_microcents=14_700,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


def test_usage_event_holds_all_fields():
    e = _make_event()
    assert e.owner_id == "org_abc"
    assert e.request_id == "req-123"
    assert e.billed_microcents == 14_700
    assert e.raw_cost_microcents == 10_500
    assert e.run_status == "end"


def test_usage_event_is_frozen():
    e = _make_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.billed_microcents = 0  # type: ignore[misc]


def test_usage_event_rejects_blank_request_id():
    with pytest.raises(ValueError, match="request_id"):
        _make_event(request_id="")


def test_usage_event_rejects_blank_owner_id():
    with pytest.raises(ValueError, match="owner_id"):
        _make_event(owner_id="")


def test_usage_event_rejects_negative_billed():
    with pytest.raises(ValueError, match="billed_microcents"):
        _make_event(billed_microcents=-1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_usage_event.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.usage_event'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/usage_event.py`:

```python
"""Normalized usage event — the contract between the AWS meter and the ledger.

The meter (Plan 3) parses a Bedrock invocation-log record, prices it, and emits
one of these. The ledger (`LedgerBackend.deplete`) consumes it. Keeping this a
single frozen dataclass means the meter and ledger evolve against one explicit
shape, and a future Metronome adapter consumes the identical event.

Money is in microcents (int): 1 cent = 10_000 microcents. `billed_microcents`
is the marked-up amount to deduct; `raw_cost_microcents` + `markup_multiplier`
are carried for audit only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageEvent:
    owner_id: str
    """Billing owner (org_id for orgs, user_id for personal). AWS-attributed."""

    request_id: str
    """Bedrock invocation requestId — the exactly-once idempotency key."""

    model_id: str
    """The AIP ARN (or model id) the run invoked, from the log `modelId`."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    raw_cost_microcents: int
    """Un-marked-up cost priced from token counts. Audit only."""

    billed_microcents: int
    """Marked-up amount to deduct from the balance."""

    markup_multiplier: float
    """The markup applied (e.g. 1.4). Audit only."""

    region: str
    """Source region of the invocation (logs are per fan-out region)."""

    run_status: str
    """`end`, `error`, or `aborted` — we bill all three."""

    occurred_at: str
    """ISO8601 timestamp from the invocation log record."""

    source: str
    """Provenance, e.g. `bedrock_invocation_log`."""

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("owner_id must be non-empty")
        if not self.request_id:
            raise ValueError("request_id must be non-empty")
        if self.billed_microcents < 0:
            raise ValueError("billed_microcents must be >= 0")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_usage_event.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/usage_event.py apps/backend/tests/unit/billing/test_usage_event.py
git commit -m "feat(billing): add normalized UsageEvent for the meter->ledger contract"
```

---

### Task 2: `LedgerBackend` interface + `DepleteResult` + `get_ledger()` factory

**Files:**
- Create: `apps/backend/core/billing/ledger_interface.py`
- Modify: `apps/backend/core/config.py` (add `LEDGER_BACKEND` near line 101)
- Test: `apps/backend/tests/unit/billing/test_ledger_interface.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_ledger_interface.py`:

```python
"""Unit tests for the ledger interface: DepleteResult + get_ledger() factory."""

import pytest

from core.billing.ddb_ledger_backend import DdbLedgerBackend
from core.billing.ledger_interface import DepleteResult, get_ledger


def test_deplete_result_fields():
    r = DepleteResult(balance_microcents=500, applied=True)
    assert r.balance_microcents == 500
    assert r.applied is True


def test_get_ledger_returns_ddb_backend_by_default(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    ledger = get_ledger()
    assert isinstance(ledger, DdbLedgerBackend)


def test_get_ledger_honours_ddb_setting(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "ddb")
    assert isinstance(get_ledger(), DdbLedgerBackend)


def test_get_ledger_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    with pytest.raises(ValueError, match="metronome"):
        get_ledger()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.ledger_interface'` (and `ddb_ledger_backend`)

- [ ] **Step 3: Add the `LEDGER_BACKEND` setting**

In `apps/backend/core/config.py`, immediately after the `CREDIT_TRANSACTIONS_TABLE` line (currently line 102), add:

```python
    LEDGER_BACKEND: str = "ddb"  # ledger system-of-record: "ddb" (interim) | "metronome" (Phase 3)
```

- [ ] **Step 4: Write minimal implementation**

Create `apps/backend/core/billing/ledger_interface.py`:

```python
"""Thin ledger interface — the swap seam between the meter and the money store.

`deplete(event)` applies one usage event to the owner's balance, exactly once.
`get_balance(owner_id)` reads it. The concrete backend is chosen by the
`LEDGER_BACKEND` setting: "ddb" (the interim hand-rolled ledger) now, and a
Metronome adapter in Phase 3 — callers depend only on this interface, never on
the backend, so the cutover is a one-line config flip with no caller changes.
"""

from __future__ import annotations

import os
from typing import NamedTuple, Protocol, runtime_checkable

from core.billing.usage_event import UsageEvent
from core.config import settings


class DepleteResult(NamedTuple):
    balance_microcents: int
    """Owner balance after this event (may be negative — the meter is post-hoc)."""

    applied: bool
    """True if this event was newly applied; False if it was a deduplicated replay."""


@runtime_checkable
class LedgerBackend(Protocol):
    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Apply one usage event to the owner's balance, exactly once on
        event.request_id. Idempotent: replaying the same request_id is a
        no-op that returns the current balance with applied=False."""
        ...

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        """Owner balance in microcents (0 if no row). consistent=True forces a
        strongly-consistent read for hot-path gating."""
        ...


def get_ledger() -> LedgerBackend:
    """Return the configured ledger backend. The swap seam for Phase 3."""
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "ddb":
        from core.billing.ddb_ledger_backend import DdbLedgerBackend

        return DdbLedgerBackend()
    raise ValueError(
        f"Unknown LEDGER_BACKEND {backend!r} (expected 'ddb'; 'metronome' lands in Phase 3)"
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: PASS (4 passed). (`DdbLedgerBackend` exists after Task 3/4; if running this task in isolation before Task 3, the import fails — implement Task 3 first or run the two tasks together. The recommended order is 1 → 2 → 3 → 4, committing each.)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/billing/ledger_interface.py apps/backend/core/config.py apps/backend/tests/unit/billing/test_ledger_interface.py
git commit -m "feat(billing): add LedgerBackend interface + get_ledger() swap seam"
```

---

### Task 3: `DdbLedgerBackend.get_balance` (delegating read)

**Files:**
- Create: `apps/backend/core/billing/ddb_ledger_backend.py`
- Test: `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py`:

```python
"""Unit tests for DdbLedgerBackend (moto-mocked DynamoDB)."""

import boto3
import pytest
from moto import mock_aws

from core.billing.ddb_ledger_backend import DdbLedgerBackend
from core.billing.usage_event import UsageEvent


@pytest.fixture
def ledger_tables(monkeypatch):
    """Provision moto-mocked credits + credit-transactions tables (mirrors
    tests/unit/services/test_credit_ledger.py)."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        yield client


def _seed_balance(client, owner_id: str, microcents: int) -> None:
    client.put_item(
        TableName="test-credits",
        Item={"user_id": {"S": owner_id}, "balance_microcents": {"N": str(microcents)}},
    )


def _event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-1",
        model_id="aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_000,
        billed_microcents=14_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_zero_for_new_owner(self, ledger_tables):
        assert await DdbLedgerBackend().get_balance("org_none") == 0

    @pytest.mark.asyncio
    async def test_reads_seeded_balance(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 50_000)
        assert await DdbLedgerBackend().get_balance("org_abc") == 50_000

    @pytest.mark.asyncio
    async def test_consistent_read_param_accepted(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 7)
        assert await DdbLedgerBackend().get_balance("org_abc", consistent=True) == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_ddb_ledger_backend.py::TestGetBalance -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.ddb_ledger_backend'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/ddb_ledger_backend.py`:

```python
"""DynamoDB-backed ledger (interim, Phase 1).

Implements `LedgerBackend` against the existing `credits` (atomic balance) and
`credit-transactions` (immutable audit) tables. `deplete()` is exactly-once on
`event.request_id` via a single TransactWriteItems (see Task 4). This backend
is destined for deletion once the Metronome adapter is live (Phase 3); it
exists only so the new every-run meter has a money store to bill in the interim.
"""

from __future__ import annotations

import os

import boto3

from core.billing.ledger_interface import DepleteResult
from core.billing.usage_event import UsageEvent
from core.config import settings


def _credits_table_name() -> str:
    name = os.environ.get("CREDITS_TABLE") or settings.CREDITS_TABLE
    if not name:
        raise RuntimeError("CREDITS_TABLE is empty — backend is misconfigured.")
    return name


def _txns_table_name() -> str:
    name = os.environ.get("CREDIT_TRANSACTIONS_TABLE") or settings.CREDIT_TRANSACTIONS_TABLE
    if not name:
        raise RuntimeError("CREDIT_TRANSACTIONS_TABLE is empty — backend is misconfigured.")
    return name


class DdbLedgerBackend:
    """LedgerBackend over DynamoDB. See module docstring."""

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(
            _credits_table_name()
        )
        resp = table.get_item(Key={"user_id": owner_id}, ConsistentRead=consistent)
        item = resp.get("Item")
        if not item:
            return 0
        return int(item.get("balance_microcents", 0))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_ddb_ledger_backend.py::TestGetBalance -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/ddb_ledger_backend.py apps/backend/tests/unit/billing/test_ddb_ledger_backend.py
git commit -m "feat(billing): add DdbLedgerBackend.get_balance"
```

---

### Task 4: `DdbLedgerBackend.deplete` — exactly-once via TransactWriteItems

**Files:**
- Modify: `apps/backend/core/billing/ddb_ledger_backend.py`
- Test: `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py` (add `TestDeplete`)

**Design (read before coding):**
`deplete(event)` issues ONE `transact_write_items` with two items:
1. **Update** the `credits` row: `ADD balance_microcents :neg SET updated_at = :now` — **no `balance >= amount` condition**. The balance may go negative; that is correct for a post-hoc meter (we already incurred the AWS cost), and Plan 4's kill-switch is what stops future spend.
2. **Put** an audit row into `credit-transactions` with `tx_id = "deduct#" + event.request_id` and `ConditionExpression = "attribute_not_exists(tx_id)"`.

If the Put's condition fails (the request_id was already applied), DynamoDB **cancels the entire transaction** — the balance is NOT decremented. boto3 raises `ClientError` with `Code == "TransactionCanceledException"`; the per-item reasons are in `e.response["CancellationReasons"]`. If reason[1] (the Put) is `ConditionalCheckFailed`, treat it as a deduplicated replay → return the current balance with `applied=False`.

The audit row deliberately omits `balance_after_microcents` (the transaction can't know the post-balance before it commits, and concurrent deplete()s make a pre-read stale). The `credits` table is the balance source of truth; the audit log records deltas. `DepleteResult.balance_microcents` comes from a consistent read *after* the transaction commits.

`transact_write_items` is a **low-level client** call needing typed AttributeValues (`{"N": "..."}`, `{"S": "..."}`); use `boto3.client("dynamodb")` and marshal explicitly (numbers as strings).

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py`:

```python
class TestDeplete:
    @pytest.mark.asyncio
    async def test_deplete_applies_once(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        result = await DdbLedgerBackend().deplete(_event(billed_microcents=14_000))
        assert result.applied is True
        assert result.balance_microcents == 86_000
        assert await DdbLedgerBackend().get_balance("org_abc", consistent=True) == 86_000

    @pytest.mark.asyncio
    async def test_deplete_writes_audit_row_keyed_on_request_id(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        await DdbLedgerBackend().deplete(_event(request_id="req-XYZ"))
        resp = ledger_tables.get_item(
            TableName="test-credit-txns",
            Key={"user_id": {"S": "org_abc"}, "tx_id": {"S": "deduct#req-XYZ"}},
        )
        item = resp["Item"]
        assert item["type"]["S"] == "deduct"
        assert item["amount_microcents"]["N"] == "-14000"
        assert item["raw_cost_microcents"]["N"] == "10000"
        assert item["bedrock_invocation_id"]["S"] == "req-XYZ"
        assert item["model_id"]["S"] == "aip-xyz"
        assert item["input_tokens"]["N"] == "1000"
        assert item["cache_read_tokens"]["N"] == "2000"

    @pytest.mark.asyncio
    async def test_deplete_is_idempotent_on_replay(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 100_000)
        backend = DdbLedgerBackend()
        first = await backend.deplete(_event(request_id="req-dup", billed_microcents=14_000))
        second = await backend.deplete(_event(request_id="req-dup", billed_microcents=14_000))
        assert first.applied is True
        assert second.applied is False
        # Deducted exactly once.
        assert second.balance_microcents == 86_000
        assert await backend.get_balance("org_abc", consistent=True) == 86_000

    @pytest.mark.asyncio
    async def test_deplete_allows_negative_balance(self, ledger_tables):
        _seed_balance(ledger_tables, "org_abc", 5_000)
        result = await DdbLedgerBackend().deplete(_event(billed_microcents=14_000))
        assert result.applied is True
        assert result.balance_microcents == -9_000

    @pytest.mark.asyncio
    async def test_deplete_zero_billed_still_writes_dedup_row(self, ledger_tables):
        # A genuinely free run (billed 0) still records its request_id so a
        # replay is a no-op and the audit log is complete.
        _seed_balance(ledger_tables, "org_abc", 100_000)
        backend = DdbLedgerBackend()
        r1 = await backend.deplete(_event(request_id="req-free", billed_microcents=0))
        r2 = await backend.deplete(_event(request_id="req-free", billed_microcents=0))
        assert r1.applied is True
        assert r2.applied is False
        assert await backend.get_balance("org_abc", consistent=True) == 100_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_ddb_ledger_backend.py::TestDeplete -v`
Expected: FAIL — `AttributeError: 'DdbLedgerBackend' object has no attribute 'deplete'`

- [ ] **Step 3: Write minimal implementation**

Add the imports at the top of `apps/backend/core/billing/ddb_ledger_backend.py` (alongside the existing `import boto3`):

```python
import time
from botocore.exceptions import ClientError
```

Add these helpers above the `DdbLedgerBackend` class:

```python
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ddb_client():
    return boto3.client("dynamodb", region_name=settings.AWS_REGION)
```

Add the `deplete` method to `DdbLedgerBackend` (below `get_balance`):

```python
    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Apply one usage event to the owner's balance, exactly once on
        event.request_id. Balance is allowed to go negative (post-hoc meter)."""
        now = _now_iso()
        tx_id = f"deduct#{event.request_id}"
        client = _ddb_client()
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": _credits_table_name(),
                            "Key": {"user_id": {"S": event.owner_id}},
                            "UpdateExpression": "ADD balance_microcents :neg SET updated_at = :now",
                            "ExpressionAttributeValues": {
                                ":neg": {"N": str(-event.billed_microcents)},
                                ":now": {"S": now},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": _txns_table_name(),
                            "Item": {
                                "user_id": {"S": event.owner_id},
                                "tx_id": {"S": tx_id},
                                "type": {"S": "deduct"},
                                "amount_microcents": {"N": str(-event.billed_microcents)},
                                "raw_cost_microcents": {"N": str(event.raw_cost_microcents)},
                                "markup_multiplier": {"N": str(event.markup_multiplier)},
                                "bedrock_invocation_id": {"S": event.request_id},
                                "model_id": {"S": event.model_id},
                                "input_tokens": {"N": str(event.input_tokens)},
                                "output_tokens": {"N": str(event.output_tokens)},
                                "cache_read_tokens": {"N": str(event.cache_read_tokens)},
                                "cache_write_tokens": {"N": str(event.cache_write_tokens)},
                                "region": {"S": event.region},
                                "run_status": {"S": event.run_status},
                                "source": {"S": event.source},
                                "occurred_at": {"S": event.occurred_at},
                                "created_at": {"S": now},
                            },
                            "ConditionExpression": "attribute_not_exists(tx_id)",
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons", [])
            # Index 1 is the Put (dedup) item. A conditional failure there means
            # this request_id was already applied — a replay. Anything else is
            # a real error we must not swallow.
            put_reason = reasons[1]["Code"] if len(reasons) > 1 else ""
            if put_reason != "ConditionalCheckFailed":
                raise
            current = await self.get_balance(event.owner_id, consistent=True)
            return DepleteResult(balance_microcents=current, applied=False)

        new_balance = await self.get_balance(event.owner_id, consistent=True)
        return DepleteResult(balance_microcents=new_balance, applied=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_ddb_ledger_backend.py -v`
Expected: PASS (all `TestGetBalance` + `TestDeplete` tests, 8 passed)

- [ ] **Step 5: Run the full billing test module + the interface test**

Run: `uv run pytest tests/unit/billing/ -v`
Expected: PASS (usage_event + ledger_interface + ddb_ledger_backend all green)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/billing/ddb_ledger_backend.py apps/backend/tests/unit/billing/test_ddb_ledger_backend.py
git commit -m "feat(billing): exactly-once DdbLedgerBackend.deplete via TransactWriteItems"
```

---

### Task 5: End-to-end seam test + final verification

**Files:**
- Test: `apps/backend/tests/unit/billing/test_ledger_interface.py` (add an integration test through the factory)

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_ledger_interface.py`:

```python
import boto3
from moto import mock_aws

from core.billing.usage_event import UsageEvent


@pytest.fixture
def ledger_tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="test-credits",
            Item={"user_id": {"S": "org_seam"}, "balance_microcents": {"N": "50000"}},
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        monkeypatch.setenv("LEDGER_BACKEND", "ddb")
        yield client


@pytest.mark.asyncio
async def test_get_ledger_deplete_then_balance_through_interface(ledger_tables):
    ledger = get_ledger()
    event = UsageEvent(
        owner_id="org_seam",
        request_id="req-seam",
        model_id="aip-1",
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        raw_cost_microcents=5_000,
        billed_microcents=7_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-07T12:00:00Z",
        source="bedrock_invocation_log",
    )
    result = await ledger.deplete(event)
    assert result.applied is True
    assert result.balance_microcents == 43_000
    assert await ledger.get_balance("org_seam", consistent=True) == 43_000
```

- [ ] **Step 2: Run test to verify it fails (or passes if seam already correct)**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py::test_get_ledger_deplete_then_balance_through_interface -v`
Expected: PASS (the factory returns `DdbLedgerBackend`, which already implements both methods). If it FAILS with an import or attribute error, the factory wiring in Task 2 is wrong — fix it.

- [ ] **Step 3: Run the full backend unit suite to confirm no regressions**

Run: `uv run pytest tests/unit/billing/ tests/unit/services/test_credit_ledger.py -v`
Expected: PASS — the new billing module is green AND the legacy `test_credit_ledger.py` still passes (this plan did not touch `credit_ledger.py`).

- [ ] **Step 4: Run lint**

Run: `uv run ruff check core/billing/ tests/unit/billing/`
Expected: no errors. (If ruff flags the `runtime_checkable`/`Protocol` import as unused, ensure both are actually referenced; they are.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/tests/unit/billing/test_ledger_interface.py
git commit -m "test(billing): end-to-end ledger seam test (factory -> deplete -> balance)"
```

---

## Self-Review

**1. Spec coverage (this plan covers the §3.2.3 "ledger interface" foundation + the §3.5 interim-DDB `requestId` idempotency caveat):**
- §3.2.3 "the ledger sits behind a thin interface (`deplete(event)` / `get_balance(owner)`)" → Task 2 (`LedgerBackend`), Task 3 + 4 (`DdbLedgerBackend`). ✅
- §3.2 "the meter emits a normalized usage event" (contract half) → Task 1 (`UsageEvent`). ✅
- §3.5 "the new every-run meter will double-deplete the DDB ledger unless its `requestId` idempotency hole is closed first" → Task 4 (exactly-once `deplete` on `request_id`). ✅
- §3.1 "the backend is swappable" → Task 2 (`get_ledger()` factory + `LEDGER_BACKEND` setting). ✅
- §3.2.2 "bill on error/aborted too" → `UsageEvent.run_status` carries the status; `deplete` bills regardless of status (no status gate). ✅
- §3.8 testing: idempotency replay safety (Task 4), allow-negative (Task 4). ✅
- **Not in this plan (correct — they belong to later plans):** the actual meter/Lambda (Plan 3), AIP/IAM attribution (Plan 2), the kill-switch + retiring `connection_pool.deduct` (Plan 4), the Metronome adapter (Phase 2/3). The other legacy DDB holes (lossy `deduct` overdraft, non-atomic `adjustment`, admin-grant bug) are intentionally left per spec §3.5 ("can be left if the interim is short").

**2. Placeholder scan:** No TBD/TODO. All code blocks are complete and directly usable.

**3. Type consistency:** `UsageEvent` field names (`owner_id`, `request_id`, `billed_microcents`, `raw_cost_microcents`, `markup_multiplier`, `model_id`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `region`, `run_status`, `occurred_at`, `source`) are used identically in `ddb_ledger_backend.deplete`'s marshalling and in every test factory. `DepleteResult(balance_microcents, applied)` is consistent across the interface, the backend return, and all assertions. `get_ledger()` / `LedgerBackend` / `DdbLedgerBackend` names match across files. The audit-row attribute `bedrock_invocation_id` matches the legacy ledger's attribute name (so existing reconcile/reporting that reads it keeps working).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-ledger-interface-ddb-idempotency.md`. Recommended order is Task 1 → 2 → 3 → 4 → 5, committing each (Task 2's interface test imports `DdbLedgerBackend`, so build Tasks 3–4 in the same sitting or reorder 3 before 2's final test run).
