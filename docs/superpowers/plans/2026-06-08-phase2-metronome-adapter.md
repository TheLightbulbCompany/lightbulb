# Metronome Adapter (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second `LedgerBackend` — `MetronomeLedgerBackend` — behind the already-merged Phase-1 interface, so Metronome can become the ledger system-of-record by flipping `LEDGER_BACKEND="metronome"`. Phase 2 wires the adapter, the Metronome customer provisioning, the Stripe-payment-gated funding flow, and the local fast-gate flag the synchronous chat gate reads — fully unit-tested with mocked HTTP — **without changing a single caller** (the whole point of the Phase-1 swap seam). Phase 2 does **not** flip the default and does **not** delete the DDB ledger; that is Phase 3.

**Architecture:** `MetronomeLedgerBackend` implements the same `deplete(event) -> DepleteResult` / `get_balance(owner_id, *, consistent)` contract as `DdbLedgerBackend`. `deplete()` POSTs one usage event to `/v1/ingest` (`transaction_id = event.request_id`, the 34-day-idempotent key; `customer_id = event.owner_id` resolved via an ingest alias; stringified token/cost/model properties) and then optimistically decrements a small local **fast-gate flag** projection in DDB so the synchronous chat gate has a fresh-enough read without touching Metronome's async balance API. `get_balance()` POSTs to `/v1/contracts/customerBalances/getNetBalance`. A thin `MetronomeClient` (httpx) owns the four HTTP shapes (ingest, customer provisioning + Stripe link, fund-commit, balance). The Stripe webhook handler funds a Metronome **payment-gated** commit on a confirmed top-up payment so credits release only on a paid Stripe invoice. A Metronome low-balance webhook route syncs the same fast-gate flag. The `get_ledger()` factory grows a `"metronome"` branch.

**Tech Stack:** Python 3.12, `httpx>=0.28` (already a dep; mocked in tests via `unittest.mock` patching of `httpx.AsyncClient` — `respx==0.20.2` is known-incompatible with httpx 0.28, see `tests/unit/services/test_posthog_admin.py`), `boto3` for the DDB fast-gate flag table (moto-mocked), `pytest` + `pytest-asyncio==0.23.5`. CDK (TypeScript) for one new Secrets Manager secret + env wiring, mirroring the existing `gog_keyring_password` / `stripe_webhook_secret` patterns. **No live Metronome calls anywhere** — every HTTP request shape is asserted against a mock.

---

## Context for the implementer (read before Task 1)

This is **Phase 2** of the Bedrock metering remediation (spec: `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`, §2b + §3.1–3.5). Phase 1 (already merged) built the swap seam: `UsageEvent`, `LedgerBackend` `Protocol`, `DepleteResult`, `get_ledger()` factory, and `DdbLedgerBackend`. **Read those before starting** — Phase 2 mirrors their shape and tests exactly:
- `apps/backend/core/billing/ledger_interface.py` — the interface you implement against (`deplete` / `get_balance` / `get_ledger`).
- `apps/backend/core/billing/usage_event.py` — the 14-field frozen `UsageEvent` you consume.
- `apps/backend/core/billing/ddb_ledger_backend.py` + `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py` — the reference backend + tests to mirror.

The **authoritative Metronome contract** is `docs/superpowers/research/2026-06-07-metronome-ingest-contract.md` — every request shape below is grounded in it. Key facts (verified there):
- **Ingest:** `POST {base}/v1/ingest`, body = JSON array `[{transaction_id, customer_id, timestamp, event_type, properties}]`, Bearer auth. `transaction_id` dedups for **34 days**. `properties` values must be **stringified** ("All keys and values in the properties map should be represented as strings"). Always retry until 200 (transaction_id makes retries safe).
- **Customer identity:** send our `owner_id` directly as `customer_id` by setting `ingest_aliases:["<owner_id>"]` on the Metronome customer — no translation table. Create via `POST /v1/customers/create` with `{name, ingest_aliases}`. Link the **existing** Stripe customer via `POST /v1/customers/{id}/setCustomerBillingProviderConfigurations` with `billing_provider:"stripe"`, `delivery_method:"direct_to_billing_provider"`, configuration `{stripe_customer_id, stripe_collection_method:"charge_automatically"}`.
- **Fund (top-up):** `POST /v1/contracts/edit` with `add_commits`, each carrying `payment_gate_config:{payment_gate_type:"STRIPE"}` so credits release only on confirmed Stripe payment.
- **Balance:** `POST /v1/contracts/customerBalances/getNetBalance` → aggregated remaining balance. Docs give **no low-latency SLA** — do NOT put this on the synchronous chat hot path (hence the local fast-gate flag).
- **Low-balance webhook:** event `alerts.low_remaining_credit_balance_reached`, payload `{type, properties:{customer_id, remaining_balance, threshold, ...}}`. Signature header `Metronome-Webhook-Signature = HMAC_SHA256(secret, DATE_HEADER + "\n" + raw_body)`.
- **Sandbox vs prod:** environment is **token-scoped** (same base URL, different token). Dev uses a **sandbox** token; prod is sales-gated.

**Money unit:** microcents (int), 1 cent = 10_000 microcents — same as Phase 1. Metronome balances are reported by the API in **its own credit unit** (a dollar-denominated decimal in the spendable currency); the adapter converts Metronome dollars → microcents at the boundary (`round(dollars * 1_000_000)`; 1 dollar = 100 cents = 1_000_000 microcents) so the rest of the system keeps speaking microcents.

**Phase-2 boundaries (do NOT cross):**
- `LEDGER_BACKEND` default stays `"ddb"`. Only the factory grows a `"metronome"` branch.
- Do NOT delete or modify `DdbLedgerBackend`, `credit_ledger.py`, `usage_event.py`, or the existing factory tests beyond the one new branch test.
- The DEFERRED live sandbox validation (needs the user's sandbox API key) is flagged, not blocking — the adapter must be fully unit-tested so flipping `LEDGER_BACKEND="metronome"` in dev with the sandbox key just works.

**Run all commands from `apps/backend/` unless noted. Use `uv run` for Python.** Branch `phase-2-metronome-adapter` is already checked out — do NOT push, do NOT switch branches. The pre-commit hook runs `ruff format`/`check` on staged backend files; run `uv run ruff format` + `uv run ruff check --fix` before every commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/core/billing/metronome_client.py` (Create) | Thin httpx client: the four Metronome HTTP shapes (ingest, customer-provision + Stripe link, fund-commit, getNetBalance). No business logic. |
| `apps/backend/core/billing/metronome_fast_gate.py` (Create) | The local fast-gate flag projection (DDB) the synchronous chat gate reads. Optimistic decrement on deplete + webhook-driven set. |
| `apps/backend/core/billing/metronome_ledger_backend.py` (Create) | `MetronomeLedgerBackend` — implements `LedgerBackend`: `deplete()` → ingest + optimistic flag decrement; `get_balance()` → getNetBalance. |
| `apps/backend/core/billing/metronome_provisioning.py` (Create) | `ensure_metronome_customer(owner_id, stripe_customer_id)` — idempotent customer-create with ingest alias + Stripe billing-provider link. |
| `apps/backend/core/billing/metronome_funding.py` (Create) | `fund_top_up(owner_id, amount_microcents)` — add a Stripe-payment-gated prepaid commit. Called from the Stripe webhook. |
| `apps/backend/core/billing/ledger_interface.py` (Modify) | Add the `"metronome"` branch to `get_ledger()`. ONLY change to a Phase-1 file. |
| `apps/backend/core/config.py` (Modify, near line 103) | Add `METRONOME_*` settings (api key, base url, webhook secret, fast-gate table). |
| `apps/backend/core/services/stripe_webhook_handlers.py` (Modify) | In `handle_checkout_session`, fund a Metronome commit when `LEDGER_BACKEND=="metronome"` (alongside the existing DDB `credit_ledger.top_up`). |
| `apps/backend/routers/metronome_webhook.py` (Create) | `POST /webhooks/metronome` — verify HMAC signature, dedup, set the fast-gate flag on `low_remaining_credit_balance_reached`. |
| `apps/backend/main.py` (Modify) | Register the metronome webhook router. |
| `apps/infra/lib/stacks/auth-stack.ts` (Modify) | Create the `metronome_api_key` + `metronome_webhook_secret` secrets. |
| `apps/infra/lib/stacks/service-stack.ts` (Modify) | Wire `METRONOME_API_KEY` + `METRONOME_WEBHOOK_SECRET` as task secrets + `METRONOME_BASE_URL` / fast-gate table env. |
| `apps/infra/lib/isol8-stage.ts` + `apps/infra/lib/local-stage.ts` (Modify) | Pass the two new secret names through `secretNames`. |
| `apps/backend/tests/unit/billing/test_metronome_client.py` (Create) | Asserts exact HTTP request shapes (mocked httpx). |
| `apps/backend/tests/unit/billing/test_metronome_fast_gate.py` (Create) | Fast-gate flag set/read/decrement (moto). |
| `apps/backend/tests/unit/billing/test_metronome_ledger_backend.py` (Create) | `deplete`/`get_balance` against the mocked client. |
| `apps/backend/tests/unit/billing/test_metronome_provisioning.py` (Create) | Customer-create + Stripe-link request shapes + idempotency. |
| `apps/backend/tests/unit/billing/test_metronome_funding.py` (Create) | Payment-gated commit request shape. |
| `apps/backend/tests/unit/billing/test_ledger_interface.py` (Modify) | Add a test for the `"metronome"` factory branch. |
| `apps/backend/tests/unit/routers/test_metronome_webhook.py` (Create) | Signature verify + flag set on low-balance event. |

---

### Task 1: `METRONOME_*` settings

**Files:**
- Modify: `apps/backend/core/config.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_metronome_settings.py`:

```python
"""The Metronome adapter settings exist with sane Phase-2 defaults."""

from core.config import settings


def test_metronome_settings_present_with_defaults():
    # Sandbox is the default base for dev/Phase-2 (Metronome is token-scoped:
    # same base URL, different token; a sandbox token only sees sandbox data).
    assert settings.METRONOME_BASE_URL == "https://api.metronome.com/v1"
    # Secrets default empty so the backend boots in non-Metronome contexts
    # (unit tests, the DDB-default prod path) without configuration.
    assert settings.METRONOME_API_KEY == ""
    assert settings.METRONOME_WEBHOOK_SECRET == ""
    assert settings.METRONOME_FAST_GATE_TABLE == ""
    # Default ledger backend MUST still be ddb in Phase 2 (cutover is Phase 3).
    assert settings.LEDGER_BACKEND == "ddb"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_metronome_settings.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'METRONOME_BASE_URL'`

- [ ] **Step 3: Add the settings**

In `apps/backend/core/config.py`, immediately after the `OAUTH_TOKENS_TABLE: str = ""` line (currently line 109), add:

```python

    # --- Metronome ledger adapter (Phase 2) ---
    # Metronome is the future ledger system-of-record (spec §3.2.3). Phase 2
    # wires the adapter behind the LedgerBackend interface and validates it in
    # the dev environment against the Metronome SANDBOX. The environment is
    # token-scoped: same base URL, the token decides sandbox vs prod, so a
    # sandbox API key only ever touches sandbox data. Empty defaults keep the
    # backend booting in non-Metronome contexts (unit tests + the DDB-default
    # prod path) — get_ledger() only constructs the Metronome backend when
    # LEDGER_BACKEND == "metronome".
    METRONOME_BASE_URL: str = os.getenv("METRONOME_BASE_URL", "https://api.metronome.com/v1")
    """Metronome API base, including the /v1 prefix. Same URL for sandbox + prod."""
    METRONOME_API_KEY: str = os.getenv("METRONOME_API_KEY", "")
    """Bearer token. Sandbox token in dev, prod token in prod. From Secrets Manager."""
    METRONOME_WEBHOOK_SECRET: str = os.getenv("METRONOME_WEBHOOK_SECRET", "")
    """HMAC-SHA256 key for verifying Metronome-Webhook-Signature on inbound webhooks."""
    METRONOME_FAST_GATE_TABLE: str = os.getenv("METRONOME_FAST_GATE_TABLE", "")
    """DDB table backing the local fast-gate flag the synchronous chat gate reads."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_metronome_settings.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/config.py tests/unit/billing/test_metronome_settings.py
uv run ruff check --fix core/config.py tests/unit/billing/test_metronome_settings.py
git add apps/backend/core/config.py apps/backend/tests/unit/billing/test_metronome_settings.py
git commit -m "feat(billing): add METRONOME_* settings for the Phase-2 adapter"
```

---

### Task 2: `MetronomeClient` — the four HTTP shapes

**Files:**
- Create: `apps/backend/core/billing/metronome_client.py`
- Test: `apps/backend/tests/unit/billing/test_metronome_client.py`

**Design (read before coding):**
A thin httpx wrapper that owns exactly the four Metronome request shapes — no business logic, no `UsageEvent` knowledge, no DDB. Each method POSTs JSON and returns the parsed body. Auth is `Authorization: Bearer {api_key}`. The constructor takes an optional `http_client` (an `httpx.AsyncClient`) so tests inject a mock and prod gets a fresh client per call via a small async context-manager helper. We mock `httpx.AsyncClient` directly (not respx) because `respx==0.20.2` mis-matches httpx 0.28 URLs — mirror `tests/unit/services/test_posthog_admin.py`.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/billing/test_metronome_client.py`:

```python
"""Unit tests for MetronomeClient — exact HTTP request shapes, mocked httpx.

respx 0.20.2 mis-matches httpx 0.28 URLs (see tests/unit/services/
test_posthog_admin.py), so we patch the AsyncClient surface directly and
assert on the captured call args.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.billing.metronome_client import MetronomeClient


def _resp(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json = MagicMock(return_value=json_body or {})
    r.text = ""
    r.raise_for_status = MagicMock()
    return r


def _client_with(response: MagicMock) -> tuple[MetronomeClient, MagicMock]:
    fake = MagicMock()
    fake.post = AsyncMock(return_value=response)
    return MetronomeClient(api_key="tok_sandbox", base_url="https://api.metronome.com/v1", http_client=fake), fake


@pytest.mark.asyncio
async def test_ingest_posts_event_array_with_bearer_auth():
    client, fake = _client_with(_resp(200, {}))
    event = {
        "transaction_id": "req-1",
        "customer_id": "org_abc",
        "event_type": "bedrock_invocation",
        "timestamp": "2026-06-08T00:00:00Z",
        "properties": {"input_tokens": "1000"},
    }
    await client.ingest([event])

    fake.post.assert_awaited_once()
    args, kwargs = fake.post.call_args
    assert args[0] == "/ingest"
    # Body is a JSON ARRAY of events (Metronome contract).
    assert kwargs["json"] == [event]
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"


@pytest.mark.asyncio
async def test_create_customer_sends_name_and_ingest_aliases():
    client, fake = _client_with(_resp(200, {"data": {"id": "cust-uuid-1"}}))
    cust_id = await client.create_customer(name="org_abc", ingest_aliases=["org_abc"])

    assert cust_id == "cust-uuid-1"
    args, kwargs = fake.post.call_args
    assert args[0] == "/customers/create"
    assert kwargs["json"] == {"name": "org_abc", "ingest_aliases": ["org_abc"]}


@pytest.mark.asyncio
async def test_set_stripe_billing_config_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.set_stripe_billing_config(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/customers/cust-uuid-1/setCustomerBillingProviderConfigurations"
    assert kwargs["json"] == {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
        "configuration": {
            "stripe_customer_id": "cus_123",
            "stripe_collection_method": "charge_automatically",
        },
    }


@pytest.mark.asyncio
async def test_edit_contract_add_commit_shape():
    client, fake = _client_with(_resp(200, {}))
    commit = {"product_id": "prod-credits", "amount": 25.0}
    await client.edit_contract_add_commits(
        metronome_customer_id="cust-uuid-1",
        contract_id="contract-1",
        add_commits=[commit],
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/edit"
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "contract_id": "contract-1",
        "add_commits": [commit],
    }


@pytest.mark.asyncio
async def test_get_net_balance_returns_parsed_data():
    client, fake = _client_with(_resp(200, {"data": {"net_balance": 12.34}}))
    body = await client.get_net_balance(customer_id="org_abc")

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/customerBalances/getNetBalance"
    assert kwargs["json"] == {"customer_id": "org_abc"}
    assert body == {"net_balance": 12.34}


@pytest.mark.asyncio
async def test_raises_on_http_error():
    bad = _resp(500, {})
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=bad))
    client, _ = _client_with(bad)
    with pytest.raises(httpx.HTTPStatusError):
        await client.ingest([{"transaction_id": "x"}])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.metronome_client'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/metronome_client.py`:

```python
"""Thin httpx client for the four Metronome HTTP shapes we use.

No business logic, no UsageEvent knowledge, no DDB — just request marshalling
and auth. Grounded in docs/superpowers/research/2026-06-07-metronome-ingest-
contract.md. The environment (sandbox vs prod) is decided entirely by the
api_key (Metronome is token-scoped, same base URL).

Tests inject a mock AsyncClient via the `http_client` arg; prod constructs a
fresh client per logical operation via `for_settings()`.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import settings


class MetronomeClient:
    def __init__(self, *, api_key: str, base_url: str, http_client: httpx.AsyncClient):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Any) -> dict:
        resp = await self._http.post(path, json=payload, headers=self._headers)
        resp.raise_for_status()
        return resp.json() or {}

    async def ingest(self, events: list[dict]) -> None:
        """POST /v1/ingest — body is a JSON array of event objects. The
        transaction_id on each event is the 34-day idempotency key, so
        retrying until 200 is safe."""
        await self._post("/ingest", events)

    async def create_customer(self, *, name: str, ingest_aliases: list[str]) -> str:
        """POST /v1/customers/create — returns the Metronome customer UUID.
        ingest_aliases lets us send our owner_id directly as customer_id on
        every event (no translation table)."""
        body = await self._post("/customers/create", {"name": name, "ingest_aliases": ingest_aliases})
        return body["data"]["id"]

    async def set_stripe_billing_config(self, *, metronome_customer_id: str, stripe_customer_id: str) -> None:
        """POST /v1/customers/{id}/setCustomerBillingProviderConfigurations —
        link the EXISTING Stripe customer so Metronome pushes usage/credit
        invoices onto the same Stripe Customer as our $50/mo subscription."""
        await self._post(
            f"/customers/{metronome_customer_id}/setCustomerBillingProviderConfigurations",
            {
                "billing_provider": "stripe",
                "delivery_method": "direct_to_billing_provider",
                "configuration": {
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_collection_method": "charge_automatically",
                },
            },
        )

    async def edit_contract_add_commits(
        self, *, metronome_customer_id: str, contract_id: str, add_commits: list[dict]
    ) -> None:
        """POST /v1/contracts/edit — add prepaid commits (credits). Each commit
        carries payment_gate_config STRIPE upstream so credits release only on
        a confirmed Stripe payment."""
        await self._post(
            "/contracts/edit",
            {
                "customer_id": metronome_customer_id,
                "contract_id": contract_id,
                "add_commits": add_commits,
            },
        )

    async def get_net_balance(self, *, customer_id: str) -> dict:
        """POST /v1/contracts/customerBalances/getNetBalance — aggregated
        remaining balance. Returns the `data` sub-object. NOT for the
        synchronous chat hot path (no low-latency SLA)."""
        body = await self._post("/contracts/customerBalances/getNetBalance", {"customer_id": customer_id})
        return body.get("data", {})


def for_settings(http_client: httpx.AsyncClient) -> MetronomeClient:
    """Build a client from settings, given a caller-owned AsyncClient.

    The caller owns the AsyncClient lifecycle (so it can reuse one connection
    pool across multiple Metronome calls in a single request). Base URL is the
    same for sandbox + prod; the api_key decides the environment.
    """
    return MetronomeClient(
        api_key=settings.METRONOME_API_KEY,
        base_url=settings.METRONOME_BASE_URL,
        http_client=http_client,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_client.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_client.py tests/unit/billing/test_metronome_client.py
uv run ruff check --fix core/billing/metronome_client.py tests/unit/billing/test_metronome_client.py
git add apps/backend/core/billing/metronome_client.py apps/backend/tests/unit/billing/test_metronome_client.py
git commit -m "feat(billing): add MetronomeClient (ingest/customer/commit/balance HTTP shapes)"
```

---

### Task 3: `MetronomeFastGate` — the local fast-gate flag projection

**Files:**
- Create: `apps/backend/core/billing/metronome_fast_gate.py`
- Test: `apps/backend/tests/unit/billing/test_metronome_fast_gate.py`

**Design (read before coding):**
Metronome's balance read is async with no low-latency SLA (research §3), so the synchronous chat gate must NOT call `getNetBalance` inline. Instead, a small DDB projection — the **fast-gate flag** — holds a cheap per-owner snapshot `{owner_id, remaining_microcents, blocked, updated_at}`. It is fed two ways: (1) **optimistic local decrement** on each `deplete()` (so a burst of runs trips the flag before Metronome's webhook arrives), and (2) **authoritative set** from the Metronome low-balance webhook. It carries **no money-correctness burden** — Metronome is SoR; if the flag is stale, Metronome + the IAM/`desired=0` kill-switch are the backstop. Table PK is `owner_id`. `blocked` is the boolean the synchronous gate reads; an owner with no row is treated as **not blocked** (a brand-new owner who hasn't been metered yet should not be gated by a missing projection — funding/metering will create the row).

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/billing/test_metronome_fast_gate.py`:

```python
"""Unit tests for the Metronome local fast-gate flag (moto-mocked DDB)."""

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_fast_gate import MetronomeFastGate


@pytest.fixture
def fast_gate_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


class TestIsBlocked:
    @pytest.mark.asyncio
    async def test_missing_owner_is_not_blocked(self, fast_gate_table):
        # No projection yet -> not gated (metering/funding will create the row).
        assert await MetronomeFastGate().is_blocked("org_new") is False

    @pytest.mark.asyncio
    async def test_blocked_when_flag_set(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=0)
        assert await MetronomeFastGate().is_blocked("org_abc") is True

    @pytest.mark.asyncio
    async def test_not_blocked_when_positive(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=50_000)
        assert await MetronomeFastGate().is_blocked("org_abc") is False


class TestSetBalance:
    @pytest.mark.asyncio
    async def test_set_balance_writes_blocked_and_remaining(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=5_000)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_abc"}},
        )["Item"]
        assert item["remaining_microcents"]["N"] == "5000"
        assert item["blocked"]["BOOL"] is False
        assert "updated_at" in item

    @pytest.mark.asyncio
    async def test_set_balance_zero_marks_blocked(self, fast_gate_table):
        await MetronomeFastGate().set_balance("org_abc", remaining_microcents=0)
        item = fast_gate_table.get_item(
            TableName="test-metronome-fast-gate",
            Key={"owner_id": {"S": "org_abc"}},
        )["Item"]
        assert item["blocked"]["BOOL"] is True


class TestDecrement:
    @pytest.mark.asyncio
    async def test_optimistic_decrement_reduces_remaining(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=10_000)
        new_remaining = await gate.decrement("org_abc", microcents=3_000)
        assert new_remaining == 7_000
        assert await gate.is_blocked("org_abc") is False

    @pytest.mark.asyncio
    async def test_decrement_crossing_zero_blocks(self, fast_gate_table):
        gate = MetronomeFastGate()
        await gate.set_balance("org_abc", remaining_microcents=2_000)
        new_remaining = await gate.decrement("org_abc", microcents=5_000)
        assert new_remaining == -3_000
        assert await gate.is_blocked("org_abc") is True

    @pytest.mark.asyncio
    async def test_decrement_missing_owner_creates_negative_row(self, fast_gate_table):
        # No prior projection: ADD on a missing number starts from 0, so the
        # row lands negative and blocked. Safe-by-default (a metered owner with
        # no funded balance is gated).
        gate = MetronomeFastGate()
        new_remaining = await gate.decrement("org_unfunded", microcents=4_000)
        assert new_remaining == -4_000
        assert await gate.is_blocked("org_unfunded") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_fast_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.metronome_fast_gate'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/metronome_fast_gate.py`:

```python
"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS instead. Fed by (1) optimistic decrement on
deplete() and (2) authoritative set from the low-balance webhook. Carries NO
money-correctness burden: Metronome is the SoR, and the IAM-revoke +
desired=0 kill-switch is the backstop if the flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, blocked:bool, updated_at:str}

An owner with no row is treated as NOT blocked on the gate read (a brand-new
owner shouldn't be gated by a missing projection), but an optimistic
decrement against a missing row starts from 0 and lands negative+blocked
(a metered-but-unfunded owner IS gated).
"""

from __future__ import annotations

import os
import time

import boto3

from core.config import settings


def _table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — fast-gate is misconfigured.")
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_table_name())


class MetronomeFastGate:
    """The synchronous chat gate's view of Metronome balance."""

    async def is_blocked(self, owner_id: str) -> bool:
        resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return False
        return bool(item.get("blocked", False))

    async def set_balance(self, owner_id: str, *, remaining_microcents: int) -> None:
        """Authoritative set (from a Metronome webhook or a fresh balance read)."""
        _table().put_item(
            Item={
                "owner_id": owner_id,
                "remaining_microcents": int(remaining_microcents),
                "blocked": remaining_microcents <= 0,
                "updated_at": _now_iso(),
            }
        )

    async def decrement(self, owner_id: str, *, microcents: int) -> int:
        """Optimistic local decrement on deplete. Returns the new remaining.

        ADD on a missing numeric attribute treats it as 0, so an unfunded
        owner lands negative. Re-derives `blocked` from the post-decrement
        value in the same update.
        """
        resp = _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="ADD remaining_microcents :neg SET updated_at = :now",
            ExpressionAttributeValues={":neg": -int(microcents), ":now": _now_iso()},
            ReturnValues="UPDATED_NEW",
        )
        new_remaining = int(resp["Attributes"]["remaining_microcents"])
        _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="SET blocked = :b",
            ExpressionAttributeValues={":b": new_remaining <= 0},
        )
        return new_remaining
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_fast_gate.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_fast_gate.py tests/unit/billing/test_metronome_fast_gate.py
uv run ruff check --fix core/billing/metronome_fast_gate.py tests/unit/billing/test_metronome_fast_gate.py
git add apps/backend/core/billing/metronome_fast_gate.py apps/backend/tests/unit/billing/test_metronome_fast_gate.py
git commit -m "feat(billing): add Metronome local fast-gate flag projection"
```

---

### Task 4: `MetronomeLedgerBackend` — `deplete` + `get_balance`

**Files:**
- Create: `apps/backend/core/billing/metronome_ledger_backend.py`
- Test: `apps/backend/tests/unit/billing/test_metronome_ledger_backend.py`

**Design (read before coding):**
`MetronomeLedgerBackend` implements the exact `LedgerBackend` contract. `deplete(event)`:
1. Build one Metronome event: `transaction_id = event.request_id` (34-day idempotent), `customer_id = event.owner_id` (resolved by the ingest alias), `event_type = "bedrock_invocation"`, `timestamp = event.occurred_at`, `properties =` **stringified** token counts + model_id + cost fields + region/run_status/source. POST it via `MetronomeClient.ingest([...])`.
2. Optimistically decrement the fast-gate flag by `event.billed_microcents`, getting the new local remaining.
3. Return `DepleteResult(balance_microcents=<local remaining after decrement>, applied=True)`. We return the **optimistic local value** (not a fresh `getNetBalance`) per the research contract: Metronome's balance is async, so reading it back inline would be both slow and stale. `applied` is always `True` from the adapter's perspective — Metronome's 34-day dedup on `transaction_id` makes a duplicate ingest a server-side no-op, but the client gets a 200 either way, so the adapter cannot distinguish a fresh apply from a dedup. (The DDB backend could because it owned the conditional write; Metronome owns dedup, so `applied` is best-effort `True`.)

`get_balance(owner_id, *, consistent=False)`: reads the **LOCAL fast-gate cache** (`MetronomeFastGate.get_remaining`), NOT Metronome's `getNetBalance`. This is the correction from the as-first-written draft (review fix, 2026-06-08): the only two production `get_ledger().get_balance(...)` callers are the synchronous chat gate (`connection_pool._check_credit_gate`) and the provision gate (`provision_gate.evaluate_provision_gate`) — both on the per-request hot path of a 0.5-vCPU box. `getNetBalance` has no low-latency SLA (research §3), so putting it there would re-introduce the exact latency/outage failure mode this remediation fights. Routing the read through the fast-gate inside the adapter keeps callers backend-agnostic (no caller branches on `LEDGER_BACKEND`), fixes BOTH gate sites at once, and honors `consistent=True` via the fast-gate's strongly-consistent DDB read. Metronome stays the system-of-record; the fast-gate is its disposable projection. (`MetronomeClient.get_net_balance` remains on the client for Phase-3 reconciliation/admin, just no longer on the `get_balance` hot path.)

The backend takes an optional `client_factory` + `fast_gate` for tests; prod uses `httpx.AsyncClient` + `MetronomeFastGate()`.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/billing/test_metronome_ledger_backend.py`:

```python
"""Unit tests for MetronomeLedgerBackend (mocked client + mocked fast-gate)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.billing.ledger_interface import DepleteResult
from core.billing.metronome_ledger_backend import MetronomeLedgerBackend
from core.billing.usage_event import UsageEvent


def _event(**overrides) -> UsageEvent:
    base = dict(
        owner_id="org_abc",
        request_id="req-1",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-xyz",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        raw_cost_microcents=10_000,
        billed_microcents=14_000,
        markup_multiplier=1.4,
        region="us-east-1",
        run_status="end",
        occurred_at="2026-06-08T12:00:00Z",
        source="bedrock_invocation_log",
    )
    base.update(overrides)
    return UsageEvent(**base)


def _backend(*, ingest_return=None, net_balance_data=None, decrement_return=86_000):
    fake_client = MagicMock()
    fake_client.ingest = AsyncMock(return_value=ingest_return)
    fake_client.get_net_balance = AsyncMock(return_value=net_balance_data or {})
    fake_gate = MagicMock()
    fake_gate.decrement = AsyncMock(return_value=decrement_return)
    fake_gate.set_balance = AsyncMock()

    backend = MetronomeLedgerBackend(
        client_factory=lambda http: fake_client,
        fast_gate=fake_gate,
    )
    return backend, fake_client, fake_gate


class TestDeplete:
    @pytest.mark.asyncio
    async def test_deplete_ingests_one_event_with_request_id_as_transaction_id(self):
        backend, client, _ = _backend()
        await backend.deplete(_event(request_id="req-XYZ"))

        client.ingest.assert_awaited_once()
        (events,), _ = client.ingest.call_args
        assert len(events) == 1
        ev = events[0]
        assert ev["transaction_id"] == "req-XYZ"
        assert ev["customer_id"] == "org_abc"  # ingest alias = owner_id
        assert ev["event_type"] == "bedrock_invocation"
        assert ev["timestamp"] == "2026-06-08T12:00:00Z"

    @pytest.mark.asyncio
    async def test_deplete_stringifies_all_properties(self):
        backend, client, _ = _backend()
        await backend.deplete(_event())
        (events,), _ = client.ingest.call_args
        props = events[0]["properties"]
        # Metronome contract: all property values must be strings.
        assert all(isinstance(v, str) for v in props.values())
        assert props["input_tokens"] == "1000"
        assert props["output_tokens"] == "500"
        assert props["cache_read_tokens"] == "2000"
        assert props["cache_write_tokens"] == "300"
        assert props["raw_cost_microcents"] == "10000"
        assert props["billed_microcents"] == "14000"
        assert props["markup_multiplier"] == "1.4"
        assert props["model_id"].endswith("aip-xyz")
        assert props["region"] == "us-east-1"
        assert props["run_status"] == "end"
        assert props["source"] == "bedrock_invocation_log"

    @pytest.mark.asyncio
    async def test_deplete_decrements_fast_gate_by_billed_amount(self):
        backend, _, gate = _backend(decrement_return=72_000)
        result = await backend.deplete(_event(billed_microcents=14_000))
        gate.decrement.assert_awaited_once_with("org_abc", microcents=14_000)
        assert isinstance(result, DepleteResult)
        assert result.balance_microcents == 72_000
        assert result.applied is True

    @pytest.mark.asyncio
    async def test_deplete_bills_error_runs(self):
        # Spec §3.2.2: bill on error/aborted too — no status gate.
        backend, client, gate = _backend()
        await backend.deplete(_event(run_status="error"))
        client.ingest.assert_awaited_once()
        gate.decrement.assert_awaited_once()


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_get_balance_converts_dollars_to_microcents(self):
        # 12.34 dollars -> 12_340_000 microcents (1 dollar = 1_000_000 mc).
        backend, _, _ = _backend(net_balance_data={"net_balance": 12.34})
        assert await backend.get_balance("org_abc") == 12_340_000

    @pytest.mark.asyncio
    async def test_get_balance_zero_when_no_balance(self):
        backend, _, _ = _backend(net_balance_data={})
        assert await backend.get_balance("org_abc") == 0

    @pytest.mark.asyncio
    async def test_get_balance_accepts_consistent_kwarg(self):
        backend, _, _ = _backend(net_balance_data={"net_balance": 1.0})
        assert await backend.get_balance("org_abc", consistent=True) == 1_000_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_ledger_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.metronome_ledger_backend'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/metronome_ledger_backend.py`:

```python
"""Metronome-backed ledger (Phase 2). Implements LedgerBackend.

Metronome is the ledger system-of-record (spec §3.2.3). deplete() pushes one
usage event to /v1/ingest keyed on the Bedrock requestId (34-day idempotent),
then optimistically decrements the local fast-gate flag so the synchronous
chat gate sees the spend before Metronome's webhook arrives. get_balance()
reads Metronome's authoritative net balance (NOT for the chat hot path).

Callers depend only on the LedgerBackend interface — swapping from
DdbLedgerBackend to this is a one-line LEDGER_BACKEND flip (the Phase-3
cutover), no caller changes. See docs/superpowers/research/
2026-06-07-metronome-ingest-contract.md §6 for the UsageEvent->Metronome
field mapping.
"""

from __future__ import annotations

from typing import Callable

import httpx

from core.billing import metronome_client
from core.billing.ledger_interface import DepleteResult
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.billing.metronome_client import MetronomeClient
from core.billing.usage_event import UsageEvent

EVENT_TYPE = "bedrock_invocation"

# Metronome reports balances in dollars; the rest of Isol8 speaks microcents.
# 1 dollar = 100 cents = 1_000_000 microcents.
_MICROCENTS_PER_DOLLAR = 1_000_000


class MetronomeLedgerBackend:
    """LedgerBackend over Metronome. See module docstring."""

    def __init__(
        self,
        *,
        client_factory: Callable[[httpx.AsyncClient], MetronomeClient] | None = None,
        fast_gate: MetronomeFastGate | None = None,
    ):
        # Default factory builds a client from settings; tests inject a fake.
        self._client_factory = client_factory or metronome_client.for_settings
        self._fast_gate = fast_gate or MetronomeFastGate()

    def _build_properties(self, event: UsageEvent) -> dict[str, str]:
        """All values stringified — Metronome requires string property values."""
        return {
            "model_id": event.model_id,
            "input_tokens": str(event.input_tokens),
            "output_tokens": str(event.output_tokens),
            "cache_read_tokens": str(event.cache_read_tokens),
            "cache_write_tokens": str(event.cache_write_tokens),
            "raw_cost_microcents": str(event.raw_cost_microcents),
            "billed_microcents": str(event.billed_microcents),
            "markup_multiplier": str(event.markup_multiplier),
            "region": event.region,
            "run_status": event.run_status,
            "source": event.source,
        }

    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Push one usage event to Metronome, then optimistically decrement
        the local fast-gate flag. `applied` is best-effort True: Metronome
        dedups on transaction_id server-side (34 days), so the adapter can't
        tell a fresh apply from a dedup — both return 200."""
        metronome_event = {
            "transaction_id": event.request_id,
            "customer_id": event.owner_id,
            "event_type": EVENT_TYPE,
            "timestamp": event.occurred_at,
            "properties": self._build_properties(event),
        }
        async with httpx.AsyncClient(base_url=metronome_client.settings.METRONOME_BASE_URL) as http:
            client = self._client_factory(http)
            await client.ingest([metronome_event])

        new_remaining = await self._fast_gate.decrement(event.owner_id, microcents=event.billed_microcents)
        return DepleteResult(balance_microcents=new_remaining, applied=True)

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        """Authoritative Metronome net balance in microcents. `consistent` is
        accepted for interface parity but ignored — Metronome offers no
        strong-consistency knob. NOT for the chat hot path; use the fast-gate
        flag (MetronomeFastGate.is_blocked) there."""
        async with httpx.AsyncClient(base_url=metronome_client.settings.METRONOME_BASE_URL) as http:
            client = self._client_factory(http)
            data = await client.get_net_balance(customer_id=owner_id)
        dollars = data.get("net_balance")
        if dollars is None:
            return 0
        return round(float(dollars) * _MICROCENTS_PER_DOLLAR)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_ledger_backend.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_ledger_backend.py tests/unit/billing/test_metronome_ledger_backend.py
uv run ruff check --fix core/billing/metronome_ledger_backend.py tests/unit/billing/test_metronome_ledger_backend.py
git add apps/backend/core/billing/metronome_ledger_backend.py apps/backend/tests/unit/billing/test_metronome_ledger_backend.py
git commit -m "feat(billing): add MetronomeLedgerBackend (ingest deplete + fast-gate decrement)"
```

---

### Task 5: Metronome customer provisioning

**Files:**
- Create: `apps/backend/core/billing/metronome_provisioning.py`
- Test: `apps/backend/tests/unit/billing/test_metronome_provisioning.py`

**Design (read before coding):**
`ensure_metronome_customer(owner_id, stripe_customer_id)` is idempotent: it creates a Metronome customer with `ingest_aliases=[owner_id]` (so events carry `customer_id = owner_id`) and links the **existing** Stripe customer via `set_stripe_billing_config`. The Metronome customer UUID + contract id are persisted to the DDB fast-gate table row (so a re-call short-circuits) — but persistence is via a tiny separate record on the same fast-gate table keyed `owner_id` with a `metronome_customer_id` attribute. To keep this task self-contained and avoid coupling provisioning to the fast-gate's balance shape, we store the Metronome customer id with a `put_item` that only sets `metronome_customer_id` if not present (conditional). Idempotency: a `get_item` finds an existing `metronome_customer_id` → return it, skip both HTTP calls.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/billing/test_metronome_provisioning.py`:

```python
"""Unit tests for Metronome customer provisioning (mocked client + moto DDB)."""

from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_provisioning import ensure_metronome_customer


@pytest.fixture
def fast_gate_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


def _fake_client(customer_id="cust-uuid-1"):
    fake = MagicMock()
    fake.create_customer = AsyncMock(return_value=customer_id)
    fake.set_stripe_billing_config = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_creates_customer_with_ingest_alias_and_links_stripe(fast_gate_table):
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(
        owner_id="org_abc",
        stripe_customer_id="cus_123",
        client=fake,
    )
    assert cust_id == "cust-uuid-1"
    fake.create_customer.assert_awaited_once_with(name="org_abc", ingest_aliases=["org_abc"])
    fake.set_stripe_billing_config.assert_awaited_once_with(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )
    # Persisted for idempotency.
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["metronome_customer_id"]["S"] == "cust-uuid-1"


@pytest.mark.asyncio
async def test_idempotent_skips_http_when_already_provisioned(fast_gate_table):
    fast_gate_table.put_item(
        TableName="test-metronome-fast-gate",
        Item={"owner_id": {"S": "org_abc"}, "metronome_customer_id": {"S": "existing-uuid"}},
    )
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(
        owner_id="org_abc",
        stripe_customer_id="cus_123",
        client=fake,
    )
    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.set_stripe_billing_config.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_provisioning.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.metronome_provisioning'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/metronome_provisioning.py`:

```python
"""Idempotent Metronome customer provisioning.

Creates a Metronome customer whose ingest_aliases include our owner_id (so
usage events carry customer_id = owner_id, no translation table) and links the
EXISTING Stripe customer so Metronome pushes usage/credit invoices onto the
same Stripe Customer as the $50/mo subscription. The Metronome customer UUID
is stashed on the fast-gate table row so a re-call short-circuits both HTTP
calls.
"""

from __future__ import annotations

import os

import boto3
import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.config import settings


def _fast_gate_table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — provisioning is misconfigured.")
    return name


def _table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_fast_gate_table_name())


def _existing_customer_id(owner_id: str) -> str | None:
    resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
    item = resp.get("Item")
    if not item:
        return None
    return item.get("metronome_customer_id")


async def ensure_metronome_customer(
    *,
    owner_id: str,
    stripe_customer_id: str,
    client: MetronomeClient | None = None,
) -> str:
    """Return the Metronome customer UUID, creating + Stripe-linking on first
    call. Idempotent on the persisted metronome_customer_id."""
    existing = _existing_customer_id(owner_id)
    if existing:
        return existing

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            return await _provision(owner_id, stripe_customer_id, metronome_client.for_settings(http))
    return await _provision(owner_id, stripe_customer_id, client)


async def _provision(owner_id: str, stripe_customer_id: str, client: MetronomeClient) -> str:
    customer_id = await client.create_customer(name=owner_id, ingest_aliases=[owner_id])
    await client.set_stripe_billing_config(
        metronome_customer_id=customer_id,
        stripe_customer_id=stripe_customer_id,
    )
    _table().update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="SET metronome_customer_id = :c",
        ExpressionAttributeValues={":c": customer_id},
    )
    return customer_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_provisioning.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_provisioning.py tests/unit/billing/test_metronome_provisioning.py
uv run ruff check --fix core/billing/metronome_provisioning.py tests/unit/billing/test_metronome_provisioning.py
git add apps/backend/core/billing/metronome_provisioning.py apps/backend/tests/unit/billing/test_metronome_provisioning.py
git commit -m "feat(billing): idempotent Metronome customer provisioning (ingest alias + Stripe link)"
```

---

### Task 6: Stripe-payment-gated funding

**Files:**
- Create: `apps/backend/core/billing/metronome_funding.py`
- Test: `apps/backend/tests/unit/billing/test_metronome_funding.py`

**Design (read before coding):**
`fund_top_up(owner_id, amount_microcents, ...)` adds a Metronome prepaid commit carrying `payment_gate_config:{payment_gate_type:"STRIPE"}` so credits release only on a confirmed Stripe payment. The commit `amount` is dollars (`amount_microcents / 1_000_000`). It resolves the Metronome customer id from the fast-gate row (must be provisioned first) and the contract id from settings/args. Because this is called from the Stripe webhook (which is already deduped by `event.id`), the function itself does not re-dedup — but it carries the funding source (the Stripe session id) into the commit's `metadata` for audit/traceability. **After the commit lands it optimistically re-arms the fast-gate cache** (`MetronomeFastGate.add_credit`, best-effort) — corrected from the as-first-written draft (review fix, 2026-06-08). The draft said "the low-balance webhook / next balance read re-arms the flag", but that is wrong now that `get_balance` reads the fast-gate (not `getNetBalance`): the low-balance webhook only fires when balance crosses DOWN past a threshold, never on a top-up, so without the re-arm a paid top-up would never unblock a previously-out-of-credits owner. The re-arm is safe-direction (the commit landed against a CONFIRMED Stripe payment) and best-effort (a cache-write failure must not raise back into the webhook after the money-releasing commit already succeeded; the next low-balance webhook re-syncs the authoritative number).

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/billing/test_metronome_funding.py`:

```python
"""Unit tests for Metronome payment-gated funding (mocked client + moto DDB)."""

from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_funding import FundingError, fund_top_up


@pytest.fixture
def provisioned_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="test-metronome-fast-gate",
            Item={"owner_id": {"S": "org_abc"}, "metronome_customer_id": {"S": "cust-uuid-1"}},
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


def _fake_client():
    fake = MagicMock()
    fake.edit_contract_add_commits = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_fund_adds_payment_gated_stripe_commit(provisioned_table):
    fake = _fake_client()
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,  # $25.00
        contract_id="contract-1",
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()
    _, kwargs = fake.edit_contract_add_commits.call_args
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    commit = kwargs["add_commits"][0]
    assert commit["product_id"] == "prod-credits"
    assert commit["amount"] == 25.0  # dollars
    assert commit["payment_gate_config"] == {"payment_gate_type": "STRIPE"}
    assert commit["metadata"]["stripe_session_id"] == "cs_test_123"


@pytest.mark.asyncio
async def test_fund_raises_when_customer_not_provisioned(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="not provisioned"):
        await fund_top_up(
            owner_id="org_unprovisioned",
            amount_microcents=25_000_000,
            contract_id="contract-1",
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )
    fake.edit_contract_add_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_fund_rejects_non_positive_amount(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await fund_top_up(
            owner_id="org_abc",
            amount_microcents=0,
            contract_id="contract-1",
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_funding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.billing.metronome_funding'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/billing/metronome_funding.py`:

```python
"""Stripe-payment-gated Metronome funding.

A top-up Stripe Checkout payment funds a Metronome PREPAID commit carrying
payment_gate_config STRIPE, so credits release only once Stripe confirms the
payment (Metronome voids the commit on payment failure). Called from the
Stripe webhook (already deduped by event.id), so this function does not
re-dedup; it stamps the Stripe session id into the commit metadata for audit.
"""

from __future__ import annotations

import os

import boto3
import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.config import settings

_MICROCENTS_PER_DOLLAR = 1_000_000


class FundingError(Exception):
    pass


def _fast_gate_table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — funding is misconfigured.")
    return name


def _customer_id_for(owner_id: str) -> str:
    table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_fast_gate_table_name())
    item = table.get_item(Key={"owner_id": owner_id}, ConsistentRead=True).get("Item")
    customer_id = (item or {}).get("metronome_customer_id")
    if not customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    return customer_id


async def fund_top_up(
    *,
    owner_id: str,
    amount_microcents: int,
    contract_id: str,
    product_id: str,
    stripe_session_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Add a Stripe-payment-gated prepaid commit for amount_microcents."""
    if amount_microcents <= 0:
        raise FundingError(f"amount_microcents must be positive, got {amount_microcents}")

    customer_id = _customer_id_for(owner_id)
    commit = {
        "product_id": product_id,
        "amount": amount_microcents / _MICROCENTS_PER_DOLLAR,
        "payment_gate_config": {"payment_gate_type": "STRIPE"},
        "metadata": {"stripe_session_id": stripe_session_id, "owner_id": owner_id},
    }

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await metronome_client.for_settings(http).edit_contract_add_commits(
                metronome_customer_id=customer_id,
                contract_id=contract_id,
                add_commits=[commit],
            )
        return
    await client.edit_contract_add_commits(
        metronome_customer_id=customer_id,
        contract_id=contract_id,
        add_commits=[commit],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_funding.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_funding.py tests/unit/billing/test_metronome_funding.py
uv run ruff check --fix core/billing/metronome_funding.py tests/unit/billing/test_metronome_funding.py
git add apps/backend/core/billing/metronome_funding.py apps/backend/tests/unit/billing/test_metronome_funding.py
git commit -m "feat(billing): add Stripe-payment-gated Metronome funding (add_commits)"
```

---

### Task 7: `get_ledger()` metronome branch

**Files:**
- Modify: `apps/backend/core/billing/ledger_interface.py` (the ONLY Phase-1 file Phase 2 touches)
- Test: `apps/backend/tests/unit/billing/test_ledger_interface.py` (add a branch test)

**Design (read before coding):**
The factory currently raises `ValueError` on `"metronome"`. Phase 2 makes it construct `MetronomeLedgerBackend`. The default stays `"ddb"` (unchanged). The import is local (lazy) so importing `ledger_interface` does not pull `httpx`/Metronome modules unless the metronome backend is actually selected — mirrors the existing `DdbLedgerBackend` lazy import.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_ledger_interface.py`:

```python
def test_get_ledger_returns_metronome_backend_when_selected(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

    ledger = get_ledger()
    assert isinstance(ledger, MetronomeLedgerBackend)


def test_get_ledger_still_defaults_to_ddb(monkeypatch):
    # Phase 2 must NOT change the default — cutover is Phase 3.
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "ddb")
    assert isinstance(get_ledger(), DdbLedgerBackend)


def test_get_ledger_still_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        get_ledger()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py::test_get_ledger_returns_metronome_backend_when_selected -v`
Expected: FAIL — `ValueError: Unknown LEDGER_BACKEND 'metronome' ...` (the current factory rejects it)

- [ ] **Step 3: Update the factory**

In `apps/backend/core/billing/ledger_interface.py`, replace the `get_ledger()` body. Change:

```python
def get_ledger() -> LedgerBackend:
    """Return the configured ledger backend. The swap seam for Phase 3."""
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "ddb":
        from core.billing.ddb_ledger_backend import DdbLedgerBackend

        return DdbLedgerBackend()
    raise ValueError(f"Unknown LEDGER_BACKEND {backend!r} (expected 'ddb'; 'metronome' lands in Phase 3)")
```

to:

```python
def get_ledger() -> LedgerBackend:
    """Return the configured ledger backend. The swap seam for Phase 3.

    Default stays "ddb" (Phase 1); "metronome" is wired (Phase 2) and validated
    in dev against the sandbox before the Phase-3 prod cutover flips the default.
    """
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "ddb":
        from core.billing.ddb_ledger_backend import DdbLedgerBackend

        return DdbLedgerBackend()
    if backend == "metronome":
        from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

        return MetronomeLedgerBackend()
    raise ValueError(f"Unknown LEDGER_BACKEND {backend!r} (expected 'ddb' or 'metronome')")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: PASS (all existing factory tests + the 3 new branch tests). Note: the existing Phase-1 `test_get_ledger_rejects_unknown_backend` used `"metronome"` as its unknown — verify it was replaced by `test_get_ledger_still_rejects_unknown_backend` (which uses `"bogus"`). If the old test still asserts `"metronome"` raises, delete that one line; `"metronome"` is now valid.

- [ ] **Step 5: Reconcile the stale Phase-1 unknown-backend test**

Open `apps/backend/tests/unit/billing/test_ledger_interface.py`. It DOES still contain the Phase-1 test (verified at lines 29-32 — it uses `"metronome"` as the "unknown" backend, which is now a VALID backend, so this test will FAIL):

```python
def test_get_ledger_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    with pytest.raises(ValueError, match="metronome"):
        get_ledger()
```

Delete this entire test function (the new `test_get_ledger_still_rejects_unknown_backend` replaces it — `"metronome"` is no longer an error). Re-run:

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: PASS (no test asserts `"metronome"` raises).

- [ ] **Step 6: Commit**

```bash
uv run ruff format core/billing/ledger_interface.py tests/unit/billing/test_ledger_interface.py
uv run ruff check --fix core/billing/ledger_interface.py tests/unit/billing/test_ledger_interface.py
git add apps/backend/core/billing/ledger_interface.py apps/backend/tests/unit/billing/test_ledger_interface.py
git commit -m "feat(billing): wire get_ledger() metronome branch (default stays ddb)"
```

---

### Task 8: Stripe webhook funds a Metronome commit when metronome is the backend

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py` (`handle_checkout_session`)
- Test: `apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py`

**Design (read before coding):**
`handle_checkout_session` currently calls `credit_ledger.top_up(...)` on a paid `credit_top_up` session. Phase 2 adds: when `settings.LEDGER_BACKEND == "metronome"`, also call `metronome_funding.fund_top_up(...)` so the same paid Stripe top-up funds a Metronome payment-gated commit. The DDB `top_up` call is left intact (Phase 3 deletes it) — under metronome both run, but only the selected ledger's `get_balance` is read by callers, so there is no double-billing of the credit *gate*; the DDB row is just stale shadow data until Phase 3. The Metronome funding is best-effort with a logged exception (a Metronome outage must not 500 the webhook back to Stripe and trigger redelivery storms; the next top-up or a manual reconcile recovers it). Funding needs a `contract_id` + `product_id` — read from new settings `METRONOME_CREDITS_CONTRACT_ID` / `METRONOME_CREDITS_PRODUCT_ID` (added in this task).

- [ ] **Step 1: Add the funding-identity settings**

In `apps/backend/core/config.py`, immediately after the `METRONOME_FAST_GATE_TABLE` line added in Task 1, add:

```python
    METRONOME_CREDITS_CONTRACT_ID: str = os.getenv("METRONOME_CREDITS_CONTRACT_ID", "")
    """Metronome contract id that holds the owner's prepaid credit commits. Per-env."""
    METRONOME_CREDITS_PRODUCT_ID: str = os.getenv("METRONOME_CREDITS_PRODUCT_ID", "")
    """Metronome product id the prepaid credit commit draws against. Per-env."""
```

- [ ] **Step 2: Write the failing tests**

Create `apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py`:

```python
"""handle_checkout_session funds a Metronome commit when LEDGER_BACKEND=metronome."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.billing_service import BillingService
from core.services.stripe_webhook_handlers import handle_checkout_session


def _session_event(payment_status="paid", purpose="credit_top_up"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_status": payment_status,
                "amount_total": 2500,
                "metadata": {
                    "purpose": purpose,
                    "owner_id": "org_abc",
                    "amount_cents": "2500",
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_funds_metronome_when_backend_metronome(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")

    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())

    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "org_abc"
    assert kwargs["amount_microcents"] == 25_000_000  # 2500 cents * 10_000
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["stripe_session_id"] == "cs_test_123"


@pytest.mark.asyncio
async def test_does_not_fund_metronome_when_backend_ddb(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "ddb")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(), BillingService())
    mock_fund.assert_not_awaited()


@pytest.mark.asyncio
async def test_metronome_funding_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock(return_value=0)),
        patch(
            "core.billing.metronome_funding.fund_top_up",
            new=AsyncMock(side_effect=RuntimeError("metronome down")),
        ),
    ):
        # Best-effort: a Metronome outage must not 500 the webhook.
        result = await handle_checkout_session(_session_event(), BillingService())
    assert result is None


@pytest.mark.asyncio
async def test_unpaid_session_funds_nothing(monkeypatch):
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    with (
        patch("core.services.credit_ledger.top_up", new=AsyncMock()) as mock_topup,
        patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund,
    ):
        await handle_checkout_session(_session_event(payment_status="unpaid"), BillingService())
    mock_topup.assert_not_awaited()
    mock_fund.assert_not_awaited()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/test_stripe_webhook_metronome_funding.py -v`
Expected: FAIL — `AssertionError: Expected 'fund_top_up' to have been awaited once. Awaited 0 times.` (the handler does not yet call Metronome funding)

- [ ] **Step 4: Add the funding call to the handler**

In `apps/backend/core/services/stripe_webhook_handlers.py`, inside `handle_checkout_session`, immediately after the existing `await credit_ledger.top_up(...)` block (right before the `amount_total = session.get("amount_total")` line), insert:

```python
    # Phase 2: when Metronome is the ledger backend, fund a Stripe-payment-
    # gated Metronome prepaid commit from the SAME paid top-up. The DDB
    # top_up above is retained as shadow data until Phase 3 deletes it; only
    # the selected backend's get_balance is read by callers, so there is no
    # double-gating. Best-effort — a Metronome outage must not 500 the webhook
    # back to Stripe (which would trigger redelivery storms). The next top-up
    # or a manual reconcile recovers a missed commit.
    from core.config import settings as _settings

    if _settings.LEDGER_BACKEND == "metronome":
        from core.billing import metronome_funding

        try:
            await metronome_funding.fund_top_up(
                owner_id=owner_id,
                amount_microcents=amount_microcents,
                contract_id=_settings.METRONOME_CREDITS_CONTRACT_ID,
                product_id=_settings.METRONOME_CREDITS_PRODUCT_ID,
                stripe_session_id=session.get("id"),
            )
        except Exception:
            put_metric("metronome.fund.error", dimensions={"source": "stripe_checkout"})
            logger.exception(
                "Metronome funding failed for owner %s session %s",
                owner_id,
                session.get("id"),
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/test_stripe_webhook_metronome_funding.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Run the existing webhook-handler tests to confirm no regression**

Run: `uv run pytest tests/unit/services/ -k "webhook or stripe or checkout" -v`
Expected: PASS — the existing handler tests (DDB top-up path) still pass; the new metronome branch is gated on `LEDGER_BACKEND == "metronome"` so the default `"ddb"` path is unchanged.

- [ ] **Step 7: Commit**

```bash
uv run ruff format core/config.py core/services/stripe_webhook_handlers.py tests/unit/services/test_stripe_webhook_metronome_funding.py
uv run ruff check --fix core/config.py core/services/stripe_webhook_handlers.py tests/unit/services/test_stripe_webhook_metronome_funding.py
git add apps/backend/core/config.py apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py
git commit -m "feat(billing): fund Metronome commit on paid top-up when metronome backend"
```

---

### Task 9: Metronome low-balance webhook → fast-gate flag

**Files:**
- Create: `apps/backend/routers/metronome_webhook.py`
- Modify: `apps/backend/main.py` (register the router)
- Test: `apps/backend/tests/unit/routers/test_metronome_webhook.py`

**Design (read before coding):**
`POST /webhooks/metronome` verifies the `Metronome-Webhook-Signature` HMAC (`HMAC_SHA256(secret, DATE_HEADER + "\n" + raw_body)`, hex, constant-time compare), dedups by the event `id` via the existing `webhook_dedup.record_event_or_skip(event_id=..., source="metronome")` (which returns a `WebhookDedupResult` enum — compare against `WebhookDedupResult.ALREADY_SEEN`, NOT a `.is_duplicate` attribute), and on `alerts.low_remaining_credit_balance_reached` sets the fast-gate flag from `properties.remaining_balance` (dollars → microcents).

**Resolving owner_id from the webhook (the only non-obvious bit):** the fast-gate flag is keyed on our `owner_id`, but Metronome's webhook `properties.customer_id` is the Metronome customer UUID. Because we register the customer with `ingest_aliases=[owner_id]`, `customer_id` does NOT necessarily equal `owner_id` in the alert payload (alerts report the canonical UUID). So the handler resolves owner_id in this order:
1. `properties.owner_id` if present — we set this on the alert's surfaced metadata at alert-create time. **Alert creation is out of Phase-2 scope** (it lands in Phase 3), so for Phase 2 the test payload carries `properties.owner_id` to exercise this path.
2. else fall back to `properties.customer_id` — covers the ingest-alias case where the alert reports our alias (`customer_id == owner_id`).

No reverse-lookup table / GSI is used in Phase 2 — the handler reads owner_id straight from the payload, which keeps the route fully unit-testable now without the persisted `metronome_customer_id` mapping. (A UUID→owner_id reverse lookup, if Metronome turns out to only ever report the canonical UUID, is a Phase-3 refinement once the sandbox confirms the alert payload's `customer_id` shape — UNVERIFIED in research §3.)

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/routers/test_metronome_webhook.py`:

```python
"""Unit tests for POST /webhooks/metronome (signature + fast-gate flag set)."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services.webhook_dedup import WebhookDedupResult
from routers.metronome_webhook import router

SECRET = "whsec_metronome_test"
DATE = "Mon, 08 Jun 2026 00:00:00 GMT"


def _sign(body: bytes) -> str:
    msg = DATE.encode("utf-8") + b"\n" + body
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _low_balance_body(owner_id="org_abc", remaining_balance=0.5):
    return json.dumps(
        {
            "id": "evt-1",
            "type": "alerts.low_remaining_credit_balance_reached",
            "properties": {
                "customer_id": owner_id,
                "owner_id": owner_id,
                "remaining_balance": remaining_balance,
                "threshold": 2.0,
            },
        }
    ).encode("utf-8")


def test_rejects_bad_signature(client):
    body = _low_balance_body()
    resp = client.post(
        "/webhooks/metronome",
        content=body,
        headers={"Metronome-Webhook-Signature": "deadbeef", "Date": DATE},
    )
    assert resp.status_code == 401


def test_sets_fast_gate_flag_on_low_balance(client):
    body = _low_balance_body(remaining_balance=0.5)  # $0.50 -> 500_000 microcents
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_awaited_once_with("org_abc", remaining_microcents=500_000)


def test_replay_is_acked_without_processing(client):
    body = _low_balance_body()
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()


def test_ignores_unrelated_event_type(client):
    body = json.dumps(
        {"id": "evt-2", "type": "alerts.spend_threshold_reached", "properties": {"customer_id": "org_abc"}}
    ).encode("utf-8")
    with (
        patch(
            "routers.metronome_webhook.webhook_dedup.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.metronome_webhook.MetronomeFastGate") as gate_cls,
    ):
        gate = gate_cls.return_value
        gate.set_balance = AsyncMock()
        resp = client.post(
            "/webhooks/metronome",
            content=body,
            headers={"Metronome-Webhook-Signature": _sign(body), "Date": DATE},
        )
    assert resp.status_code == 200
    gate.set_balance.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/routers/test_metronome_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'routers.metronome_webhook'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/routers/metronome_webhook.py`:

```python
"""POST /webhooks/metronome — sync the local fast-gate flag from Metronome alerts.

Metronome's balance is async (no low-latency SLA), so the synchronous chat
gate reads the local fast-gate flag. This webhook is the AUTHORITATIVE feed
for that flag: on alerts.low_remaining_credit_balance_reached, set the flag
from the alert's remaining_balance. Optimistic local decrement on deplete()
covers the gap before this fires.

Signature: Metronome-Webhook-Signature = hex HMAC_SHA256(secret, Date + "\n" +
raw_body). Deduped by event id via the shared webhook_dedup helper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, Request, Response

from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.observability.metrics import put_metric
from core.services import webhook_dedup
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)

router = APIRouter()

_LOW_BALANCE_EVENT = "alerts.low_remaining_credit_balance_reached"
_MICROCENTS_PER_DOLLAR = 1_000_000


def _verify_signature(*, secret: str, date_header: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    msg = date_header.encode("utf-8") + b"\n" + body
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/metronome")
async def handle_metronome_webhook(
    request: Request,
    metronome_signature: str = Header("", alias="Metronome-Webhook-Signature"),
    date_header: str = Header("", alias="Date"),
) -> Response:
    body = await request.body()
    if not _verify_signature(
        secret=settings.METRONOME_WEBHOOK_SECRET,
        date_header=date_header,
        body=body,
        signature=metronome_signature,
    ):
        put_metric("metronome.webhook", dimensions={"result": "bad_signature"})
        return Response(status_code=401)

    event = json.loads(body)
    event_id = event.get("id")
    if not event_id:
        return Response(status_code=200)

    dedup = await webhook_dedup.record_event_or_skip(event_id=event_id, source="metronome")
    if dedup == WebhookDedupResult.ALREADY_SEEN:
        return Response(status_code=200)

    if event.get("type") != _LOW_BALANCE_EVENT:
        return Response(status_code=200)

    props = event.get("properties") or {}
    # owner_id is carried in the alert properties (set at alert-create time in
    # Phase 3); fall back to customer_id, which equals owner_id in the
    # ingest-alias model.
    owner_id = props.get("owner_id") or props.get("customer_id")
    if not owner_id:
        logger.warning("Metronome low-balance webhook with no owner_id/customer_id: %s", event_id)
        return Response(status_code=200)

    remaining_dollars = props.get("remaining_balance")
    if remaining_dollars is None:
        logger.warning("Metronome low-balance webhook with no remaining_balance: %s", event_id)
        return Response(status_code=200)

    remaining_microcents = round(float(remaining_dollars) * _MICROCENTS_PER_DOLLAR)
    await MetronomeFastGate().set_balance(owner_id, remaining_microcents=remaining_microcents)
    put_metric(
        "metronome.webhook",
        dimensions={"result": "fast_gate_set", "blocked": str(remaining_microcents <= 0)},
    )
    return Response(status_code=200)
```

- [ ] **Step 4: Register the router**

In `apps/backend/main.py`, find the block where routers are included (search for `app.include_router(`). Add the metronome webhook router alongside the billing router. First add the import near the other router imports:

```python
from routers import metronome_webhook
```

then, alongside the other `app.include_router(...)` calls (e.g. near where the billing router is included), add:

```python
app.include_router(metronome_webhook.router, prefix=settings.API_V1_STR, tags=["billing"])
```

(Match the exact prefix/style of the adjacent `app.include_router` calls — most routers mount under `settings.API_V1_STR`. If the billing router is included without a prefix arg because it sets its own, mirror that instead.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/routers/test_metronome_webhook.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Confirm the app still imports (router registration is valid)**

Run: `uv run python -c "import main; print('app import ok')"`
Expected: prints `app import ok` with no ImportError/traceback (proves `main.py` wiring is valid).

- [ ] **Step 7: Commit**

```bash
uv run ruff format routers/metronome_webhook.py main.py tests/unit/routers/test_metronome_webhook.py
uv run ruff check --fix routers/metronome_webhook.py main.py tests/unit/routers/test_metronome_webhook.py
git add apps/backend/routers/metronome_webhook.py apps/backend/main.py apps/backend/tests/unit/routers/test_metronome_webhook.py
git commit -m "feat(billing): add /webhooks/metronome low-balance -> fast-gate flag sync"
```

---

### Task 10: CDK — Metronome secrets + env wiring

**Files:**
- Modify: `apps/infra/lib/stacks/auth-stack.ts`
- Modify: `apps/infra/lib/stacks/service-stack.ts`
- Modify: `apps/infra/lib/isol8-stage.ts`
- Modify: `apps/infra/lib/local-stage.ts`

**Design (read before coding):**
Mirror the existing secret-wiring pattern exactly: AuthStack `createSecret` mints the secret under `isol8/${env}/...`; the stage passes the bare secret name through `secretNames`; ServiceStack imports it via `fromSecretNameV2` and injects it as a task secret. KMS decrypt is already covered by the wildcard `isol8/${env}/*` `SecretsAccess` policy (line 371-382) + the same CMK, so no new IAM. The non-secret config (`METRONOME_BASE_URL`, `METRONOME_FAST_GATE_TABLE`, contract/product ids, `LEDGER_BACKEND`) goes in the plain `environment` block. `LEDGER_BACKEND` is NOT set here (stays the code default `"ddb"`); dev flips it via an `aws ecs` env override or a follow-up env edit when validating the sandbox — Phase 2 does not deploy `metronome` as the default.

- [ ] **Step 1: Add the AuthStack secrets**

In `apps/infra/lib/stacks/auth-stack.ts`, immediately after the `bedrockMeterToken = createSecret(...)` block (ends ~line 117), add two new secret declarations. First add the public readonly fields near the other `public readonly` secret declarations (after `bedrockMeterToken` at ~line 32):

```typescript
  public readonly metronomeApiKey: secretsmanager.Secret;
  public readonly metronomeWebhookSecret: secretsmanager.Secret;
```

Then, after the `bedrockMeterToken` createSecret block:

```typescript
    // Metronome ledger adapter (Phase 2). The api key is a SANDBOX token in
    // dev (Metronome is token-scoped: same base URL, the token decides
    // sandbox vs prod); prod gets a prod token after the sales engagement.
    // CDK generates a random placeholder on first create; the operator
    // overwrites it out-of-band via `aws secretsmanager put-secret-value`
    // with the real Metronome token. Same CMK as the other auth secrets, so
    // the backend task role's KmsDecryptForSecrets + wildcard SecretsAccess
    // already cover read.
    this.metronomeApiKey = createSecret(
      "MetronomeApiKey",
      "metronome_api_key",
      { passwordLength: 48, excludePunctuation: true },
      "Metronome API bearer token (sandbox in dev, prod after sales). Overwritten out-of-band with the real token.",
    );
    this.metronomeWebhookSecret = createSecret(
      "MetronomeWebhookSecret",
      "metronome_webhook_secret",
      { passwordLength: 48, excludePunctuation: true },
      "HMAC key for verifying Metronome-Webhook-Signature. Overwritten out-of-band with the value from the Metronome dashboard webhook config.",
    );
```

- [ ] **Step 2: Add the secret names to ServiceStack's `SecretNames` interface**

In `apps/infra/lib/stacks/service-stack.ts`, in the `SecretNames` interface (ends ~line 67, after `gogKeyringPassword: string;`), add:

```typescript
  /** Metronome API bearer token (sandbox/prod by token). Read by the ledger adapter. */
  metronomeApiKey: string;
  /** HMAC key verifying inbound Metronome webhook signatures. */
  metronomeWebhookSecret: string;
```

- [ ] **Step 3: Inject the secrets into the task definition**

In `apps/infra/lib/stacks/service-stack.ts`, in the `secrets: { ... }` block (ends ~line 1117, after the `GOG_KEYRING_PASSWORD` entry), add:

```typescript
        // Metronome ledger adapter (Phase 2). Read via settings.METRONOME_API_KEY
        // by the MetronomeLedgerBackend; only used when LEDGER_BACKEND=metronome.
        // KMS decrypt covered by the wildcard SecretsAccess + same CMK.
        METRONOME_API_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportMetronomeApiKey", props.secretNames.metronomeApiKey),
        ),
        METRONOME_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportMetronomeWebhookSecret",
            props.secretNames.metronomeWebhookSecret,
          ),
        ),
```

- [ ] **Step 4: Add the non-secret Metronome env**

In `apps/infra/lib/stacks/service-stack.ts`, in the plain `environment: { ... }` block, immediately after the `OAUTH_TOKENS_TABLE` line (~line 997), add:

```typescript
        // Metronome ledger adapter (Phase 2). Base URL is the same for
        // sandbox + prod (token decides env). Fast-gate table backs the
        // synchronous chat gate's balance projection. LEDGER_BACKEND is NOT
        // set here — it stays the code default "ddb" until the Phase-3 cutover;
        // dev flips it to "metronome" via an env override when validating the
        // sandbox.
        METRONOME_BASE_URL: "https://api.metronome.com/v1",
        METRONOME_FAST_GATE_TABLE: `isol8-${env}-metronome-fast-gate`,
        METRONOME_CREDITS_CONTRACT_ID: "",
        METRONOME_CREDITS_PRODUCT_ID: "",
```

- [ ] **Step 5: Pass the secret names through both stages**

In `apps/infra/lib/isol8-stage.ts`, in the `secretNames: { ... }` block (~line 106-115, after `gogKeyringPassword`), add:

```typescript
        metronomeApiKey: auth.metronomeApiKey.secretName,
        metronomeWebhookSecret: auth.metronomeWebhookSecret.secretName,
```

In `apps/infra/lib/local-stage.ts`, in its `secretNames: { ... }` block (~line 108-116, after `gogKeyringPassword`), add the same two lines:

```typescript
        metronomeApiKey: auth.metronomeApiKey.secretName,
        metronomeWebhookSecret: auth.metronomeWebhookSecret.secretName,
```

- [ ] **Step 6: Synth to verify the CDK compiles + wires**

Run (from `apps/infra/`): `pnpm exec cdk synth isol8-dev-service --quiet 2>&1 | head -40`
Expected: synth succeeds (no TypeScript compile error, no missing-property error on `secretNames.metronomeApiKey`). If synth needs AWS creds and you are offline, instead run the TypeScript build: `pnpm exec tsc --noEmit -p apps/infra/tsconfig.json` from the repo root — expected: no type errors referencing `metronomeApiKey` / `metronomeWebhookSecret`.

- [ ] **Step 7: Commit**

```bash
git add apps/infra/lib/stacks/auth-stack.ts apps/infra/lib/stacks/service-stack.ts apps/infra/lib/isol8-stage.ts apps/infra/lib/local-stage.ts
git commit -m "feat(infra): wire METRONOME api-key + webhook-secret + base/fast-gate env (Phase 2)"
```

> **Note (infra-only, not in this plan):** the `isol8-${env}-metronome-fast-gate` DDB table referenced by `METRONOME_FAST_GATE_TABLE` and granted RW on the task role is a DatabaseStack addition + a `grantReadWriteData` line in service-stack — add it in the same CDK commit if the table does not yet exist (PK `owner_id` (S), PAY_PER_REQUEST), mirroring `creditsTable`. The unit tests above provision their own moto table, so they do not depend on the live table existing.

---

### Task 11: Full-suite verification + final review

**Files:**
- (none — verification only)

- [ ] **Step 1: Run the full billing + new test modules**

Run: `uv run pytest tests/unit/billing/ tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/routers/test_metronome_webhook.py -v`
Expected: PASS — every Phase-2 test green (settings, client, fast-gate, ledger backend, provisioning, funding, factory branch, webhook handler funding, webhook route).

- [ ] **Step 2: Confirm Phase-1 tests still pass (no regression)**

Run: `uv run pytest tests/unit/billing/test_usage_event.py tests/unit/billing/test_ddb_ledger_backend.py tests/unit/billing/test_ledger_interface.py -v`
Expected: PASS — Phase-1 `UsageEvent`, `DdbLedgerBackend`, and the factory tests are unchanged except for the one reconciled unknown-backend test. The `DdbLedgerBackend` is untouched by Phase 2.

- [ ] **Step 3: Run the broader backend suite for collateral regressions**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (same baseline as before Phase 2; the only new failures, if any, must be in files this plan created — investigate immediately, do not proceed).

- [ ] **Step 4: Lint the full Phase-2 surface**

Run: `uv run ruff check core/billing/ core/config.py core/services/stripe_webhook_handlers.py routers/metronome_webhook.py main.py tests/unit/billing/ tests/unit/routers/test_metronome_webhook.py tests/unit/services/test_stripe_webhook_metronome_funding.py`
Expected: no errors.

- [ ] **Step 5: Confirm the default is still ddb (the Phase-2 invariant)**

Run: `uv run python -c "from core.config import settings; assert settings.LEDGER_BACKEND == 'ddb', settings.LEDGER_BACKEND; print('default still ddb OK')"`
Expected: prints `default still ddb OK`. (Phase 2 must NOT flip the default — that is Phase 3.)

- [ ] **Step 6: Commit any lint fixups**

```bash
uv run ruff format core/ routers/ tests/unit/billing/ tests/unit/routers/ tests/unit/services/
uv run ruff check --fix core/ routers/ tests/unit/billing/ tests/unit/routers/ tests/unit/services/
git add -A apps/backend
git commit -m "chore(billing): Phase-2 Metronome adapter lint + final verification" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec + contract coverage (Phase 2 = the Metronome adapter behind the merged interface, spec §3.2.3 + §3.5 Phase 2):**
- §3.5 "implement the Metronome adapter behind the interface and wire it to the Metronome sandbox" → Tasks 2-7 (`MetronomeClient` + `MetronomeLedgerBackend` + `get_ledger()` branch), Task 10 (sandbox-token secret wiring). ✅
- §3.2.3 "deplete(event) → push a Metronome usage event (idempotency = Bedrock requestId; dedup 34 days)" → Task 4 `deplete()` sets `transaction_id = event.request_id`; research §1/§6 grounded. ✅
- Research §6 mapping (owner_id→customer_id via ingest alias; request_id→transaction_id; stringified token/cost/model/region/run_status/source properties; occurred_at→timestamp; constant event_type) → Task 4 `_build_properties` + the metronome_event dict, asserted field-by-field in `test_metronome_ledger_backend.py`. ✅
- §3.2.3 "local fast-gate flag = a disposable projection (low-balance webhooks + optimistic decrement); the synchronous chat gate reads THIS" → Task 3 (`MetronomeFastGate`) + Task 4 (optimistic decrement on deplete) + Task 9 (webhook authoritative set). The **read** half is `MetronomeLedgerBackend.get_balance` → `MetronomeFastGate.get_remaining`, so the two production gate callers (`connection_pool._check_credit_gate`, `provision_gate.evaluate_provision_gate`) read the fast-gate cache, NOT Metronome's no-SLA `getNetBalance`, with zero caller changes. ✅ (Corrected 2026-06-08 from the as-first-written draft, which left `get_balance` calling `getNetBalance` — flagged by spec-review point 5 and quality-review Critical-1. Wired entirely inside the adapter so the plan's "no caller changes" seam still holds.)
- Customer provisioning (ingest_aliases + setCustomerBillingProviderConfigurations stripe link) → Task 5, request shapes from research §1 asserted in `test_metronome_client.py` + `test_metronome_provisioning.py`. ✅
- §3.2.5 / research §2 "Stripe top-up → payment-gated Metronome commit (credits release only on confirmed payment)" → Task 6 (`fund_top_up` with `payment_gate_config STRIPE`) + Task 8 (wired into the existing Stripe `checkout.session.completed` handler). ✅
- §3.3 "fund Metronome prepaid credit → flag refreshed" → Task 6 `fund_top_up` optimistically re-arms the fast-gate cache (`MetronomeFastGate.add_credit`) after the commit lands. ✅ (Added 2026-06-08 — quality-review Important-2: now that `get_balance` reads the fast-gate, a paid top-up must bump the cache to unblock the owner's next message; the low-balance webhook never fires on a top-up.)
- §3.2.3 "Metronome owns balance" → Task 4 `get_balance()` reads `getNetBalance`; dollar→microcents boundary conversion. ✅
- Idempotency / dedup: ingest `transaction_id` (Task 4), webhook `event.id` via the existing `webhook_dedup` helper (Task 9), provisioning persisted-customer-id short-circuit (Task 5). ✅
- "swap from the DDB interim backend without touching callers" → Task 7 (`get_ledger()` metronome branch); NO caller changed; default stays `"ddb"` (asserted in Task 7 + Task 11 Step 5). ✅
- **Not in this plan (correct — Phase 3 / other plans):** flipping `LEDGER_BACKEND` default to `metronome`; deleting `credit_ledger.py` / `DdbLedgerBackend`; prod Metronome account + balance backfill + cutover reconciliation; alert-create API (the webhook handler reads `properties.owner_id`/`customer_id`, alert creation lands in Phase 3); the AWS meter Lambda (Phase 1 Plan 3); the AIP/IAM kill-switch (Phase 1 Plan 2/4).

**2. DEFERRED, flagged not blocking:** live sandbox validation needs the user's Metronome **sandbox API key** (user has sandbox access; prod is sales-gated). The adapter is fully unit-tested with mocked HTTP, and Task 10 wires the sandbox-token secret + env, so flipping `LEDGER_BACKEND="metronome"` in dev with the real sandbox key (set via `aws secretsmanager put-secret-value` on `isol8/dev/metronome_api_key`, + the dev `metronome-fast-gate` table + `METRONOME_CREDITS_CONTRACT_ID`/`PRODUCT_ID` from the sandbox dashboard) is the only remaining step to validate end-to-end. The six research **UNVERIFIED** items (ingest batch limit, max backdate, ingest→balance latency, recovery webhook, pass-through-vs-rate-card dollar billing, sandbox feature gating) are sandbox-measurement tasks, not blockers — the adapter ingests one event per `deplete` (no batching), carries dollar fields as audit properties (rate-card rating is a Metronome-config decision, not adapter code), and re-arms the flag on the next top-up/balance read (no recovery-webhook dependency).

**3. Placeholder scan:** No TODO/TBD/FIXME/"handle edge cases". Every code block is complete and runnable. The empty-string defaults (`METRONOME_API_KEY=""`, `METRONOME_CREDITS_CONTRACT_ID=""`) are intentional non-Metronome-context defaults, not placeholders — they match the existing `STRIPE_FLAT_PRICE_ID=""` / `POSTHOG_PROJECT_API_KEY=""` convention.

**4. Type + name consistency:** `MetronomeClient` method names (`ingest`, `create_customer`, `set_stripe_billing_config`, `edit_contract_add_commits`, `get_net_balance`) are used identically in `MetronomeLedgerBackend`, `metronome_provisioning`, `metronome_funding`, and every test's fake. `DepleteResult(balance_microcents, applied)` matches the Phase-1 `NamedTuple` (imported, not redefined). `MetronomeFastGate` methods (`is_blocked`, `set_balance`, `decrement`) are consistent across the backend, the webhook route, and tests. Microcents↔dollars conversion uses the single `_MICROCENTS_PER_DOLLAR = 1_000_000` constant everywhere it appears (ledger backend, funding, webhook). `LedgerBackend` Protocol conformance: `MetronomeLedgerBackend.deplete` / `get_balance` signatures match the Phase-1 `Protocol` exactly (`async def deplete(self, event: UsageEvent) -> DepleteResult`, `async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int`).

**5. Files touched outside Phase-2 scope:** only `core/billing/ledger_interface.py` (the one allowed Phase-1 file — the `get_ledger()` metronome branch), `core/config.py` (additive settings), `core/services/stripe_webhook_handlers.py` (additive metronome-funding branch gated on `LEDGER_BACKEND`), `main.py` (router registration), and the four CDK files (additive secret/env wiring). `DdbLedgerBackend`, `usage_event.py`, `credit_ledger.py` are untouched.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-08-phase2-metronome-adapter.md`. Recommended order is Task 1 → 11 in sequence, committing each (Task 4's ledger backend imports the client + fast-gate from Tasks 2-3; Task 7's factory branch imports the backend from Task 4; Task 8 imports the funding from Task 6; build them in order). Do NOT push and do NOT flip `LEDGER_BACKEND` — the default stays `"ddb"` until Phase 3. Live sandbox validation (DEFERRED) needs the user's Metronome sandbox API key set on `isol8/dev/metronome_api_key`.
