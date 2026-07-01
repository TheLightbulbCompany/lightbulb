# Platform-Ops Surface — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the platform-ops surface foundation — a server-side ops layer + owner-scoped ops API, a live-ECS fleet operator landing at `/admin`, and a deploy/stack-status view — replacing the DDB-derived fleet counts with live ECS reads.

**Architecture:** New `core/services/ops/*` modules read AWS with the backend task role and return a normalized DTO envelope (`{data, freshness_ts, source_state, errors}`). `routers/ops.py` exposes `GET /ops/fleet` + `/ops/deploys` under dual auth (Clerk platform-admin OR a new owner-scoped `platform_ops` token). The Next.js admin surface (Server Components + `adminFetch`) gains a design-system kit (`StatusChip`/`Tile`/`Card`/`MetricStat`), an operator landing (attention strip + live fleet grid), and a `/admin/deploys` page. New IAM: `cloudformation:DescribeStackEvents`+`ListStacks`, `cloudwatch:DescribeAlarms`.

**Tech Stack:** FastAPI + boto3 + pydantic-settings + DynamoDB (backend); AWS CDK / TypeScript (infra); Next.js 16 App Router + React 19 Server Components + Tailwind v4 (frontend); pytest (backend tests), vitest (frontend tests).

## Global Constraints

- **Never modify prod servers directly** — all changes go through version control + CI/CD. New IAM/table/secret land via CDK, deployed by `deploy.yml`.
- **Any CDK stack-structure change regenerates `deploy.yml`** (synth-time drift check at `apps/infra/pipeline.ts`). After every CDK edit run synth and **commit the regenerated `deploy.yml`** — never revert it, or Synthesize fails and skips all deploys.
- **Frontend CI runs `tsc --noEmit` over test files too** — annotate any mock-builder return types; don't rely on vitest stripping types.
- **NEVER run full `pnpm test`** (~17 GB RAM). Run targeted single-file vitest only.
- **No `node_modules` symlink may be committed** — before any frontend commit, run `git ls-tree -r HEAD | awk '$1=="120000"'` and ensure it's empty for the worktree.
- **boto3 clients**: always `boto3.client("<svc>", region_name=settings.AWS_REGION)`; lazy-singleton pattern so import-time works without creds (mirror `cloudwatch_logs.py:33-37`).
- **DynamoDB access**: repositories only, via `core/dynamodb.get_table("<short-name>")` + `run_in_thread(...)`. Table name resolves to `isol8-{env}-<short-name>`.
- **EMF metrics**: `from core.observability.metrics import put_metric, timing`; never pass high-cardinality dimensions (owner_id/user_id are denied).
- **Backend tests run under bogus AWS creds**: `env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest ...` (from `apps/backend`).
- **The `platform_ops` token verifier MUST check BOTH `kind=="platform_ops"` AND `sub==PLATFORM_OPS_OWNER_ID`** — the existing `service_token.verify()` checks neither owner nor a distinct kind, so a tenant `paperclip_service` token must be rejected by `/ops/*`.
- **DTO envelope is mandatory**: no ops read may return empty `data` on failure — a failed/throttled/timed-out source surfaces as `source_state != "ok"` with the domain in `errors[]`. Swallow-to-empty is forbidden.
- **Design surface is the existing DARK admin theme** (zinc base — see `layout.tsx`, `users/page.tsx` use `text-zinc-100`). The operator register is expressed through the semantic status-color system, NOT a cream recolor (reconciles spec §4's "cream/forest" wording with the confirmed dark-admin constraint + the "no recolor" preference). Cream/forest CSS vars in `globals.css` remain the main-app brand.

**Reference spec:** `docs/superpowers/specs/2026-06-30-platform-ops-surface-design.md` (§5 Phase 1 = scope + Done-when).

---

## File Structure

**Backend (create):**
- `apps/backend/core/services/ops/__init__.py` — package marker.
- `apps/backend/core/services/ops/envelope.py` — `OpsEnvelope` + `read_source()` wrapper (timeout, source_state).
- `apps/backend/core/services/ops/ecs_ops.py` — live fleet enumeration + fleet DTO.
- `apps/backend/core/services/ops/deploys.py` — CFN stack status + scoped root-cause.
- `apps/backend/core/services/platform_ops_token.py` — mint/verify (dedicated key, owner-id check).
- `apps/backend/core/repositories/ops_cache_repo.py` — `ops-cache` table get/put(ttl).
- `apps/backend/routers/ops.py` — the ops router + dual-auth dependency.

**Backend (modify):**
- `apps/backend/core/config.py` — add `PLATFORM_OPS_OWNER_ID`, `PLATFORM_OPS_TOKEN_KEY`, `OPS_CACHE_TABLE`.
- `apps/backend/core/services/system_health.py` — `_fleet_counts` reads live ECS (via `ecs_ops`), keeps return shape.
- `apps/backend/main.py` — register `ops.router`.

**Infra (modify):**
- `apps/infra/lib/stacks/database-stack.ts` — `ops-cache` TTL table.
- `apps/infra/lib/stacks/auth-stack.ts` — `platform_ops_token_key` secret.
- `apps/infra/lib/stacks/service-stack.ts` — new IAM statements + env vars + secret injection + `SecretNames` field.
- `.github/workflows/deploy.yml` — regenerated by synth (committed, not hand-edited).

**Frontend (create):**
- `apps/frontend/src/components/admin/StatusChip.tsx`, `Tile.tsx`, `Card.tsx`, `MetricStat.tsx` — design-system kit.
- `apps/frontend/src/components/admin/AutoRefresh.tsx` — client interval `router.refresh()`.
- `apps/frontend/src/lib/aws.ts` — `AWS_REGION` constant (env-overridable).
- `apps/frontend/src/app/admin/deploys/page.tsx` — deploy/stack view.
- `apps/frontend/src/app/admin/audit/page.tsx` — platform-wide audit view.
- `apps/frontend/.interface-design/system.md` — saved design system (committed under repo).

**Frontend (modify):**
- `apps/frontend/src/app/admin/_lib/api.ts` — ops read helpers + types (`getFleet`, `getDeploys`), `region` on `ContainerSlice`.
- `apps/frontend/src/app/admin/page.tsx` — operator landing (replaces redirect).
- `apps/frontend/src/app/admin/layout.tsx` — nav links (Overview, Deploys, Audit, Logs).
- `apps/frontend/src/app/admin/users/[id]/container/page.tsx:88` + `ContainerActionsPanel.tsx:289` — region single-source.

---

## Task 1: Config settings for platform-ops

**Files:**
- Modify: `apps/backend/core/config.py` (after the `PAPERCLIP_SERVICE_TOKEN_KEY` block, ~line 263)
- Test: `apps/backend/tests/unit/core/test_config_platform_ops.py`

**Interfaces:**
- Produces: `settings.PLATFORM_OPS_OWNER_ID: str`, `settings.PLATFORM_OPS_TOKEN_KEY: str`, `settings.OPS_CACHE_TABLE: str` — consumed by Tasks 2, 3, 5, 6, 7.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/core/test_config_platform_ops.py
from core.config import settings


def test_platform_ops_settings_exist_with_defaults():
    # Fields must exist and default to empty string (unset in local/dev tests).
    assert hasattr(settings, "PLATFORM_OPS_OWNER_ID")
    assert hasattr(settings, "PLATFORM_OPS_TOKEN_KEY")
    assert hasattr(settings, "OPS_CACHE_TABLE")
    assert isinstance(settings.PLATFORM_OPS_OWNER_ID, str)
    assert isinstance(settings.PLATFORM_OPS_TOKEN_KEY, str)
    assert isinstance(settings.OPS_CACHE_TABLE, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/test_config_platform_ops.py -v`
Expected: FAIL — `AttributeError` on the new attributes.

- [ ] **Step 3: Add the settings**

```python
# In apps/backend/core/config.py, after PAPERCLIP_SERVICE_TOKEN_KEY (~line 263):

    # --- Platform Ops (operator console + owner-scoped ops API) ---
    # The founder's owner_id (user_* or org_*). The platform_ops token's
    # `sub` must equal this; it gates the agent path on /ops/*.
    PLATFORM_OPS_OWNER_ID: str = os.getenv("PLATFORM_OPS_OWNER_ID", "")
    # Dedicated HS256 key for signing/verifying platform_ops tokens. Distinct
    # from PAPERCLIP_SERVICE_TOKEN_KEY so ops tokens are independently
    # revocable. Populated from Secrets Manager + KMS by the CDK service stack.
    PLATFORM_OPS_TOKEN_KEY: str = os.getenv("PLATFORM_OPS_TOKEN_KEY", "")
    # DynamoDB table for the shared ops cache (fleet/cost/metrics snapshots),
    # set by CDK. Empty in local tests.
    OPS_CACHE_TABLE: str = os.getenv("OPS_CACHE_TABLE", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/core/test_config_platform_ops.py
git commit -m "feat(ops): add platform-ops config settings"
```

---

## Task 2: `platform_ops` token (mint/verify with dedicated key + owner-id check)

**Files:**
- Create: `apps/backend/core/services/platform_ops_token.py`
- Test: `apps/backend/tests/unit/core/services/test_platform_ops_token.py`

**Interfaces:**
- Consumes: `os.environ["PLATFORM_OPS_TOKEN_KEY"]`.
- Produces:
  - `PLATFORM_OPS_TOKEN_KIND = "platform_ops"`
  - `mint(owner_id: str, ttl_days: int = 365) -> str`
  - `verify(token: str, expected_owner_id: str) -> dict` — raises `jwt.InvalidTokenError` on wrong kind, empty sub, or `sub != expected_owner_id`; raises `jwt.ExpiredSignatureError` when expired. Consumed by `routers/ops.py` (Task 7).

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/core/services/test_platform_ops_token.py
import os
import jwt
import pytest

# Ensure a dedicated key is present BEFORE importing the module under test.
os.environ.setdefault("PLATFORM_OPS_TOKEN_KEY", "test-ops-key-abc")
os.environ.setdefault("PAPERCLIP_SERVICE_TOKEN_KEY", "test-paperclip-key-xyz")

from core.services import platform_ops_token
from core.services import service_token


def test_mint_and_verify_roundtrip():
    tok = platform_ops_token.mint("org_founder")
    claims = platform_ops_token.verify(tok, expected_owner_id="org_founder")
    assert claims["kind"] == "platform_ops"
    assert claims["sub"] == "org_founder"
    assert "jti" in claims


def test_verify_rejects_wrong_owner():
    tok = platform_ops_token.mint("org_founder")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(tok, expected_owner_id="org_someone_else")


def test_verify_rejects_paperclip_service_token():
    # THE hole this closes: a tenant's paperclip_service token must NOT be
    # accepted by the ops verifier, even for the founder's owner_id, because
    # it is signed with a different key AND carries the wrong kind.
    tenant_tok = service_token.mint("org_founder")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(tenant_tok, expected_owner_id="org_founder")


def test_verify_rejects_wrong_kind_same_key():
    # A token signed with the ops key but the wrong kind is rejected.
    import datetime as _dt

    payload = {
        "sub": "org_founder",
        "kind": "not_platform_ops",
        "iat": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
        "exp": int((_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)).timestamp()),
    }
    bad = jwt.encode(payload, os.environ["PLATFORM_OPS_TOKEN_KEY"], algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(bad, expected_owner_id="org_founder")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/test_platform_ops_token.py -v`
Expected: FAIL — `ModuleNotFoundError: core.services.platform_ops_token`.

- [ ] **Step 3: Implement the module**

```python
# apps/backend/core/services/platform_ops_token.py
"""platform_ops service tokens.

Owner-scoped JWTs the backend mints so the founder's agent (Friday) can call
the ops API from its container over outbound HTTPS. Distinct from
service_token.py (paperclip_service) in three deliberate ways:
  1. A dedicated signing key (PLATFORM_OPS_TOKEN_KEY) — independently revocable.
  2. A distinct `kind` ("platform_ops") — a tenant paperclip_service token is
     rejected on the ops path.
  3. verify() enforces `sub == expected_owner_id` — the token is bound to the
     single platform owner, so it grants nothing cross-tenant.

Format: HS256 JWT. Claims: sub (owner_id), kind, iat, exp, jti.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

PLATFORM_OPS_TOKEN_KIND = "platform_ops"
DEFAULT_TTL_DAYS = 365


def _signing_key() -> str:
    key = os.environ.get("PLATFORM_OPS_TOKEN_KEY")
    if not key:
        raise RuntimeError("PLATFORM_OPS_TOKEN_KEY env var not set")
    return key


def mint(owner_id: str, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """Mint a platform_ops token bound to owner_id."""
    if not owner_id:
        raise ValueError("owner_id is required")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": owner_id,
        "kind": PLATFORM_OPS_TOKEN_KIND,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify(token: str, expected_owner_id: str) -> dict:
    """Verify a platform_ops token bound to expected_owner_id.

    Raises jwt.ExpiredSignatureError on expiry, jwt.InvalidTokenError on wrong
    kind / empty sub / owner mismatch / bad signature.
    """
    claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    if claims.get("kind") != PLATFORM_OPS_TOKEN_KIND:
        raise jwt.InvalidTokenError(f"Wrong kind claim: {claims.get('kind')!r}")
    sub = claims.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("Missing sub claim")
    if not expected_owner_id or sub != expected_owner_id:
        raise jwt.InvalidTokenError("Token sub does not match the platform owner")
    return claims
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/platform_ops_token.py apps/backend/tests/unit/core/services/test_platform_ops_token.py
git commit -m "feat(ops): platform_ops token with dedicated key + owner binding"
```

---

## Task 3: `ops-cache` repository

**Files:**
- Create: `apps/backend/core/repositories/ops_cache_repo.py`
- Test: `apps/backend/tests/unit/core/repositories/test_ops_cache_repo.py`

**Interfaces:**
- Consumes: `core.dynamodb.get_table("ops-cache")`, `run_in_thread`.
- Produces:
  - `async get(cache_key: str) -> dict | None` — returns the stored item (`{cache_key, value, ttl, updated_at}`) or None.
  - `async put(cache_key: str, value: dict, ttl_seconds: int = 30) -> dict`
  - Consumed by `ecs_ops` (Task 5) for the short-TTL fleet cache.

- [ ] **Step 1: Write the failing test (mock the table)**

```python
# apps/backend/tests/unit/core/repositories/test_ops_cache_repo.py
import time
from unittest.mock import MagicMock, patch

import pytest

from core.repositories import ops_cache_repo


@pytest.mark.asyncio
async def test_put_then_get_roundtrip():
    store: dict = {}

    fake_table = MagicMock()

    def _put_item(Item):
        store[Item["cache_key"]] = Item

    def _get_item(Key):
        item = store.get(Key["cache_key"])
        return {"Item": item} if item else {}

    fake_table.put_item.side_effect = _put_item
    fake_table.get_item.side_effect = _get_item

    with patch.object(ops_cache_repo, "_get_table", return_value=fake_table):
        before = int(time.time())
        put = await ops_cache_repo.put("fleet#dev", {"running": 3}, ttl_seconds=30)
        assert put["value"] == {"running": 3}
        assert put["ttl"] >= before + 30

        got = await ops_cache_repo.get("fleet#dev")
        assert got is not None
        assert got["value"] == {"running": 3}

        missing = await ops_cache_repo.get("nope")
        assert missing is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/repositories/test_ops_cache_repo.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the repo**

```python
# apps/backend/core/repositories/ops_cache_repo.py
"""Ops cache repository — DynamoDB `ops-cache` table.

A generic shared cache keyed by an opaque cache_key string, holding a JSON
`value` blob with a DynamoDB `ttl` for auto-expiry. Backs the P1 fleet
short-TTL cache and (later phases) cost/metrics snapshots.
"""

from __future__ import annotations

import time

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("ops-cache")  # -> isol8-{env}-ops-cache


async def get(cache_key: str) -> dict | None:
    table = _get_table()
    resp = await run_in_thread(table.get_item, Key={"cache_key": cache_key})
    return resp.get("Item")


async def put(cache_key: str, value: dict, ttl_seconds: int = 30) -> dict:
    table = _get_table()
    item = {
        "cache_key": cache_key,
        "value": value,
        "ttl": int(time.time()) + ttl_seconds,
        "updated_at": utc_now_iso(),
    }
    await run_in_thread(table.put_item, Item=item)
    return item
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/ops_cache_repo.py apps/backend/tests/unit/core/repositories/test_ops_cache_repo.py
git commit -m "feat(ops): ops-cache repository (ttl-keyed shared cache)"
```

---

## Task 4: Ops DTO envelope + `read_source` wrapper

**Files:**
- Create: `apps/backend/core/services/ops/__init__.py` (empty)
- Create: `apps/backend/core/services/ops/envelope.py`
- Test: `apps/backend/tests/unit/core/services/ops/test_envelope.py`

**Interfaces:**
- Produces:
  - `SourceState` = literal strings `"ok" | "degraded" | "stale" | "unavailable" | "partial"`.
  - `def envelope(data, *, source_state="ok", errors=None, freshness_ts=None) -> dict` → `{data, freshness_ts, source_state, errors}`.
  - `async read_source(name: str, coro_factory, *, timeout_s: float = 8.0) -> tuple[object, str, list]` → returns `(data, source_state, errors)`; on exception/timeout returns `(None, "unavailable", [{domain, reason}])` and emits an EMF error metric. NEVER raises. Consumed by `ecs_ops`, `deploys`, `routers/ops.py` (Tasks 5, 6, 7).

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/core/services/ops/test_envelope.py
import asyncio

import pytest

from core.services.ops import envelope as env


def test_envelope_shape_ok():
    e = env.envelope({"running": 3}, freshness_ts="2026-06-30T00:00:00Z")
    assert e["data"] == {"running": 3}
    assert e["source_state"] == "ok"
    assert e["errors"] == []
    assert e["freshness_ts"] == "2026-06-30T00:00:00Z"


@pytest.mark.asyncio
async def test_read_source_success():
    async def ok():
        return {"n": 1}

    data, state, errors = await env.read_source("ecs", ok)
    assert data == {"n": 1}
    assert state == "ok"
    assert errors == []


@pytest.mark.asyncio
async def test_read_source_swallows_exception_as_unavailable():
    async def boom():
        raise RuntimeError("throttled")

    data, state, errors = await env.read_source("ecs", boom)
    assert data is None
    assert state == "unavailable"
    assert errors and errors[0]["domain"] == "ecs"
    assert "throttled" in errors[0]["reason"]


@pytest.mark.asyncio
async def test_read_source_timeout():
    async def slow():
        await asyncio.sleep(1)
        return {"n": 1}

    data, state, errors = await env.read_source("ecs", slow, timeout_s=0.01)
    assert data is None
    assert state == "unavailable"
    assert errors and errors[0]["domain"] == "ecs"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/ops/test_envelope.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement envelope + read_source**

```python
# apps/backend/core/services/ops/__init__.py
```

```python
# apps/backend/core/services/ops/envelope.py
"""Ops DTO envelope + a fail-safe source-read wrapper.

Every ops read returns {data, freshness_ts, source_state, errors}. A failed,
throttled, or timed-out read surfaces as source_state != "ok" with the failing
domain in errors[] — NEVER as empty data that renders 'all clear'. This is the
contract that prevents silent false-negatives on the operator console.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Semantic states — kept as plain strings for JSON transport.
OK = "ok"
DEGRADED = "degraded"
STALE = "stale"
UNAVAILABLE = "unavailable"
PARTIAL = "partial"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def envelope(
    data: Any,
    *,
    source_state: str = OK,
    errors: list[dict] | None = None,
    freshness_ts: str | None = None,
) -> dict:
    return {
        "data": data,
        "freshness_ts": freshness_ts or _now_iso(),
        "source_state": source_state,
        "errors": errors or [],
    }


async def read_source(
    domain: str,
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    timeout_s: float = 8.0,
) -> tuple[Any, str, list[dict]]:
    """Run one source read with a timeout; never raise.

    Returns (data, source_state, errors). On any failure returns
    (None, "unavailable", [{domain, reason}]) and emits an EMF error metric.
    """
    try:
        data = await asyncio.wait_for(coro_factory(), timeout=timeout_s)
        return data, OK, []
    except asyncio.TimeoutError:
        put_metric("ops.source.error", dimensions={"domain": domain, "reason": "timeout"})
        logger.warning("ops source %s timed out after %.1fs", domain, timeout_s)
        return None, UNAVAILABLE, [{"domain": domain, "reason": f"timeout after {timeout_s}s"}]
    except Exception as e:  # noqa: BLE001 — fail-safe by contract
        put_metric("ops.source.error", dimensions={"domain": domain, "reason": "exception"})
        logger.warning("ops source %s failed: %s", domain, e)
        return None, UNAVAILABLE, [{"domain": domain, "reason": str(e)}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ops/__init__.py apps/backend/core/services/ops/envelope.py apps/backend/tests/unit/core/services/ops/test_envelope.py
git commit -m "feat(ops): DTO envelope + fail-safe read_source wrapper"
```

---

## Task 5: Live-ECS fleet reads (`ecs_ops`)

**Files:**
- Create: `apps/backend/core/services/ops/ecs_ops.py`
- Test: `apps/backend/tests/unit/core/services/ops/test_ecs_ops.py`

**Interfaces:**
- Consumes: `container_repo.scan_all()` (fleet index — owner_id/service_name/status rows), a boto3 ECS client, `ops_cache_repo`, `envelope`.
- Produces:
  - `async fleet_counts_live() -> dict[str, int]` → `{running, provisioning, stopped, error, unknown, total}` — a drop-in for `system_health._fleet_counts` return shape (adds `unknown`; total = sum of all).
  - `async fleet_grid() -> list[dict]` → per-container `{owner_id, service_name, status, running_count, desired_count, pending_count, rollout_state, health}` for the landing fleet grid.
  - Both read through the `ops-cache` (`fleet#{env}` key, ~20s TTL). Consumed by Tasks 7 (router) and 8 (system_health).

**Notes for the implementer:**
- ECS `describe_services` accepts **≤10 services per call** — batch. `describe_tasks` accepts **≤100 tasks** — batch. Use the cluster ARN from `settings.ECS_CLUSTER_ARN`.
- Health mapping: a service is `running` when a single `PRIMARY` deployment has `rolloutState=="COMPLETED"` and `runningCount>=desiredCount>=1`; `provisioning` when `rolloutState=="IN_PROGRESS"` or `pendingCount>0`; `error` when `rolloutState=="FAILED"` or the DDB row status is `error`; `stopped` when `desiredCount==0`; else `unknown`.
- Wrap boto3 calls in `asyncio.to_thread` (they're blocking), mirroring `ecs_manager.py:962`.

- [ ] **Step 1: Write the failing tests (mock ECS client + repo + cache)**

```python
# apps/backend/tests/unit/core/services/ops/test_ecs_ops.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.ops import ecs_ops


def _svc(name, desired, running, pending, rollout):
    return {
        "serviceName": name,
        "desiredCount": desired,
        "runningCount": running,
        "pendingCount": pending,
        "deployments": [{"status": "PRIMARY", "rolloutState": rollout}],
    }


@pytest.mark.asyncio
async def test_fleet_grid_maps_health():
    rows = [
        {"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"},
        {"owner_id": "org_b", "service_name": "openclaw-b-2", "status": "error"},
    ]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {
        "services": [
            _svc("openclaw-a-1", 1, 1, 0, "COMPLETED"),
            _svc("openclaw-b-2", 1, 0, 0, "FAILED"),
        ]
    }

    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=None)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.put", AsyncMock()),
    ):
        grid = await ecs_ops.fleet_grid()

    by_owner = {g["owner_id"]: g for g in grid}
    assert by_owner["org_a"]["status"] == "running"
    assert by_owner["org_b"]["status"] == "error"


@pytest.mark.asyncio
async def test_fleet_counts_live_shape():
    rows = [
        {"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"},
    ]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {
        "services": [_svc("openclaw-a-1", 1, 1, 0, "COMPLETED")]
    }
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=None)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.put", AsyncMock()),
    ):
        counts = await ecs_ops.fleet_counts_live()

    assert set(counts) == {"running", "provisioning", "stopped", "error", "unknown", "total"}
    assert counts["running"] == 1
    assert counts["total"] == 1


@pytest.mark.asyncio
async def test_fleet_grid_served_from_cache_when_fresh():
    cached = {"value": [{"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"}]}
    with (
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=cached)),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock()) as scan,
    ):
        grid = await ecs_ops.fleet_grid()
    assert grid[0]["owner_id"] == "org_a"
    scan.assert_not_called()  # cache hit → no live ECS enumeration
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/ops/test_ecs_ops.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `ecs_ops`**

```python
# apps/backend/core/services/ops/ecs_ops.py
"""Live-ECS fleet reads for the operator console.

Enumerates the fleet from the DDB container index, enriches with live
describe_services (≤10/call), maps to health, and serves through the
ops-cache (~20s TTL). Replaces the DDB-derived counts in system_health.
"""

from __future__ import annotations

import asyncio

import boto3

from core.config import settings
from core.repositories import container_repo, ops_cache_repo

_FLEET_CACHE_TTL_S = 20
_DESCRIBE_SERVICES_BATCH = 10

_client = None


def _ecs():
    global _client
    if _client is None:
        _client = boto3.client("ecs", region_name=settings.AWS_REGION)
    return _client


def _cache_key() -> str:
    return f"fleet#{settings.ENVIRONMENT or 'dev'}"


def _classify(svc: dict, ddb_status: str) -> str:
    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)
    pending = svc.get("pendingCount", 0)
    primary = next(
        (d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"),
        None,
    )
    rollout = primary.get("rolloutState") if primary else None
    if ddb_status == "error" or rollout == "FAILED":
        return "error"
    if desired == 0:
        return "stopped"
    if rollout == "IN_PROGRESS" or pending > 0:
        return "provisioning"
    if rollout == "COMPLETED" and running >= desired >= 1:
        return "running"
    return "unknown"


async def _describe(service_names: list[str]) -> dict[str, dict]:
    """Return {service_name: service_dict} via batched describe_services."""
    out: dict[str, dict] = {}
    for i in range(0, len(service_names), _DESCRIBE_SERVICES_BATCH):
        batch = service_names[i : i + _DESCRIBE_SERVICES_BATCH]
        resp = await asyncio.to_thread(
            _ecs().describe_services,
            cluster=settings.ECS_CLUSTER_ARN,
            services=batch,
        )
        for svc in resp.get("services", []):
            out[svc.get("serviceName")] = svc
    return out


async def _build_grid() -> list[dict]:
    rows = await container_repo.scan_all()
    named = [r for r in rows if r.get("service_name")]
    services = await _describe([r["service_name"] for r in named])
    grid: list[dict] = []
    for r in named:
        svc = services.get(r["service_name"], {})
        status = _classify(svc, r.get("status", ""))
        grid.append(
            {
                "owner_id": r.get("owner_id"),
                "service_name": r["service_name"],
                "status": status,
                "desired_count": svc.get("desiredCount", 0),
                "running_count": svc.get("runningCount", 0),
                "pending_count": svc.get("pendingCount", 0),
                "rollout_state": next(
                    (d.get("rolloutState") for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"),
                    None,
                ),
            }
        )
    return grid


async def fleet_grid() -> list[dict]:
    cached = await ops_cache_repo.get(_cache_key())
    if cached is not None and "value" in cached:
        return cached["value"]
    grid = await _build_grid()
    await ops_cache_repo.put(_cache_key(), grid, ttl_seconds=_FLEET_CACHE_TTL_S)
    return grid


async def fleet_counts_live() -> dict[str, int]:
    grid = await fleet_grid()
    counts = {"running": 0, "provisioning": 0, "stopped": 0, "error": 0, "unknown": 0}
    for g in grid:
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    counts["total"] = sum(counts.values())
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ops/ecs_ops.py apps/backend/tests/unit/core/services/ops/test_ecs_ops.py
git commit -m "feat(ops): live-ECS fleet reads with short-TTL cache"
```

---

## Task 6: CFN deploy/stack status (`deploys`)

**Files:**
- Create: `apps/backend/core/services/ops/deploys.py`
- Test: `apps/backend/tests/unit/core/services/ops/test_deploys.py`

**Interfaces:**
- Consumes: a boto3 CloudFormation client.
- Produces:
  - `async list_stacks() -> list[dict]` → per-stack `{stack_name, status, status_reason, last_updated, root_cause}` for the isol8 stacks. `root_cause` is populated (via `_root_cause`) only when `status` ends with `_FAILED` or is `ROLLBACK_COMPLETE`.
  - `async _root_cause(stack_name: str) -> str | None` → walks `describe_stack_events` newest-first, bounded to the current operation window, returns the earliest `*_FAILED` `ResourceStatusReason` within it.
  - Consumed by Task 7 (router).

**Notes for the implementer:**
- `ROLLBACK_COMPLETE` = create failed + rolled back (only deletable) — classify as failed.
- The event walk must NOT return a months-old prior failure: page newest-first, stop when you cross the boundary of the current operation (the first event whose `ResourceType == "AWS::CloudFormation::Stack"` and `ResourceStatus` ends with `_IN_PROGRESS`), and return the earliest `*_FAILED` seen within that window. Cap at 5 pages.
- Filter to isol8 stacks: name starts with `isol8-{env}-` (via `settings.ENVIRONMENT`).

- [ ] **Step 1: Write the failing tests (mock CFN client)**

```python
# apps/backend/tests/unit/core/services/ops/test_deploys.py
from unittest.mock import MagicMock, patch

import pytest

from core.services.ops import deploys


@pytest.mark.asyncio
async def test_list_stacks_classifies_and_finds_root_cause():
    fake = MagicMock()
    fake.describe_stacks.return_value = {
        "Stacks": [
            {"StackName": "isol8-dev-service", "StackStatus": "UPDATE_ROLLBACK_COMPLETE", "LastUpdatedTime": None},
            {"StackName": "isol8-dev-api", "StackStatus": "UPDATE_COMPLETE", "LastUpdatedTime": None},
            {"StackName": "other-stack", "StackStatus": "CREATE_COMPLETE", "LastUpdatedTime": None},
        ]
    }
    # Newest-first events: an IN_PROGRESS stack event marks the operation start.
    fake.describe_stack_events.return_value = {
        "StackEvents": [
            {"ResourceType": "AWS::ECS::Service", "ResourceStatus": "UPDATE_FAILED", "ResourceStatusReason": "task failed to start"},
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_IN_PROGRESS", "ResourceStatusReason": "User initiated"},
            {"ResourceType": "AWS::ECS::Service", "ResourceStatus": "UPDATE_FAILED", "ResourceStatusReason": "OLD failure from months ago"},
        ]
    }
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()

    by_name = {r["stack_name"]: r for r in rows}
    # non-isol8 stack filtered out
    assert "other-stack" not in by_name
    assert by_name["isol8-dev-api"]["root_cause"] is None
    failed = by_name["isol8-dev-service"]
    assert failed["status"] == "UPDATE_ROLLBACK_COMPLETE"
    # root cause is the failure WITHIN the current operation window, not the old one
    assert failed["root_cause"] == "task failed to start"


@pytest.mark.asyncio
async def test_list_stacks_empty_when_no_isol8_stacks():
    fake = MagicMock()
    fake.describe_stacks.return_value = {"Stacks": [{"StackName": "unrelated", "StackStatus": "CREATE_COMPLETE"}]}
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()
    assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/ops/test_deploys.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `deploys`**

```python
# apps/backend/core/services/ops/deploys.py
"""CloudFormation deploy/stack status for the operator console.

Reports per-stack status and, for failed/rolled-back stacks, the root-cause
failure WITHIN the current operation window (not a stale prior failure).
"""

from __future__ import annotations

import asyncio

import boto3

from core.config import settings

_MAX_EVENT_PAGES = 5
_client = None


def _cfn():
    global _client
    if _client is None:
        _client = boto3.client("cloudformation", region_name=settings.AWS_REGION)
    return _client


def _is_failed(status: str) -> bool:
    return status.endswith("_FAILED") or status == "ROLLBACK_COMPLETE" or status.endswith("ROLLBACK_COMPLETE")


async def _root_cause(stack_name: str) -> str | None:
    """Earliest *_FAILED ResourceStatusReason within the current operation."""
    earliest_reason: str | None = None
    next_token: str | None = None
    pages = 0
    while pages < _MAX_EVENT_PAGES:
        kwargs = {"StackName": stack_name}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = await asyncio.to_thread(_cfn().describe_stack_events, **kwargs)
        for ev in resp.get("StackEvents", []):  # newest-first
            rtype = ev.get("ResourceType")
            rstatus = ev.get("ResourceStatus", "")
            # Boundary of the current operation: the stack's own *_IN_PROGRESS.
            if rtype == "AWS::CloudFormation::Stack" and rstatus.endswith("_IN_PROGRESS"):
                return earliest_reason
            if rstatus.endswith("_FAILED"):
                # newest-first walk → keep overwriting so we end with the earliest
                earliest_reason = ev.get("ResourceStatusReason") or earliest_reason
        next_token = resp.get("NextToken")
        pages += 1
        if not next_token:
            break
    return earliest_reason


async def list_stacks() -> list[dict]:
    prefix = f"isol8-{settings.ENVIRONMENT or 'dev'}-"
    resp = await asyncio.to_thread(_cfn().describe_stacks)
    rows: list[dict] = []
    for st in resp.get("Stacks", []):
        name = st.get("StackName", "")
        if not name.startswith(prefix):
            continue
        status = st.get("StackStatus", "")
        root_cause = await _root_cause(name) if _is_failed(status) else None
        last_updated = st.get("LastUpdatedTime")
        rows.append(
            {
                "stack_name": name,
                "status": status,
                "status_reason": st.get("StackStatusReason"),
                "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else None,
                "root_cause": root_cause,
            }
        )
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ops/deploys.py apps/backend/tests/unit/core/services/ops/test_deploys.py
git commit -m "feat(ops): CFN deploy status + scoped root-cause"
```

---

## Task 7: `routers/ops.py` — dual-auth + `/ops/fleet` + `/ops/deploys` + token mint

**Files:**
- Create: `apps/backend/routers/ops.py`
- Modify: `apps/backend/main.py` (import + register)
- Test: `apps/backend/tests/unit/routers/test_ops.py`

**Interfaces:**
- Consumes: `ecs_ops.fleet_grid`/`fleet_counts_live`, `deploys.list_stacks`, `envelope.read_source`/`envelope.envelope`, `platform_ops_token.mint`/`verify`, `require_platform_admin`, `get_current_user`, `resolve_owner_id`, `settings.PLATFORM_OPS_OWNER_ID`.
- Produces routes (all under `/api/v1/ops`):
  - `GET /ops/fleet` → envelope over `{counts, grid}` (dual auth).
  - `GET /ops/deploys` → envelope over `list_stacks()` (dual auth).
  - `POST /ops/token` → mint a `platform_ops` token for `PLATFORM_OPS_OWNER_ID` (Clerk platform-admin ONLY).

**Dual-auth dependency (`require_ops_access`):** accept the request if EITHER (a) the bearer is a Clerk platform-admin JWT (`require_platform_admin` succeeds) OR (b) the bearer is a valid `platform_ops` token whose `sub == settings.PLATFORM_OPS_OWNER_ID`. Default-deny: if neither path validates, raise 401. The agent path must be tried WITHOUT letting a bad Clerk token 401 short-circuit the platform_ops attempt.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_ops.py
import os

os.environ.setdefault("PLATFORM_OPS_TOKEN_KEY", "test-ops-key-abc")
os.environ.setdefault("PAPERCLIP_SERVICE_TOKEN_KEY", "test-paperclip-key-xyz")

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.services import platform_ops_token, service_token
import routers.ops as ops_router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_OPS_OWNER_ID", "org_founder")
    app = FastAPI()
    app.include_router(ops_router.router, prefix="/api/v1/ops")
    return TestClient(app, raise_server_exceptions=False)


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_fleet_accepts_platform_ops_token(client):
    tok = platform_ops_token.mint("org_founder")
    with (
        patch.object(ops_router.ecs_ops, "fleet_counts_live", AsyncMock(return_value={"total": 1})),
        patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=[{"owner_id": "org_a"}])),
    ):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] == "ok"
    assert body["data"]["counts"]["total"] == 1


def test_fleet_rejects_tenant_paperclip_token(client):
    tenant = service_token.mint("org_founder")  # ubiquitous tenant token
    res = client.get("/api/v1/ops/fleet", headers=_auth(tenant))
    assert res.status_code == 401


def test_fleet_rejects_platform_ops_token_for_wrong_owner(client):
    tok = platform_ops_token.mint("org_not_founder")
    res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 401


def test_fleet_degrades_when_source_unavailable(client):
    tok = platform_ops_token.mint("org_founder")
    with (
        patch.object(ops_router.ecs_ops, "fleet_counts_live", AsyncMock(side_effect=RuntimeError("throttled"))),
        patch.object(ops_router.ecs_ops, "fleet_grid", AsyncMock(return_value=[])),
    ):
        res = client.get("/api/v1/ops/fleet", headers=_auth(tok))
    assert res.status_code == 200
    body = res.json()
    assert body["source_state"] != "ok"          # NOT rendered as all-clear
    assert body["errors"] and body["errors"][0]["domain"] == "ecs"
```

> Note: the Clerk-admin path is validated by `require_platform_admin`, which needs a real Clerk JWT + JWKS; that path is covered by existing admin router tests. These tests exercise the platform_ops path + the reject cases, which are the new security-critical surface.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/routers/test_ops.py -v`
Expected: FAIL — `routers.ops` missing.

- [ ] **Step 3: Implement the router**

```python
# apps/backend/routers/ops.py
"""Platform-ops API — operator console + owner-scoped agent access.

Dual auth: a Clerk platform-admin JWT (the admin UI) OR a platform_ops token
bound to settings.PLATFORM_OPS_OWNER_ID (the founder's agent). Default-deny.
"""

from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import get_current_user, require_platform_admin, resolve_owner_id
from core.config import settings
from core.services import platform_ops_token
from core.services.ops import deploys, ecs_ops
from core.services.ops import envelope as env

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


async def require_ops_access(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Allow Clerk platform-admins OR the platform_ops owner token.

    Returns a short principal label ("admin" | "platform_ops"). Default-deny.
    """
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing credentials")
    token = creds.credentials

    # Agent path first — cheap local HS256 verify, no network.
    owner = settings.PLATFORM_OPS_OWNER_ID
    if owner:
        try:
            platform_ops_token.verify(token, expected_owner_id=owner)
            return "platform_ops"
        except (jwt.InvalidTokenError, jwt.ExpiredSignatureError, RuntimeError):
            pass  # fall through to the admin path

    # Admin path — Clerk JWT. get_current_user raises 401 on invalid tokens.
    try:
        auth = await get_current_user(credentials=creds)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid ops credentials")
    return require_platform_admin(auth) and "admin"


@router.get("/fleet")
async def get_fleet(_principal: str = Depends(require_ops_access)):
    counts, cstate, cerr = await env.read_source("ecs", ecs_ops.fleet_counts_live)
    grid, gstate, gerr = await env.read_source("ecs", ecs_ops.fleet_grid)
    errors = cerr + gerr
    state = env.OK if not errors else env.PARTIAL if (counts or grid) else env.UNAVAILABLE
    return env.envelope({"counts": counts, "grid": grid}, source_state=state, errors=errors)


@router.get("/deploys")
async def get_deploys(_principal: str = Depends(require_ops_access)):
    data, state, errors = await env.read_source("cloudformation", deploys.list_stacks)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.post("/token")
async def mint_ops_token(auth=Depends(require_platform_admin)):
    """Mint a platform_ops token for the configured platform owner.

    Admin-only (NOT self-renewal by a bare ops token). Delivered to the
    founder's container as a Secrets-Manager env var out of band.
    """
    if not settings.PLATFORM_OPS_OWNER_ID:
        raise HTTPException(status_code=409, detail="PLATFORM_OPS_OWNER_ID not configured")
    token = platform_ops_token.mint(settings.PLATFORM_OPS_OWNER_ID)
    return {"token": token, "owner_id": settings.PLATFORM_OPS_OWNER_ID, "kind": "platform_ops"}
```

- [ ] **Step 4: Register the router in main.py**

```python
# In apps/backend/main.py import block (~line 32-61), add `ops` to the
# `from routers import (...)` tuple (alphabetically, after `oauth`).

# In the registration section (~line 764, near hooks_proxy), add:
app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/routers/test_ops.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/ops.py apps/backend/main.py apps/backend/tests/unit/routers/test_ops.py
git commit -m "feat(ops): /ops/fleet + /ops/deploys + token mint (dual auth)"
```

---

## Task 8: Swap `system_health._fleet_counts` to live ECS (keep shape)

**Files:**
- Modify: `apps/backend/core/services/system_health.py:114-129`
- Test: `apps/backend/tests/unit/core/services/test_system_health_fleet.py`

**Interfaces:**
- Consumes: `ecs_ops.fleet_counts_live` (Task 5).
- Produces: `_fleet_counts()` now returns live counts; `get_system_health()` return shape UNCHANGED (`fleet` still has `running/provisioning/stopped/error/total`, plus a new `unknown` key). Existing caller `GET /admin/system/health` must keep working (degrades to `{}` on error, per existing `_safe`).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/core/services/test_system_health_fleet.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import system_health


@pytest.mark.asyncio
async def test_fleet_counts_uses_live_ecs():
    with patch(
        "core.services.system_health.ecs_ops.fleet_counts_live",
        AsyncMock(return_value={"running": 2, "provisioning": 0, "stopped": 1, "error": 0, "unknown": 0, "total": 3}),
    ):
        counts = await system_health._fleet_counts()
    assert counts["running"] == 2
    assert counts["total"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/test_system_health_fleet.py -v`
Expected: FAIL — `_fleet_counts` still queries DDB (`ecs_ops` not referenced → AttributeError on the patch target).

- [ ] **Step 3: Rewrite `_fleet_counts`**

```python
# In apps/backend/core/services/system_health.py, add the import near the top:
from core.services.ops import ecs_ops

# Replace the body of _fleet_counts (lines 114-129) with:
async def _fleet_counts() -> dict[str, int]:
    """Containers by status — LIVE from ECS (was DDB-derived).

    Delegates to ecs_ops.fleet_counts_live(); get_system_health()'s _safe
    wrapper still degrades this to {} if ECS is unreachable, so the admin
    health page always renders.
    """
    return await ecs_ops.fleet_counts_live()
```

- [ ] **Step 4: Run test + the existing system_health tests**

Run: `cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 uv run pytest tests/unit/core/services/test_system_health_fleet.py tests/unit/core/services/test_system_health.py -v`
Expected: PASS (new test + existing suite still green — if an existing test asserted DDB-derived behavior, update it to patch `ecs_ops.fleet_counts_live`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/system_health.py apps/backend/tests/unit/core/services/test_system_health_fleet.py
git commit -m "feat(ops): system_health fleet counts read live ECS"
```

---

## Task 9: CDK — `ops-cache` table + `platform_ops_token_key` secret

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (add `opsCacheTable`)
- Modify: `apps/infra/lib/stacks/auth-stack.ts` (add `platformOpsTokenKey` secret + export field)
- Test: `apps/infra/test/database-stack.test.ts` (if the repo has stack tests; otherwise synth-assert — see Step 2)

**Interfaces:**
- Produces: `DatabaseStack.opsCacheTable: dynamodb.Table` (name `isol8-{env}-ops-cache`, PK `cache_key`, TTL attr `ttl`); `AuthStack.platformOpsTokenKey` secret at name `isol8/{env}/platform_ops_token_key`. Consumed by Task 10 (service-stack wiring).

- [ ] **Step 1: Add the ops-cache table**

```typescript
// apps/infra/lib/stacks/database-stack.ts
// 1. Declare the public field near the other table fields (~line 58):
  public readonly opsCacheTable: dynamodb.Table;

// 2. In the constructor, after the pendingUpdatesTable block (~line 150):
    this.opsCacheTable = new dynamodb.Table(this, "OpsCacheTable", {
      tableName: `isol8-${env}-ops-cache`,
      partitionKey: { name: "cache_key", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: config.removalPolicy,
      pointInTimeRecovery: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
      timeToLiveAttribute: "ttl",
    });
```

- [ ] **Step 2: Add the platform_ops_token_key secret**

```typescript
// apps/infra/lib/stacks/auth-stack.ts
// 1. Declare the public field near paperclipServiceTokenKey:
  public readonly platformOpsTokenKey: secretsmanager.Secret;

// 2. In the constructor, after the paperclipServiceTokenKey createSecret (~line 106):
    this.platformOpsTokenKey = createSecret(
      "PlatformOpsTokenKey",
      "platform_ops_token_key",
      { passwordLength: 64, excludePunctuation: true },
      "Dedicated symmetric secret for signing/verifying platform_ops tokens (owner-scoped ops API).",
    );
```

- [ ] **Step 3: Typecheck + synth**

Run: `cd apps/infra && npx tsc --noEmit`
Expected: clean.
Run: `cd apps/infra && npx cdk synth --quiet 2>&1 | head -50` (if it needs creds, note "CI synths" and rely on the deploy pipeline).
Expected: no synth error; the new table + secret appear.

- [ ] **Step 4: Commit (with deploy.yml if regenerated)**

```bash
cd apps/infra && npx cdk synth --quiet >/dev/null 2>&1 || true
cd ../.. && git status --short   # check whether .github/workflows/deploy.yml changed
git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/auth-stack.ts
# If deploy.yml regenerated, ALSO: git add .github/workflows/deploy.yml
git commit -m "feat(ops): ops-cache table + platform_ops_token_key secret (CDK)"
```

---

## Task 10: CDK — service-stack IAM + env vars + secret injection

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (IAM statements + env + `SecretNames` field + secret import)
- Wherever `SecretNames` is populated per stage (grep `paperclipServiceTokenKey:` in `apps/infra/lib/*stage*.ts`) — add `platformOpsTokenKey`.

**Interfaces:**
- Consumes: `DatabaseStack.opsCacheTable`, `AuthStack.platformOpsTokenKey` (Task 9).
- Produces: backend task role gains `cloudformation:DescribeStackEvents`+`ListStacks` and `cloudwatch:DescribeAlarms`; backend env gains `OPS_CACHE_TABLE`, `PLATFORM_OPS_OWNER_ID`, and the `PLATFORM_OPS_TOKEN_KEY` secret.

- [ ] **Step 1: Extend the CloudFormation IAM statement**

```typescript
// apps/infra/lib/stacks/service-stack.ts — the Ec2SelfDiscovery statement (~line 678-689)
// change actions to include the two new CFN reads:
    actions: [
      "ec2:DescribeInstances",
      "ec2:DescribeTags",
      "cloudformation:DescribeStacks",
      "cloudformation:DescribeStackEvents",
      "cloudformation:ListStacks",
    ],
```

- [ ] **Step 2: Add a CloudWatch alarms read statement**

```typescript
// apps/infra/lib/stacks/service-stack.ts — after the CloudWatchLogsReadForAdmin
// statement (~line 636), add:
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchAlarmsReadForAdmin",
        actions: ["cloudwatch:DescribeAlarms"],
        resources: ["*"],
      }),
    );
```

- [ ] **Step 3: Add env vars + the secret + SecretNames field**

```typescript
// (a) In the environment: { ... } block (~line 1078), add:
        OPS_CACHE_TABLE: props.database.opsCacheTable.tableName,
        PLATFORM_OPS_OWNER_ID: process.env.PLATFORM_OPS_OWNER_ID ?? "",

// (b) In the secrets: { ... } block (near PAPERCLIP_SERVICE_TOKEN_KEY, ~line 1222):
        PLATFORM_OPS_TOKEN_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(
            this,
            "ImportPlatformOpsTokenKey",
            props.secretNames.platformOpsTokenKey,
          ),
        ),

// (c) In the SecretNames interface (~line 51-58), add:
  /**
   * Dedicated symmetric secret for signing/verifying platform_ops tokens.
   * Encrypted under the same CMK as the other auth secrets, so the existing
   * KmsDecryptForSecrets policy covers it.
   */
  platformOpsTokenKey: string;
```

- [ ] **Step 4: Wire `platformOpsTokenKey` at each stage's SecretNames**

```typescript
// In each stage file that constructs the ServiceStack props.secretNames
// (grep: `paperclipServiceTokenKey:`), add the sibling:
      platformOpsTokenKey: authStack.platformOpsTokenKey.secretName,
```

- [ ] **Step 5: Typecheck + synth + deploy.yml drift**

Run: `cd apps/infra && npx tsc --noEmit`
Expected: clean.
Run: `cd apps/infra && npx cdk synth --quiet >/dev/null 2>&1 || true` then `cd ../.. && git status --short`.
Expected: if `deploy.yml` regenerated, it MUST be committed.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/lib/*stage*.ts
# If deploy.yml regenerated: git add .github/workflows/deploy.yml
git commit -m "feat(ops): backend IAM (CFN events/alarms) + ops env/secret wiring"
```

---

## Task 11: Frontend design-system kit + `system.md`

**Files:**
- Create: `apps/frontend/src/components/admin/StatusChip.tsx`, `Tile.tsx`, `Card.tsx`, `MetricStat.tsx`
- Create: `apps/frontend/.interface-design/system.md`
- Test: `apps/frontend/src/components/admin/__tests__/StatusChip.test.tsx`

**Interfaces:**
- Produces:
  - `type OpsStatus = "running" | "provisioning" | "stopped" | "error" | "unknown"`.
  - `<StatusChip status={OpsStatus | string} label?={string} />` — semantic dark-theme chip.
  - `<Tile title label tone?={"ok"|"warn"|"error"|"info"|"unknown"} children />`.
  - `<Card title? children />`, `<MetricStat label value hint? />`.
  - Consumed by Tasks 13, 14.

**Design checkpoint (interface-design):** Intent = a platform operator scanning fleet health at a glance; must read calm when healthy, loud only on incidents. Palette = existing dark admin (zinc surfaces) + semantic status: emerald=healthy/running, sky=in-progress/provisioning, amber=warn/stopped, red=incident/error, zinc=unknown/unable-to-read. This mirrors the EXISTING `statusChipClasses` in `container/page.tsx:59-76` — extract it into the reusable `StatusChip` so the whole console shares one status language. Depth = borders-only (matches admin). Spacing base = 4px (Tailwind default).

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/admin/__tests__/StatusChip.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusChip } from "../StatusChip";

describe("StatusChip", () => {
  it("renders the status label and an emerald class for running", () => {
    const { container } = render(<StatusChip status="running" />);
    expect(screen.getByText(/running/i)).toBeTruthy();
    expect(container.querySelector("span")?.className).toContain("emerald");
  });

  it("renders a zinc (unknown) class for an unrecognized status", () => {
    const { container } = render(<StatusChip status="wat" />);
    expect(container.querySelector("span")?.className).toContain("zinc");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/admin/__tests__/StatusChip.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Implement the kit**

```tsx
// apps/frontend/src/components/admin/StatusChip.tsx
import { cn } from "@/lib/utils";

export type OpsStatus = "running" | "provisioning" | "stopped" | "error" | "unknown";

const CLASSES: Record<string, string> = {
  running: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  provisioning: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  stopped: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  error: "bg-red-500/15 text-red-300 border-red-500/30",
  unknown: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
};

export function StatusChip({ status, label }: { status: string; label?: string }) {
  const cls = CLASSES[status] ?? CLASSES.unknown;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        cls,
      )}
    >
      {label ?? status}
    </span>
  );
}
```

```tsx
// apps/frontend/src/components/admin/Card.tsx
import { cn } from "@/lib/utils";

export function Card({ title, children, className }: { title?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-lg border border-zinc-800 bg-zinc-900/40 p-4", className)}>
      {title ? <h2 className="mb-3 text-sm font-semibold text-zinc-300">{title}</h2> : null}
      {children}
    </div>
  );
}
```

```tsx
// apps/frontend/src/components/admin/Tile.tsx
import { cn } from "@/lib/utils";

const TONE: Record<string, string> = {
  ok: "border-emerald-500/30",
  warn: "border-amber-500/30",
  error: "border-red-500/40",
  info: "border-sky-500/30",
  unknown: "border-zinc-700",
};

export function Tile({
  title,
  label,
  tone = "unknown",
  children,
}: {
  title: string;
  label?: string;
  tone?: "ok" | "warn" | "error" | "info" | "unknown";
  children?: React.ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border bg-zinc-900/40 p-3", TONE[tone])}>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{title}</div>
      {label ? <div className="mt-1 text-lg font-semibold text-zinc-100">{label}</div> : null}
      {children}
    </div>
  );
}
```

```tsx
// apps/frontend/src/components/admin/MetricStat.tsx
export function MetricStat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div>
      <div className="text-2xl font-semibold tabular-nums text-zinc-100">{value}</div>
      <div className="text-xs text-zinc-500">{label}</div>
      {hint ? <div className="text-[10px] text-zinc-600">{hint}</div> : null}
    </div>
  );
}
```

- [ ] **Step 4: Write `system.md`**

```markdown
<!-- apps/frontend/.interface-design/system.md -->
# Admin Operator Console — Design System

**Direction:** operator console for a live agent fleet. Calm when healthy, loud only on incidents.
**Surface:** existing DARK admin theme (zinc-900/40 cards, zinc-800 borders, zinc-100 text). NOT cream/forest — that is the main-app brand; the admin surface stays dark (no recolor).
**Depth:** borders-only (`border-zinc-800`); no shadows.
**Spacing base:** 4px (Tailwind scale).
**Radius:** `rounded-lg` cards, `rounded` chips.

## Semantic status colors (the operator register)
- running / healthy → emerald (`bg-emerald-500/15 text-emerald-300 border-emerald-500/30`)
- provisioning / in-progress → sky
- stopped / warn → amber
- error / incident → red
- unknown / unable-to-read → zinc  ← a degraded source read renders THIS, never green

## Components
- `StatusChip` — one status language everywhere (extracted from container/page.tsx).
- `Tile` — attention-strip cell; `tone` border encodes severity.
- `Card` — section container.
- `MetricStat` — tabular-nums metric.
```

- [ ] **Step 5: Run test + tsc**

Run: `cd apps/frontend && pnpm vitest run src/components/admin/__tests__/StatusChip.test.tsx`
Expected: PASS.
Run: `cd apps/frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/admin/StatusChip.tsx apps/frontend/src/components/admin/Card.tsx apps/frontend/src/components/admin/Tile.tsx apps/frontend/src/components/admin/MetricStat.tsx apps/frontend/src/components/admin/__tests__/StatusChip.test.tsx apps/frontend/.interface-design/system.md
git commit -m "feat(admin): operator-console design-system kit + system.md"
```

---

## Task 12: Frontend ops read helpers + types + region single-source

**Files:**
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` (add `getFleet`, `getDeploys`, types, `region` on `ContainerSlice`)
- Create: `apps/frontend/src/lib/aws.ts`
- Modify: `apps/frontend/src/app/admin/users/[id]/container/page.tsx:88`, `ContainerActionsPanel.tsx:289`
- Test: `apps/frontend/src/lib/__tests__/aws.test.ts`

**Interfaces:**
- Produces:
  - `AWS_REGION: string` (from `process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1"`).
  - Types `OpsEnvelope<T>`, `FleetData`, `FleetGridItem`, `DeployRow`.
  - `getFleet(token): Promise<OpsEnvelope<FleetData> | null>`, `getDeploys(token): Promise<OpsEnvelope<DeployRow[]> | null>`. Consumed by Tasks 13, 14.

- [ ] **Step 1: Write the failing test (region constant)**

```ts
// apps/frontend/src/lib/__tests__/aws.test.ts
import { describe, expect, it } from "vitest";
import { AWS_REGION } from "../aws";

describe("AWS_REGION", () => {
  it("defaults to us-east-1 when the env var is unset", () => {
    expect(typeof AWS_REGION).toBe("string");
    expect(AWS_REGION.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/aws.test.ts`
Expected: FAIL — `../aws` missing.

- [ ] **Step 3: Add the region constant + ops helpers + types**

```ts
// apps/frontend/src/lib/aws.ts
// Single source for the AWS region used in console-URL construction.
// Override per-env with NEXT_PUBLIC_AWS_REGION; defaults to us-east-1.
export const AWS_REGION: string = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";
```

```ts
// apps/frontend/src/app/admin/_lib/api.ts — add near the other types/helpers:

export interface OpsEnvelope<T> {
  data: T;
  freshness_ts: string;
  source_state: "ok" | "degraded" | "stale" | "unavailable" | "partial";
  errors: { domain: string; reason: string }[];
}

export interface FleetGridItem {
  owner_id: string;
  service_name: string;
  status: string;
  desired_count: number;
  running_count: number;
  pending_count: number;
  rollout_state: string | null;
}

export interface FleetData {
  counts: Record<string, number> | null;
  grid: FleetGridItem[] | null;
}

export interface DeployRow {
  stack_name: string;
  status: string;
  status_reason: string | null;
  last_updated: string | null;
  root_cause: string | null;
}

export async function getFleet(token: string): Promise<OpsEnvelope<FleetData> | null> {
  return adminFetch<OpsEnvelope<FleetData>>(token, "/ops/fleet");
}

export async function getDeploys(token: string): Promise<OpsEnvelope<DeployRow[]> | null> {
  return adminFetch<OpsEnvelope<DeployRow[]>>(token, "/ops/deploys");
}
```

- [ ] **Step 4: Single-source the region at both call sites**

```ts
// apps/frontend/src/app/admin/users/[id]/container/page.tsx
// add import at top:
import { AWS_REGION } from "@/lib/aws";
// line 88: replace `const region = "us-east-1";` with:
  const region = AWS_REGION;
```

```tsx
// apps/frontend/src/app/admin/users/[id]/container/ContainerActionsPanel.tsx
// add import at top:
import { AWS_REGION } from "@/lib/aws";
// line ~289: the placeholder string is cosmetic; interpolate the region so it
// tracks a single source:
  placeholder={`877352799272.dkr.ecr.${AWS_REGION}.amazonaws.com/isol8/openclaw-extended:<tag>`}
```

- [ ] **Step 5: Run test + tsc**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/aws.test.ts`
Expected: PASS.
Run: `cd apps/frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/lib/aws.ts apps/frontend/src/lib/__tests__/aws.test.ts apps/frontend/src/app/admin/_lib/api.ts "apps/frontend/src/app/admin/users/[id]/container/page.tsx" "apps/frontend/src/app/admin/users/[id]/container/ContainerActionsPanel.tsx"
git commit -m "feat(admin): ops read helpers + single-sourced AWS region"
```

---

## Task 13: `/admin` operator landing (attention strip + live fleet)

**Files:**
- Modify: `apps/frontend/src/app/admin/page.tsx` (replace the redirect)
- Create: `apps/frontend/src/components/admin/AutoRefresh.tsx`
- Modify: `apps/frontend/src/app/admin/layout.tsx` (add Overview nav link)
- Test: `apps/frontend/src/components/admin/__tests__/AutoRefresh.test.tsx`

**Interfaces:**
- Consumes: `getFleet` (Task 12), `StatusChip`/`Tile`/`Card`/`MetricStat` (Task 11).
- Produces: the operator landing. `AutoRefresh` client component calls `router.refresh()` on an interval (re-runs the Server Component → re-fetches through `adminFetch`).

- [ ] **Step 1: Write the failing test (AutoRefresh calls router.refresh)**

```tsx
// apps/frontend/src/components/admin/__tests__/AutoRefresh.test.tsx
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

import { AutoRefresh } from "../AutoRefresh";

describe("AutoRefresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("calls router.refresh on the interval", () => {
    render(<AutoRefresh intervalMs={1000} />);
    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/admin/__tests__/AutoRefresh.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Implement AutoRefresh + the landing**

```tsx
// apps/frontend/src/components/admin/AutoRefresh.tsx
"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export function AutoRefresh({ intervalMs = 20000 }: { intervalMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(id);
  }, [router, intervalMs]);
  return null;
}
```

```tsx
// apps/frontend/src/app/admin/page.tsx — REPLACE the redirect with:
import { auth } from "@clerk/nextjs/server";

import { AutoRefresh } from "@/components/admin/AutoRefresh";
import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { StatusChip } from "@/components/admin/StatusChip";
import { Tile } from "@/components/admin/Tile";
import { getFleet } from "./_lib/api";

export default async function AdminOverviewPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const fleet = token ? await getFleet(token) : null;

  const degraded = !fleet || fleet.source_state !== "ok";
  const counts = fleet?.data.counts ?? null;
  const grid = fleet?.data.grid ?? [];
  const attention = grid.filter((g) => g.status === "error" || g.status === "unknown");

  return (
    <div className="space-y-6">
      <AutoRefresh intervalMs={20000} />
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-zinc-100">Operations</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>

      {/* Attention strip */}
      <Card title="Needs attention">
        {attention.length === 0 ? (
          <p className="text-sm text-zinc-500">All containers healthy.</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {attention.map((g) => (
              <Tile key={g.service_name} title={g.owner_id} label={g.status} tone={g.status === "error" ? "error" : "unknown"} />
            ))}
          </div>
        )}
      </Card>

      {/* Summary tiles */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {(["running", "provisioning", "stopped", "error", "unknown"] as const).map((k) => (
          <Card key={k}>
            <MetricStat label={k} value={counts ? (counts[k] ?? 0) : "—"} />
          </Card>
        ))}
      </div>

      {/* Live fleet grid */}
      <Card title="Fleet">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {grid.map((g) => (
            <div key={g.service_name} className="flex items-center justify-between rounded border border-zinc-800 p-2">
              <span className="truncate text-xs text-zinc-400">{g.owner_id}</span>
              <StatusChip status={g.status} />
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
```

- [ ] **Step 4: Add the Overview nav link**

```tsx
// apps/frontend/src/app/admin/layout.tsx — in the <nav> block (~line 47), add
// as the FIRST link:
        <Link href="/admin" className="...">Overview</Link>
```

- [ ] **Step 5: Run test + tsc**

Run: `cd apps/frontend && pnpm vitest run src/components/admin/__tests__/AutoRefresh.test.tsx`
Expected: PASS.
Run: `cd apps/frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/app/admin/page.tsx apps/frontend/src/components/admin/AutoRefresh.tsx apps/frontend/src/components/admin/__tests__/AutoRefresh.test.tsx apps/frontend/src/app/admin/layout.tsx
git commit -m "feat(admin): operator landing — attention strip + live fleet"
```

---

## Task 14: `/admin/deploys` view + `/admin/audit` platform-wide view + Logs nav

**Files:**
- Create: `apps/frontend/src/app/admin/deploys/page.tsx`
- Create: `apps/frontend/src/app/admin/audit/page.tsx`
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` (allow `getActions` with only `admin_user_id` / neither filter — already supported; confirm)
- Modify: `apps/frontend/src/app/admin/layout.tsx` (add Deploys, Audit, Logs nav links)

**Interfaces:**
- Consumes: `getDeploys` (Task 12), `getActions` (existing), `StatusChip`/`Card` (Task 11), `AuditRow` (existing).
- Produces: two new server-component pages.

- [ ] **Step 1: Implement the deploys page**

```tsx
// apps/frontend/src/app/admin/deploys/page.tsx
import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { StatusChip } from "@/components/admin/StatusChip";
import { getDeploys } from "../_lib/api";

function deployTone(status: string): string {
  if (status.endsWith("_FAILED") || status.includes("ROLLBACK")) return "error";
  if (status.endsWith("_IN_PROGRESS")) return "provisioning";
  if (status.endsWith("_COMPLETE")) return "running";
  return "unknown";
}

export default async function DeploysPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const res = token ? await getDeploys(token) : null;
  const rows = res?.data ?? [];
  const degraded = !res || res.source_state !== "ok";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-zinc-100">Deploys</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>
      <Card>
        {rows.length === 0 ? (
          <p className="text-sm text-zinc-500">No isol8 stacks found.</p>
        ) : (
          <div className="space-y-2">
            {rows.map((r) => (
              <div key={r.stack_name} className="rounded border border-zinc-800 p-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm text-zinc-200">{r.stack_name}</span>
                  <StatusChip status={deployTone(r.status)} label={r.status} />
                </div>
                {r.root_cause ? (
                  <p className="mt-2 text-xs text-red-300">Root cause: {r.root_cause}</p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Implement the platform-wide audit page**

```tsx
// apps/frontend/src/app/admin/audit/page.tsx
import { auth } from "@clerk/nextjs/server";

import { AuditRow } from "@/components/admin/AuditRow";
import { Card } from "@/components/admin/Card";
import { getActions } from "../_lib/api";

interface AuditPageProps {
  searchParams: Promise<{ admin_user_id?: string; cursor?: string }>;
}

export default async function PlatformAuditPage({ searchParams }: AuditPageProps) {
  const params = await searchParams;
  const { getToken } = await auth();
  const token = await getToken();
  const result = token
    ? await getActions(token, { admin_user_id: params.admin_user_id, limit: 100, cursor: params.cursor })
    : { items: [], cursor: null };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-zinc-100">Platform audit</h1>
      <Card>
        {result.items.length === 0 ? (
          <p className="text-sm text-zinc-500">No admin actions recorded.</p>
        ) : (
          <div className="divide-y divide-zinc-800">
            {result.items.map((entry, i) => (
              <AuditRow key={i} entry={entry} />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
```

> Note: if `AuditRow`'s prop type is not exported or its shape differs, import the type from where `getActions` returns it and adapt the prop name — do not invent fields. Mirror `users/[id]/actions/page.tsx`.

- [ ] **Step 3: Add nav links (Deploys, Audit, Logs)**

```tsx
// apps/frontend/src/app/admin/layout.tsx — in the <nav> block, add:
        <Link href="/admin/deploys" className="...">Deploys</Link>
        <Link href="/admin/audit" className="...">Audit</Link>
        {/* Surface the previously orphaned per-user logs viewer entry point */}
        <Link href="/admin/users" className="...">Logs</Link>
```

> The per-user Logs viewer lives at `users/[id]/logs`; there is no fleet-wide logs page in Phase 1 (that's Phase 2). For P1, "surface the orphaned logs viewer" = ensure the per-user logs tab is reachable from the user detail nav (confirm `users/[id]` layout links to `./logs`); if it already does, this reduces to the Phase-2 note. Keep the top-nav "Logs" link pointing at the users list where per-user logs are reached, OR omit it and defer the fleet-wide logs nav to Phase 2. Pick the omit-and-defer option if the per-user logs tab is already linked.

- [ ] **Step 4: tsc + targeted vitest (if any component test added)**

Run: `cd apps/frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/app/admin/deploys/page.tsx apps/frontend/src/app/admin/audit/page.tsx apps/frontend/src/app/admin/layout.tsx
git commit -m "feat(admin): deploys view + platform-wide audit view + nav"
```

---

## Task 15: Fetch-layer consolidation note + final verification

**Files:**
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` and `apps/frontend/src/app/admin/_actions/_shared.ts` (unify onto one request core)
- Verify: full Phase-1 backend + frontend targeted suites

**Interfaces:** no new external surface — internal DRY only. `adminFetch` (reads) and `adminRequest`/`adminPost` (mutations) currently duplicate URL-building + Clerk-token + error handling. Consolidate the shared core into one helper both call, preserving both public signatures (`adminFetch<T>(token, path, opts)` and `adminPost(path, body)`).

- [ ] **Step 1: Extract the shared request core**

Create a single internal `adminHttp({ method, path, token, body, idempotent })` in `_lib/api.ts` that builds the URL (`apiUrl()` + query), sets `Authorization`/`Content-Type`/optional `Idempotency-Key`, `cache: "no-store"`, and returns `{ ok, status, data }`. Re-express `adminFetch<T>` as a thin wrapper that returns `data as T | null`. Have `_actions/_shared.ts adminRequest` import and delegate to `adminHttp` (fetching the token via `auth()` first), preserving its `ActionResult` return.

> Keep both public entry points; only the duplicated internals move. Do not change any caller.

- [ ] **Step 2: Typecheck**

Run: `cd apps/frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Run the full Phase-1 backend suite (targeted files only)**

Run:
```bash
cd apps/backend && env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 \
  uv run pytest \
  tests/unit/core/test_config_platform_ops.py \
  tests/unit/core/services/test_platform_ops_token.py \
  tests/unit/core/repositories/test_ops_cache_repo.py \
  tests/unit/core/services/ops/ \
  tests/unit/routers/test_ops.py \
  tests/unit/core/services/test_system_health_fleet.py -v
```
Expected: ALL PASS.

- [ ] **Step 4: Ruff + frontend targeted vitest**

Run: `cd apps/backend && uv run ruff format --check . && uv run ruff check .` (fix + re-commit if it auto-edits).
Run: `cd apps/frontend && pnpm vitest run src/components/admin/__tests__/StatusChip.test.tsx src/components/admin/__tests__/AutoRefresh.test.tsx src/lib/__tests__/aws.test.ts`
Expected: PASS.

- [ ] **Step 5: No committed node_modules symlink**

Run: `git ls-tree -r HEAD | awk '$1=="120000"{print}'`
Expected: empty.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/app/admin/_lib/api.ts apps/frontend/src/app/admin/_actions/_shared.ts
git commit -m "refactor(admin): unify admin fetch layer onto one request core"
```

---

## Self-Review (completed by plan author)

**Spec coverage (spec §5 Phase 1):**
- Shared UI kit + `system.md` + one fetch client → Tasks 11, 15. ✔
- `/admin` operator landing (attention strip + live fleet) → Task 13. ✔
- Live-ECS health (replace `_fleet_counts`, ops-cache table + short-TTL cache) → Tasks 3, 5, 8, 9. ✔
- Deploy/stack status view (DescribeStacks + scoped DescribeStackEvents) → Tasks 6, 14. ✔
- Coherence: region single-source both sites → Task 12; platform-wide audit view → Task 14; surface orphaned logs viewer → Task 14 Step 3 (defer-if-already-linked). ✔
- Ops layer + `routers/ops.py` + DTO envelope + platform_ops token (mint/revoke) + `/ops/fleet` + `/ops/deploys` → Tasks 2, 4, 7. **Revoke:** mint is built (Task 7 `POST /ops/token`); explicit *revoke* (jti deny-list) is NOT built in P1 — rotation is via the CDK secret (documented in spec §3.1). Flagged below.
- New IAM: `cloudformation:DescribeStackEvents`+`ListStacks`, `cloudwatch:DescribeAlarms` → Task 10. ✔
- **Done-when (spec §5 P1):** attention strip + live-ECS fleet (Task 13), `/ops/*` under both auth with tenant rejected (Task 7 tests), `system.md` committed (Task 11), region single-sourced both sites (Task 12), backend mocked-boto3 + targeted vitest green (Task 15), new IAM + `deploy.yml` regen (Tasks 9, 10). ✔

**Known scope notes (intentional, flagged for the executor):**
1. **Token revocation** is rotation-by-secret in P1, not a jti deny-list. If per-token revocation is wanted in P1, add a small `ops-cache`-backed jti deny-list checked in `platform_ops_token.verify`; otherwise it's a documented later increment.
2. **"Surface orphaned logs viewer"** resolves to a nav confirmation (Task 14 Step 3) — the fleet-wide logs surface is Phase 2. If the per-user logs tab is already linked from the user-detail nav, omit the top-nav Logs link.
3. **`deploy.yml` regen** appears in Tasks 9 and 10 — commit whichever run regenerates it; if neither does (no structural change detected), that's fine.
4. **Design surface** is dark zinc (not cream/forest) — reconciled in Global Constraints + `system.md`; surfaced for the user in the handoff.

**Placeholder scan:** none — every code step has complete code.
**Type consistency:** `OpsEnvelope<T>` / `source_state` union match backend `envelope()` keys; `FleetGridItem` matches `ecs_ops.fleet_grid` dict; `DeployRow` matches `deploys.list_stacks` dict; `StatusChip` status strings match `ecs_ops._classify` outputs (`running/provisioning/stopped/error/unknown`). ✔

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`).
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).
