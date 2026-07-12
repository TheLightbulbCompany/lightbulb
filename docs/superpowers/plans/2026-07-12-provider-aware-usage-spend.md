# Provider-Aware Usage & Spend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three misleading dollar-spend displays with provider-accurate surfaces: Metronome-billed spend for Bedrock owners, plan-quota bars for ChatGPT-OAuth owners, tokens-only for BYOK — across web and mobile.

**Architecture:** One new backend endpoint `GET /billing/spend` returns a `mode`-discriminated payload built by a new `core/services/spend_service.py` (Metronome draft-invoice read for Bedrock, gateway RPC `models.authStatus` for OAuth, graceful `tokens_only` fallback). Frontend/mobile usage screens render by mode; the Teams dashboard "Month Spend" re-points to the same billed number; the Teams Costs panel and all raw-`$` displays are removed.

**Tech Stack:** FastAPI + httpx (Metronome REST), DynamoDB repos, gateway WS pool RPC, Next.js 16 + SWR + vitest, Expo RN + jest.

**Spec:** `docs/superpowers/specs/2026-07-12-provider-aware-usage-spend-design.md` — read it first.

## Global Constraints

- Work on a dedicated worktree branch (never main). Do not push or open PRs without explicit user approval.
- Backend tests: `cd apps/backend && uv run pytest <file> -v` per task; full `uv run pytest tests/ -q` only in the final task.
- Frontend: NEVER run the full `pnpm test` (RAM); run single vitest files only (`pnpm vitest run <file>`). Run `pnpm exec tsc --noEmit` after adding/changing TS (CI typechecks test files too).
- Mobile: `cd apps/mobile && pnpm jest <file> --ci`.
- No PR/bug references in code comments.
- Provider-choice values are exactly `bedrock_claude`, `chatgpt_oauth`, `byo_key` (see `billing_repo.py:349`).
- All money in the new payload is **integer cents**.
- Never let a Metronome or gateway failure surface as an error in the usage panel — degrade to `tokens_only` / omit the costs block.

---

### Task 1: `MetronomeClient.list_customer_invoices`

**Files:**
- Modify: `apps/backend/core/billing/metronome_client.py` (add method after `get_net_balance`, ~line 340)
- Test: `apps/backend/tests/unit/billing/test_metronome_client_invoices.py` (create; follow the style of the existing metronome client tests in `tests/unit/billing/`)

**Interfaces:**
- Produces: `async def list_customer_invoices(self, *, customer_id: str, status: str = "DRAFT") -> list[dict]` — returns the raw invoice dicts from Metronome's `data` array (each has `line_items[]`, `start_timestamp`, `total`).

- [ ] **Step 1: Write the failing test**

```python
"""MetronomeClient.list_customer_invoices — draft-invoice read for the spend surface."""

import httpx
import pytest

from core.billing.metronome_client import MetronomeClient


def _client(handler) -> MetronomeClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.metronome.com/v1")
    return MetronomeClient(api_key="test-key", base_url="https://api.metronome.com/v1", http_client=http)


@pytest.mark.asyncio
async def test_list_customer_invoices_hits_customer_scoped_path_with_status():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [{"id": "inv-1", "line_items": []}]})

    client = _client(handler)
    invoices = await client.list_customer_invoices(customer_id="cus-123")

    assert seen["path"].endswith("/customers/cus-123/invoices")
    assert seen["params"] == {"status": "DRAFT"}
    assert invoices == [{"id": "inv-1", "line_items": []}]


@pytest.mark.asyncio
async def test_list_customer_invoices_empty_data_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.list_customer_invoices(customer_id="cus-123") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/billing/test_metronome_client_invoices.py -v`
Expected: FAIL — `AttributeError: 'MetronomeClient' object has no attribute 'list_customer_invoices'`

- [ ] **Step 3: Implement**

Add after `get_net_balance` in `metronome_client.py`:

```python
    async def list_customer_invoices(self, *, customer_id: str, status: str = "DRAFT") -> list[dict]:
        """GET /v1/customers/{id}/invoices — invoice list for one customer.

        With status=DRAFT this returns the continuously-updated draft usage
        invoice for the current billing period; its line_items carry
        pricing_group_values (our ``tier`` key), which is what the
        user-facing spend surface renders. Read-only display path — the
        gates never call this.
        """
        body = await self._get(f"/customers/{customer_id}/invoices", params={"status": status})
        return body.get("data") or []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/billing/test_metronome_client_invoices.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/metronome_client.py apps/backend/tests/unit/billing/test_metronome_client_invoices.py
git commit -m "feat(billing): MetronomeClient draft-invoice read"
```

---

### Task 2: Spend service — Bedrock billed-spend branch

**Files:**
- Create: `apps/backend/core/services/spend_service.py`
- Test: `apps/backend/tests/unit/services/test_spend_service.py` (create)

**Interfaces:**
- Consumes: `MetronomeClient.list_customer_invoices(customer_id=..., status="DRAFT")` (Task 1); `billing_repo.get_by_owner_id(owner_id)` (existing); `settings.METRONOME_LIFECYCLE_ENABLED`.
- Produces:
  - `async def get_billed_month_spend(owner_id: str) -> BilledSpend | None` — `None` when lifecycle off / no metronome customer / any Metronome failure. `BilledSpend` is a dataclass: `total_cents: int`, `by_model: list[dict]` (each `{"model": str, "tier": str, "cents": int}`), `period_start: str | None`.
  - Module-level 60s cache keyed by owner_id (in-process, like the fast-gate pattern).

- [ ] **Step 1: Write the failing tests**

```python
"""spend_service — provider-aware spend summary building blocks."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import spend_service


DRAFT_INVOICE = {
    "id": "inv-1",
    "start_timestamp": "2026-07-01T00:00:00+00:00",
    "line_items": [
        # usage line items carry our `tier` pricing group; totals are cents
        {"name": "Opus usage", "total": 900.0, "pricing_group_values": {"tier": "opus"}},
        {"name": "Sonnet usage", "total": 334.5, "pricing_group_values": {"tier": "sonnet"}},
        # commit draw-down: negative, no tier — must be excluded
        {"name": "Prepaid commit", "total": -1234.5, "pricing_group_values": None},
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    spend_service._spend_cache.clear()
    yield
    spend_service._spend_cache.clear()


def _patch_lifecycle(enabled: bool):
    return patch.object(spend_service.settings, "METRONOME_LIFECYCLE_ENABLED", enabled)


@pytest.mark.asyncio
async def test_billed_spend_sums_tier_line_items_and_skips_credit_items():
    client = AsyncMock()
    client.list_customer_invoices.return_value = [DRAFT_INVOICE]
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo, "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")

    assert billed is not None
    assert billed.total_cents == 1234  # 900 + 334.5 rounded, credit item excluded
    assert billed.period_start == "2026-07-01T00:00:00+00:00"
    tiers = {row["tier"]: row for row in billed.by_model}
    assert tiers["opus"]["cents"] == 900
    assert tiers["sonnet"]["cents"] == 334  # round-half-even is fine; assert int
    assert tiers["opus"]["model"] == "Claude Opus"


@pytest.mark.asyncio
async def test_billed_spend_none_when_lifecycle_off():
    with _patch_lifecycle(False):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_without_metronome_customer():
    with (
        _patch_lifecycle(True),
        patch.object(spend_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value={})),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_on_metronome_error():
    client = AsyncMock()
    client.list_customer_invoices.side_effect = RuntimeError("metronome down")
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo, "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_zero_when_no_draft_invoice_yet():
    client = AsyncMock()
    client.list_customer_invoices.return_value = []
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo, "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")
    assert billed is not None
    assert billed.total_cents == 0
    assert billed.by_model == []


@pytest.mark.asyncio
async def test_billed_spend_cached_within_ttl():
    client = AsyncMock()
    client.list_customer_invoices.return_value = [DRAFT_INVOICE]
    with (
        _patch_lifecycle(True),
        patch.object(spend_service, "_metronome_client", return_value=client),
        patch.object(
            spend_service.billing_repo, "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        await spend_service.get_billed_month_spend("owner-1")
        await spend_service.get_billed_month_spend("owner-1")
    assert client.list_customer_invoices.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_spend_service.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` for `core.services.spend_service`

- [ ] **Step 3: Implement `spend_service.py` (Bedrock branch)**

```python
"""Provider-aware spend summary for the user-facing usage surface.

Three modes, one per provider path (see the 2026-07-12 design spec):

- ``bedrock``      — month-to-date BILLED spend from the Metronome draft
                     usage invoice (the number Metronome finalizes), with a
                     per-model split from the ``tier`` pricing group.
- ``oauth_quota``  — ChatGPT plan-quota windows proxied from the container's
                     ``models.authStatus`` gateway RPC.
- ``tokens_only``  — BYOK / trial / any degraded case. The panel renders the
                     token grid it already has; this module adds nothing.

Display-only. The provision/chat gates never read this module; a failure
here must never block anything — every branch degrades, never raises.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# Cache TTL for the Metronome draft-invoice read. The panel revalidates on
# focus; without this every remount would hit Metronome.
_CACHE_TTL_SECONDS = 60.0
_spend_cache: dict[str, tuple[float, "BilledSpend"]] = {}

# tier (Metronome pricing-group value) -> display name. Tiers are the model
# families the meter Lambda prices by; unknown tiers fall back to the raw key.
_TIER_DISPLAY_NAMES = {
    "opus": "Claude Opus",
    "sonnet": "Claude Sonnet",
    "haiku": "Claude Haiku",
}


@dataclass
class BilledSpend:
    total_cents: int
    by_model: list[dict]
    period_start: str | None


def _metronome_client():
    """Late-bound client factory (patchable in tests)."""
    from core.billing.metronome_client import for_settings

    return for_settings(httpx.AsyncClient())


async def get_billed_month_spend(owner_id: str) -> BilledSpend | None:
    """Month-to-date billed spend from the Metronome draft invoice.

    None when the Metronome lifecycle is off, the owner has no Metronome
    customer, or Metronome errors — callers hide the dollar surface then.
    Usage line items only: an item counts iff its pricing_group_values
    carry a ``tier``; commit/credit draw-downs (no tier, negative totals)
    are excluded — remaining credits are the balance surface's job.
    """
    if not settings.METRONOME_LIFECYCLE_ENABLED:
        return None

    cached = _spend_cache.get(owner_id)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    account = await billing_repo.get_by_owner_id(owner_id) or {}
    customer_id = account.get("metronome_customer_id")
    if not customer_id:
        return None

    try:
        invoices = await _metronome_client().list_customer_invoices(customer_id=customer_id)
    except Exception:
        logger.warning("spend: Metronome draft-invoice read failed for %s", owner_id, exc_info=True)
        return None

    by_tier: dict[str, int] = {}
    period_start: str | None = None
    for invoice in invoices:
        period_start = period_start or invoice.get("start_timestamp")
        for item in invoice.get("line_items") or []:
            tier = (item.get("pricing_group_values") or {}).get("tier")
            if not tier:
                continue
            by_tier[tier] = by_tier.get(tier, 0) + round(float(item.get("total") or 0))

    by_model = [
        {"model": _TIER_DISPLAY_NAMES.get(tier, tier), "tier": tier, "cents": cents}
        for tier, cents in sorted(by_tier.items(), key=lambda kv: -kv[1])
    ]
    billed = BilledSpend(
        total_cents=sum(by_tier.values()),
        by_model=by_model,
        period_start=period_start,
    )
    _spend_cache[owner_id] = (time.monotonic(), billed)
    return billed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_spend_service.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/spend_service.py apps/backend/tests/unit/services/test_spend_service.py
git commit -m "feat(billing): billed month spend from Metronome draft invoice"
```

---

### Task 3: Spend service — OAuth quota branch + mode dispatcher

**Files:**
- Modify: `apps/backend/core/services/spend_service.py`
- Test: `apps/backend/tests/unit/services/test_spend_service.py` (extend)

**Interfaces:**
- Consumes: `get_ecs_manager().resolve_running_container(owner_id) -> (container, ip)` and `get_gateway_pool().send_rpc(user_id=..., req_id=..., method="models.authStatus", params={}, ip=..., token=container["gateway_token"])` — the exact pattern in `routers/channels.py:84-98`. The RPC result shape is `{"ts": ..., "providers": [{"provider": "openai", "usage": {"windows": [{"label", "usedPercent", "resetAt"}], "plan", "summary"}}]}` (OpenClaw `models-auth-status.ts`; `resetAt` is ms epoch).
- Produces: `async def get_spend_summary(owner_id: str, provider_choice: str | None) -> dict` — the `/billing/spend` payload:
  - `{"mode": "bedrock", "billed_month_to_date_cents": int, "by_model": [...], "period_start": str|None}`
  - `{"mode": "oauth_quota", "plan": str|None, "windows": [{"label": str, "used_percent": float, "resets_at_ms": int|None}]}`
  - `{"mode": "tokens_only"}`

- [ ] **Step 1: Write the failing tests** (append to `test_spend_service.py`)

```python
AUTH_STATUS_RESULT = {
    "ts": 1,
    "providers": [
        {"provider": "anthropic"},
        {
            "provider": "openai",
            "usage": {
                "plan": "pro",
                "windows": [
                    {"label": "5h", "usedPercent": 43, "resetAt": 1789000000000},
                    {"label": "Week", "usedPercent": 61},
                ],
            },
        },
    ],
}


def _patch_gateway(rpc_result=None, rpc_error=None, container=("c", "10.0.0.1")):
    """Patch the ecs/pool accessors spend_service uses for the OAuth branch."""
    ecs = AsyncMock()
    ecs.resolve_running_container.return_value = (
        ({"gateway_token": "tok"}, container[1]) if container else (None, None)
    )
    pool = AsyncMock()
    if rpc_error:
        pool.send_rpc.side_effect = rpc_error
    else:
        pool.send_rpc.return_value = rpc_result
    return (
        patch.object(spend_service, "_ecs_manager", return_value=ecs),
        patch.object(spend_service, "_gateway_pool", return_value=pool),
    )


@pytest.mark.asyncio
async def test_summary_oauth_maps_quota_windows():
    p1, p2 = _patch_gateway(rpc_result=AUTH_STATUS_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload["mode"] == "oauth_quota"
    assert payload["plan"] == "pro"
    assert payload["windows"] == [
        {"label": "5h", "used_percent": 43, "resets_at_ms": 1789000000000},
        {"label": "Week", "used_percent": 61, "resets_at_ms": None},
    ]


@pytest.mark.asyncio
async def test_summary_oauth_degrades_to_tokens_only_on_rpc_failure():
    p1, p2 = _patch_gateway(rpc_error=RuntimeError("container gone"))
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_oauth_degrades_when_no_container():
    p1, p2 = _patch_gateway(container=None)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_byok_and_unknown_are_tokens_only():
    assert await spend_service.get_spend_summary("owner-1", "byo_key") == {"mode": "tokens_only"}
    assert await spend_service.get_spend_summary("owner-1", None) == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_bedrock_uses_billed_spend():
    billed = spend_service.BilledSpend(total_cents=1234, by_model=[], period_start="2026-07-01")
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=billed)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {
        "mode": "bedrock",
        "billed_month_to_date_cents": 1234,
        "by_model": [],
        "period_start": "2026-07-01",
    }


@pytest.mark.asyncio
async def test_summary_bedrock_degrades_when_metronome_unavailable():
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=None)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {"mode": "tokens_only"}
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_spend_service.py -v`
Expected: Task-2 tests PASS, new tests FAIL — `AttributeError: ... has no attribute 'get_spend_summary'`

- [ ] **Step 3: Implement** (append to `spend_service.py`)

```python
def _ecs_manager():
    from core.containers import get_ecs_manager

    return get_ecs_manager()


def _gateway_pool():
    from core.containers import get_gateway_pool

    return get_gateway_pool()


async def _oauth_quota_payload(owner_id: str) -> dict | None:
    """ChatGPT plan-quota windows via the container's models.authStatus RPC.

    None on ANY failure (container down, RPC error, usage block absent —
    the upstream quota endpoint is unofficial and may vanish); the caller
    falls back to tokens_only. OpenClaw caches upstream 60s; no cache here.
    """
    try:
        container, ip = await _ecs_manager().resolve_running_container(owner_id)
        if not container or not ip:
            return None
        result = await _gateway_pool().send_rpc(
            user_id=owner_id,
            req_id=f"spend-authstatus-{owner_id}",
            method="models.authStatus",
            params={},
            ip=ip,
            token=container["gateway_token"],
        )
        for provider in (result or {}).get("providers") or []:
            if provider.get("provider") != "openai":
                continue
            usage = provider.get("usage") or {}
            windows = [
                {
                    "label": w.get("label", ""),
                    "used_percent": w.get("usedPercent", 0),
                    "resets_at_ms": w.get("resetAt"),
                }
                for w in usage.get("windows") or []
            ]
            if not windows:
                return None
            return {"mode": "oauth_quota", "plan": usage.get("plan"), "windows": windows}
        return None
    except Exception:
        logger.warning("spend: models.authStatus quota read failed for %s", owner_id, exc_info=True)
        return None


async def get_spend_summary(owner_id: str, provider_choice: str | None) -> dict:
    """The /billing/spend payload. Never raises — degraded == tokens_only."""
    if provider_choice == "bedrock_claude":
        billed = await get_billed_month_spend(owner_id)
        if billed is None:
            return {"mode": "tokens_only"}
        return {
            "mode": "bedrock",
            "billed_month_to_date_cents": billed.total_cents,
            "by_model": billed.by_model,
            "period_start": billed.period_start,
        }
    if provider_choice == "chatgpt_oauth":
        return await _oauth_quota_payload(owner_id) or {"mode": "tokens_only"}
    return {"mode": "tokens_only"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_spend_service.py -v`
Expected: 12 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/spend_service.py apps/backend/tests/unit/services/test_spend_service.py
git commit -m "feat(billing): provider-aware spend summary with oauth quota branch"
```

---

### Task 4: `GET /billing/spend` endpoint

**Files:**
- Modify: `apps/backend/schemas/billing.py` (add response models)
- Modify: `apps/backend/routers/billing.py` (new route after `/my-usage`, ~line 230)
- Test: `apps/backend/tests/unit/routers/test_billing_spend.py` (create; copy the FastAPI test-client + auth-override fixture pattern from an existing `tests/unit/routers/test_billing*.py`)

**Interfaces:**
- Consumes: `spend_service.get_spend_summary(owner_id, provider_choice)` (Task 3); `resolve_owner_id(auth)`, `billing_repo.get_by_owner_id` (both already imported in `routers/billing.py`).
- Produces: `GET /api/v1/billing/spend` → `SpendResponse` — the wire contract Tasks 7/9 consume:

```json
{"mode": "bedrock", "billed_month_to_date_cents": 1234,
 "by_model": [{"model": "Claude Opus", "tier": "opus", "cents": 900}],
 "period_start": "2026-07-01T00:00:00+00:00", "plan": null, "windows": null}
```

- [ ] **Step 1: Add schemas** (in `schemas/billing.py`)

```python
class SpendByModel(BaseModel):
    model: str
    tier: str
    cents: int


class SpendQuotaWindow(BaseModel):
    label: str
    used_percent: float
    resets_at_ms: int | None = None


class SpendResponse(BaseModel):
    """Provider-aware spend summary. `mode` picks the render variant:
    bedrock -> billed fields; oauth_quota -> plan/windows; tokens_only -> neither."""

    mode: Literal["bedrock", "oauth_quota", "tokens_only"]
    billed_month_to_date_cents: int | None = None
    by_model: list[SpendByModel] | None = None
    period_start: str | None = None
    plan: str | None = None
    windows: list[SpendQuotaWindow] | None = None
```

(Add `from typing import Literal` to the imports if absent.)

- [ ] **Step 2: Write the failing router test**

```python
"""GET /billing/spend — provider-aware spend summary endpoint."""

from unittest.mock import AsyncMock, patch

import pytest

# Reuse the app/client/auth fixtures used by the existing billing router
# tests in this directory (same get_current_user override pattern).


@pytest.mark.asyncio
async def test_spend_endpoint_bedrock(client):
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch(
            "routers.billing.spend_service.get_spend_summary",
            new=AsyncMock(return_value={
                "mode": "bedrock",
                "billed_month_to_date_cents": 1234,
                "by_model": [{"model": "Claude Opus", "tier": "opus", "cents": 900}],
                "period_start": "2026-07-01T00:00:00+00:00",
            }),
        ) as summary,
    ):
        resp = await client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "bedrock"
    assert body["billed_month_to_date_cents"] == 1234
    assert body["by_model"][0]["tier"] == "opus"
    # provider_choice from the billing row is what gets dispatched on
    assert summary.await_args.args[1] == "bedrock_claude"


@pytest.mark.asyncio
async def test_spend_endpoint_tokens_only_without_billing_row(client):
    with patch(
        "routers.billing.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.get("/api/v1/billing/spend")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "tokens_only"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_spend.py -v`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 4: Implement the route** (in `routers/billing.py`, after `/my-usage`)

```python
from core.services import spend_service  # add to the imports block
from schemas.billing import SpendResponse  # extend the existing schemas import


@router.get(
    "/spend",
    response_model=SpendResponse,
    summary="Provider-aware spend summary",
    description=(
        "Bedrock owners: month-to-date BILLED spend from Metronome with a "
        "per-model split. ChatGPT-OAuth owners: plan-quota windows. BYOK/"
        "trial/degraded: tokens_only (the panel keeps its token grid). "
        "Display-only; never errors on upstream failure."
    ),
    operation_id="get_spend",
)
async def get_spend(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    account = await billing_repo.get_by_owner_id(owner_id) or {}
    if account.get("provider_choice") is None:
        return SpendResponse(mode="tokens_only")
    payload = await spend_service.get_spend_summary(owner_id, account.get("provider_choice"))
    return SpendResponse(**payload)
```

- [ ] **Step 5: Run tests, then commit**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_spend.py tests/unit/services/test_spend_service.py -v`
Expected: all PASS

```bash
git add apps/backend/schemas/billing.py apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_spend.py
git commit -m "feat(billing): GET /billing/spend provider-aware endpoint"
```

---

### Task 5: Teams dashboard re-point + delete `/teams/costs`

**Files:**
- Modify: `apps/backend/routers/teams/feed.py` (delete `get_costs` route at lines 142-148; rework the costs block in `get_dashboard`)
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (delete `get_costs`, ~line 1946 — grep first to confirm `feed.py` was its only caller)
- Test: `apps/backend/tests/unit/routers/teams/test_feed_dashboard_costs.py` (create, following the existing teams feed test setup in `tests/unit/routers/teams/`)

**Interfaces:**
- Consumes: `spend_service.get_billed_month_spend(owner_id) -> BilledSpend | None` (Task 2); `resolve_owner_id(auth)` (already imported in `feed.py`).
- Produces: `/teams/dashboard` response where `dashboard.costs` is either absent (non-Bedrock / Metronome down) or `{"monthSpendCents": <int>}` (Bedrock). The Paperclip-sourced costs block is never passed through. `/teams/costs` returns 404 (route gone).

- [ ] **Step 1: Write the failing tests**

```python
"""/teams/dashboard costs block — Metronome-billed, never Paperclip cost_events."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.spend_service import BilledSpend

# Reuse the existing teams-feed test fixtures (TeamsContext override +
# _agents._admin mock) from this directory's other feed tests.

PAPERCLIP_DASH = {
    "agents": {}, "tasks": {},
    "costs": {"monthSpendCents": 55555, "monthBudgetCents": 0, "monthUtilizationPercent": 0},
}


@pytest.mark.asyncio
async def test_dashboard_costs_replaced_with_billed_spend(client, mock_admin):
    mock_admin.get_dashboard.return_value = dict(PAPERCLIP_DASH)
    with patch(
        "routers.teams.feed.spend_service.get_billed_month_spend",
        new=AsyncMock(return_value=BilledSpend(total_cents=1234, by_model=[], period_start=None)),
    ):
        resp = await client.get("/api/v1/teams/dashboard")
    assert resp.status_code == 200
    assert resp.json()["dashboard"]["costs"] == {"monthSpendCents": 1234}


@pytest.mark.asyncio
async def test_dashboard_costs_omitted_when_not_billed(client, mock_admin):
    mock_admin.get_dashboard.return_value = dict(PAPERCLIP_DASH)
    with patch(
        "routers.teams.feed.spend_service.get_billed_month_spend",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.get("/api/v1/teams/dashboard")
    assert resp.status_code == 200
    assert "costs" not in resp.json()["dashboard"]


@pytest.mark.asyncio
async def test_costs_route_deleted(client):
    resp = await client.get("/api/v1/teams/costs")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/test_feed_dashboard_costs.py -v`
Expected: FAIL (costs passthrough still present; /teams/costs still 200)

- [ ] **Step 3: Implement**

In `feed.py`: delete the `get_costs` route (lines 142-148), add `from core.services import spend_service` to imports, and in `get_dashboard` replace the return block:

```python
    dash = dash if isinstance(dash, dict) else {}
    # Paperclip's cost_events ledger only sees successful Paperclip-driven
    # runs — a structural undercount. The billed number comes from
    # Metronome (the invoice we actually charge); when the owner isn't on
    # the Bedrock path (or Metronome is down) the costs block is omitted
    # and the frontend hides the Month Spend card.
    dash.pop("costs", None)
    billed = await spend_service.get_billed_month_spend(resolve_owner_id(auth))
    if billed is not None:
        dash["costs"] = {"monthSpendCents": billed.total_cents}
    return {
        "dashboard": dash,
        "sidebar_badges": badges,
    }
```

In `paperclip_admin_client.py`: run `grep -rn "get_costs" apps/backend --include='*.py'` — confirm only the deleted route and the client method remain, then delete the `get_costs` method.

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/teams/ -v`
Expected: new tests PASS; fix any existing feed tests that asserted the old costs passthrough (update them to the new contract, don't delete coverage).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/teams/feed.py apps/backend/core/services/paperclip_admin_client.py apps/backend/tests/unit/routers/teams/
git commit -m "feat(teams): dashboard month spend from Metronome; drop /teams/costs"
```

---

### Task 6: Backend drive-by fixes

**Files:**
- Modify: `apps/backend/core/services/admin_service.py:235` (dead `"current"` period)
- Modify: `apps/backend/core/services/usage_service.py` (stale docstring re `meta/` raw-cost override)
- Test: extend the existing admin_service overview test (find it: `grep -rln "get_overview" apps/backend/tests/`)

**Interfaces:** none new — behavior fix only.

- [ ] **Step 1: Write/adjust the failing test** — in the existing overview test file, assert that `usage_repo.get_period_usage` is called with the current `YYYY-MM` period string (freeze time or compute the same way the code does), not `"current"`:

```python
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_overview_reads_current_month_usage_period(...existing fixtures...):
    now = datetime.now(timezone.utc)
    expected_period = f"{now.year}-{now.month:02d}"
    # ... invoke get_overview with mocked repos ...
    usage_repo_mock.get_period_usage.assert_awaited_with(owner_id, expected_period)
```

- [ ] **Step 2: Run to verify it fails** (asserts `"current"` today)

- [ ] **Step 3: Fix** — in `admin_service.py` replace line 235:

```python
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    period = f"{now.year}-{now.month:02d}"  # usage_repo rows are YYYY-MM / lifetime / member:* — "current" is never written
```

(Match `usage_service.py:13-14`'s period computation; import at module top if cleaner.)

In `usage_service.py`, fix the docstring block (~lines 68-79) that claims the raw-cost override applies to `meta/` refs: the code gate is `_is_bedrock_model` (amazon-bedrock/ prefix only) and the Meta trial is gone — rewrite the docstring to describe Bedrock-only override, no code change.

- [ ] **Step 4: Run** `cd apps/backend && uv run pytest tests/unit/services/test_admin_service*.py tests/unit/services/test_usage_service*.py -v` — PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/admin_service.py apps/backend/core/services/usage_service.py apps/backend/tests/
git commit -m "fix(admin): read real YYYY-MM usage period; correct usage_service docstring"
```

---

### Task 7: Web — `useSpend` hook + provider-aware UsagePanel

**Files:**
- Create: `apps/frontend/src/hooks/useSpend.ts`
- Modify: `apps/frontend/src/components/control/panels/UsagePanel.tsx`
- Test: `apps/frontend/src/components/control/panels/__tests__/UsagePanel.spend.test.tsx` (create; follow an existing panel vitest for render/mocking conventions)

**Interfaces:**
- Consumes: `GET /billing/spend` (Task 4 `SpendResponse` shape); existing `useApi()` and `useBilling()`.
- Produces: `useSpend(): { spend: SpendResponse | undefined, isLoading: boolean }`; `SpendResponse` TS type exported from `useSpend.ts` (Task 9 mobile mirrors it).

- [ ] **Step 1: Write `useSpend.ts`**

```typescript
"use client";

import useSWR from "swr";
import { useApi } from "@/lib/api";

export interface SpendByModel {
  model: string;
  tier: string;
  cents: number;
}

export interface SpendQuotaWindow {
  label: string;
  used_percent: number;
  resets_at_ms: number | null;
}

export interface SpendResponse {
  mode: "bedrock" | "oauth_quota" | "tokens_only";
  billed_month_to_date_cents: number | null;
  by_model: SpendByModel[] | null;
  period_start: string | null;
  plan: string | null;
  windows: SpendQuotaWindow[] | null;
}

/** Provider-aware spend summary. tokens_only means: render the token grid
 * with no dollar/quota block (BYOK, trial, or degraded upstream). */
export function useSpend() {
  const api = useApi();
  const { data, isLoading } = useSWR<SpendResponse>(
    "/billing/spend",
    () => api.get("/billing/spend") as Promise<SpendResponse>,
    { dedupingInterval: 30000 },
  );
  return { spend: data, isLoading };
}
```

- [ ] **Step 2: Write the failing render tests**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UsagePanel } from "../UsagePanel";

const usageFixture = {
  period: "2026-07",
  total_spend: 12.3456,
  total_input_tokens: 1000,
  total_output_tokens: 2000,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  request_count: 5,
  lifetime_spend: 99,
  by_member: [
    { user_id: "u1", display_name: "Ada", email: null, total_spend: 5, total_input_tokens: 1, total_output_tokens: 1, request_count: 2 },
  ],
};

const spendState: { value: unknown } = { value: { mode: "tokens_only" } };

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: { subscription_status: "active" },
    isLoading: false,
    fetchUsage: async () => usageFixture,
    refresh: () => {},
  }),
}));
vi.mock("@/hooks/useSpend", () => ({
  useSpend: () => ({ spend: spendState.value, isLoading: false }),
}));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));

describe("UsagePanel spend display", () => {
  it("never renders the usage-counters dollar figures", async () => {
    spendState.value = { mode: "tokens_only" };
    render(<UsagePanel />);
    expect(await screen.findByText(/5 requests/)).toBeInTheDocument();
    expect(screen.queryByText(/Total spend/)).toBeNull();
    expect(screen.queryByText(/Lifetime/)).toBeNull();
    expect(screen.queryByText("$12.3456")).toBeNull();
    // member table keeps requests, drops the Spend column
    expect(screen.queryByText("Spend")).toBeNull();
  });

  it("bedrock mode renders billed month-to-date and per-model split", async () => {
    spendState.value = {
      mode: "bedrock",
      billed_month_to_date_cents: 1234,
      by_model: [{ model: "Claude Opus", tier: "opus", cents: 900 }],
      period_start: "2026-07-01T00:00:00+00:00",
    };
    render(<UsagePanel />);
    expect(await screen.findByText("$12.34")).toBeInTheDocument();
    expect(screen.getByText(/Billed this month/i)).toBeInTheDocument();
    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
  });

  it("oauth mode renders quota windows", async () => {
    spendState.value = {
      mode: "oauth_quota",
      plan: "pro",
      windows: [{ label: "Week", used_percent: 61, resets_at_ms: null }],
    };
    render(<UsagePanel />);
    expect(await screen.findByText(/Week/)).toBeInTheDocument();
    expect(screen.getByText(/61%/)).toBeInTheDocument();
    expect(screen.getByText(/included in your ChatGPT plan/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/frontend && pnpm vitest run src/components/control/panels/__tests__/UsagePanel.spend.test.tsx`
Expected: FAIL (dollar figures still rendered; modes not implemented)

- [ ] **Step 4: Rework `UsagePanel.tsx`**

Edits (keep existing styling vocabulary — `border-[#e0dbd0]`, `text-[#8a8578]`, etc.):

1. Import + call `useSpend()`.
2. **Delete** from the Token Usage card footer: `<span>Total spend: {formatDollars(usage.total_spend, 4)}</span>` (keep the requests count span).
3. **Delete** the per-member `Spend` `<th>`/`<td>` (keep Member + Requests columns).
4. **Delete** the entire "Lifetime Spend" card block (lines 163-172).
5. **Add** above the Token Usage card a mode-switched block:

```tsx
{spend?.mode === "bedrock" && (
  <div className="rounded-lg border border-[#e0dbd0] p-4 space-y-3">
    <div className="flex items-center justify-between">
      <h3 className="text-sm font-medium">Billed this month</h3>
      <span className="text-lg font-semibold font-mono">
        {formatCents(spend.billed_month_to_date_cents ?? 0)}
      </span>
    </div>
    {(spend.by_model ?? []).map((row) => (
      <div key={row.tier} className="flex items-center justify-between text-xs text-[#8a8578]">
        <span>{row.model}</span>
        <span className="font-mono">{formatCents(row.cents)}</span>
      </div>
    ))}
    <p className="text-[10px] text-[#8a8578]/60">
      From your credits invoice — matches what you're charged.
    </p>
  </div>
)}
{spend?.mode === "oauth_quota" && (
  <div className="rounded-lg border border-[#e0dbd0] p-4 space-y-3">
    <div className="flex items-center justify-between">
      <h3 className="text-sm font-medium">ChatGPT plan limits</h3>
      {spend.plan && <span className="text-xs uppercase text-[#8a8578]">{spend.plan}</span>}
    </div>
    {(spend.windows ?? []).map((w) => (
      <div key={w.label} className="space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span>{w.label} limit</span>
          <span className="text-[#8a8578]">
            {Math.round(w.used_percent)}% used
            {w.resets_at_ms ? ` · resets ${new Date(w.resets_at_ms).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}
          </span>
        </div>
        <div className="h-1.5 rounded bg-[#e0dbd0]">
          <div
            className="h-1.5 rounded bg-[#1a1a1a]"
            style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%` }}
          />
        </div>
      </div>
    ))}
    <p className="text-[10px] text-[#8a8578]/60">Included in your ChatGPT plan.</p>
  </div>
)}
```

6. Add helper next to `formatTokens`:

```typescript
function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
```

7. Delete the now-unused `formatDollars` helper.

- [ ] **Step 5: Run test + typecheck, commit**

Run: `cd apps/frontend && pnpm vitest run src/components/control/panels/__tests__/UsagePanel.spend.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, no type errors

```bash
git add apps/frontend/src/hooks/useSpend.ts apps/frontend/src/components/control/panels/
git commit -m "feat(web): provider-aware UsagePanel; drop usage-counter dollar displays"
```

---

### Task 8: Web — settings spend line, dashboard card, Costs panel removal

**Files:**
- Modify: `apps/frontend/src/app/settings/page.tsx` (3 spend lines: 220, 260, 309)
- Modify: `apps/frontend/src/components/teams/panels/DashboardPanel.tsx` (EMPTY_DASHBOARD + Month Spend card)
- Modify: `apps/frontend/src/components/teams/shared/types.ts` (`DashboardCosts`/`DashboardSummary`)
- Delete: `apps/frontend/src/components/teams/panels/CostsPanel.tsx`
- Modify: `apps/frontend/src/components/teams/TeamsPanelRouter.tsx:30` (drop `costs` loader)
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts` (drop `costs: "Costs"` from `TEAMS_LIST_KEYS`; add `"teams-costs": "teams-dashboard"` to `RETIRED_PANEL_ALIASES`)
- Modify: `apps/frontend/src/components/teams/command-palette/commandPaletteActions.ts` (remove the `go-costs` action)
- Test: `apps/frontend/src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx` (create)

**Interfaces:**
- Consumes: `/teams/dashboard` where `costs` is now optional `{ monthSpendCents: number }` (Task 5).
- Produces: no `teams-costs` panel anywhere; stale `?panel=teams-costs` URLs resolve to `teams-dashboard` via the alias map.

- [ ] **Step 1: Type + data changes**

In `types.ts`:

```typescript
export interface DashboardCosts {
  monthSpendCents: number;
}
// ...
export interface DashboardSummary {
  agents: DashboardAgentBuckets;
  tasks: DashboardTaskBuckets;
  runActivity: DashboardRunActivityDay[];
  costs?: DashboardCosts;
  pendingApprovals: number;
  budgets: DashboardBudgets;
}
```

In `DashboardPanel.tsx`: remove `costs` from `EMPTY_DASHBOARD`; replace the Month Spend `MetricCard` block (lines ~293-308) with:

```tsx
{dashboard.costs != null && (
  <MetricCard
    icon={DollarSign}
    value={formatCents(dashboard.costs.monthSpendCents)}
    label="Month Spend"
    to="/chat?panel=usage"
    onClick={(e) => handleShellLinkClick(e, "/chat?panel=usage")}
    description={<span>Billed credits this month</span>}
  />
)}
```

(Verify `handleShellLinkClick` accepts a non-teams panel URL — it routes through the same shell URL grammar the `?panel=llm` links use; if it asserts a teams prefix, use the plain `to` link without the onClick interception.)

- [ ] **Step 2: Deletions**

- Delete `CostsPanel.tsx`; remove the `costs:` entry from `PANEL_LOADERS` in `TeamsPanelRouter.tsx`; remove `costs: "Costs"` from `TEAMS_LIST_KEYS` in `panelRegistry.ts`; add `"teams-costs": "teams-dashboard"` to `RETIRED_PANEL_ALIASES` (same pattern as `"teams-skills": "teams-dashboard"`).
- In `commandPaletteActions.ts`, delete the `go-costs` action object (grep `go-costs`).
- In `settings/page.tsx`, delete all three `Current period spend: … · Lifetime: …` lines (keep the surrounding subscription copy); delete the local `formatDollars` helper if now unused.
- Grep for leftovers: `grep -rn "CostsPanel\|teams-costs\|go-costs" apps/frontend/src` → only the alias entry should remain.

- [ ] **Step 3: Write the dashboard card test**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
// Mock useTeamsApi the way the existing DashboardPanel/teams panel tests do,
// returning a dashboard fixture; assert on card presence.

describe("DashboardPanel month spend card", () => {
  it("renders the card from billed cents when costs present", async () => {
    // fixture: { dashboard: { ..., costs: { monthSpendCents: 1234 } }, sidebar_badges: {} }
    render(<DashboardPanel />);
    expect(await screen.findByText("Month Spend")).toBeInTheDocument();
    expect(screen.getByText("$12.34")).toBeInTheDocument();
  });

  it("hides the card when costs block absent", async () => {
    // fixture without costs
    render(<DashboardPanel />);
    expect(await screen.findByText(/Pending Approvals/)).toBeInTheDocument();
    expect(screen.queryByText("Month Spend")).toBeNull();
  });
});
```

(Fill the mock plumbing from the nearest existing teams-panel test — match its `useTeamsApi`/router mocks exactly.)

- [ ] **Step 4: Run tests + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS; tsc will also flag any missed `CostsPanel`/`DashboardCosts` references — fix them.

- [ ] **Step 5: Commit**

```bash
git add -A apps/frontend/src apps/frontend/src/app/settings/page.tsx
git commit -m "feat(web): re-point month spend to billed credits; remove Costs panel and spend lines"
```

---

### Task 9: Mobile — provider-aware usage screen

**Files:**
- Modify: `apps/mobile/src/app/(app)/control/usage.tsx`
- Modify: `apps/mobile/src/app/(app)/settings.tsx` (delete unused `current_spend`/`lifetime_spend` fields from the local `BillingAccount` interface, lines 31-32)
- Test: `apps/mobile/src/app/(app)/control/__tests__/usage.spend.test.tsx` (create; follow `src/app/(app)/__tests__/chat.test.tsx` conventions for mocking `createApiClient`/Clerk/router)

**Interfaces:**
- Consumes: `GET /billing/spend` (`SpendResponse` wire shape from Task 4) alongside the existing `GET /billing/usage`.
- Produces: mobile usage screen with the same three modes as web; no `$` from usage-counters anywhere.

- [ ] **Step 1: Write the failing test** — mock the api client so `/billing/usage` returns the usage fixture (same shape as the existing `UsageSummary` interface) and `/billing/spend` returns each mode; assert:

```tsx
// tokens_only: no "$" text rendered anywhere; token tiles + request count present
// bedrock: "Billed this month" + "$12.34" + "Claude Opus" rendered
// oauth_quota: "Week limit" + "61%" rendered
// member rows: name + request count rendered, no member dollar value
```

Write the three cases as real RNTL tests using the chat.test.tsx mock scaffolding.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/mobile && pnpm jest src/app/\(app\)/control/__tests__/usage.spend.test.tsx --ci`
Expected: FAIL

- [ ] **Step 3: Implement in `usage.tsx`**

1. Add the `SpendResponse` interface (mirror Task 7's TS shape verbatim).
2. In `useUsageData`, fetch both in parallel; spend failures must not fail the screen:

```typescript
const [usage, spend] = await Promise.all([
  api.get("/billing/usage") as Promise<UsageSummary>,
  (api.get("/billing/spend") as Promise<SpendResponse>).catch(
    () => ({ mode: "tokens_only" }) as SpendResponse,
  ),
]);
if (loadSeq.current !== seq) return;
setState({ kind: "ready", usage, spend });
```

(Extend the `ready` state variant to `{ kind: "ready"; usage: UsageSummary; spend: SpendResponse }`.)

3. Delete the spend card (the `spendHead`/`spendValue` block around lines 232-239: total_spend + "Lifetime · $…") and the per-member `memberSpend` dollar text (line ~265) — keep member name + request count.
4. Render mode blocks above the token tiles, using the existing `Card`/`Tile` building blocks:
   - `bedrock`: a Card with "Billed this month" + `$ (cents/100).toFixed(2)` + one row per `by_model` entry.
   - `oauth_quota`: a Card with per-window rows: `"{label} limit"` + `"{round(used_percent)}% used"` (a simple `View` pair with a width-percentage inner bar, mirroring the web bar).
   - `tokens_only`: nothing extra.
5. Delete the now-unused spend styles (`spendHead`, `spendValue`, `memberSpend`).
6. In `settings.tsx`, delete the two dead interface fields.

- [ ] **Step 4: Run test + typecheck**

Run: `cd apps/mobile && pnpm jest src/app/\(app\)/control/__tests__/usage.spend.test.tsx --ci && pnpm exec tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src
git commit -m "feat(mobile): provider-aware usage screen; drop usage-counter dollars"
```

---

### Task 10: Full verification sweep

**Files:** none new.

- [ ] **Step 1: Backend full suite**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: all pass. Failures in tests you didn't write are still your problem (dependent systems) — fix them.

- [ ] **Step 2: Frontend typecheck + lint + the task-added vitest files (single-file runs only)**

```bash
cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint
pnpm vitest run src/components/control/panels/__tests__/UsagePanel.spend.test.tsx
pnpm vitest run src/components/teams/panels/__tests__/DashboardPanel.costs.test.tsx
```

- [ ] **Step 3: Mobile typecheck + the new jest file**

```bash
cd apps/mobile && pnpm exec tsc --noEmit && pnpm jest src/app/\(app\)/control/__tests__/usage.spend.test.tsx --ci
```

- [ ] **Step 4: Leftover-reference sweep**

```bash
grep -rn "teams-costs\|CostsPanel\|go-costs" apps/frontend/src   # only the RETIRED_PANEL_ALIASES entry
grep -rn "get_costs" apps/backend --include='*.py'               # nothing
grep -rn "Total spend\|Lifetime Spend\|Current period spend" apps/frontend/src apps/mobile/src  # nothing
```

- [ ] **Step 5: Commit any stragglers; STOP for user review**

Do NOT push or open a PR. Report results and wait for explicit approval. Post-merge live validation (from the spec, run on dev with the user): compare `/billing/spend` MTD against the Metronome dashboard draft invoice for a Bedrock owner; compare quota bars against Codex CLI `/status` for an OAuth owner.
