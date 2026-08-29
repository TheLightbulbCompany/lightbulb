# Admin Growth Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fleet-wide `/admin/growth` page that renders the DECIDED GTM metric set (activation gate, North Star, retention, depth, counter-metrics) for every prod owner, with internal accounts segmented out of every headline number.

**Architecture:** One new backend read `GET /api/v1/ops/growth` computes everything on read (no new tables, no workers) from four sources — DynamoDB scans (billing / containers / paperclip-companies / integrations), the Paperclip Aurora DB via the RDS Data API (the run/routine ledger — the backend task role already holds this grant), PostHog HogQL (client funnel + attribution cross-check only), and CloudWatch `GetMetricData` (gate-blocked codes). A pure `derive()` turns rows into the metrics dict and is where all tests live. The page is a Next.js Server Component reading through the existing `adminFetch` + `OpsEnvelope` contract, rendered with the existing `Card`/`MetricStat` primitives — counts and named lists, no percentages, no chart library.

**Tech Stack:** FastAPI + boto3 (`rds-data`, `cloudwatch`) + httpx (PostHog), pytest; Next.js 16 Server Components + Tailwind; CDK (one IAM statement).

**Spec:** `docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md` (§0.1 research + §1 decided metrics) and `docs/growth/2026-08-17-blitzscaling-strategy.md` §3.

## Global Constraints

- Metric definitions are DECIDED — do not redesign them. Activation gate = subscribed → container ready → **Friday's first run delivered** (owner decision 2026-08-26: `bootstrap_state == "delivered"` on the paperclip-companies owner row, stamped by the kickoff observer when Friday's kickoff run reaches `final`) → ≥1 automation armed → first trigger/schedule-fired run succeeds. Chat is NOT activation.
- **Internal/external segmentation before any aggregate.** Internal = `run_analytics.is_internal_owner` (Clerk email `@isol8.co` or `prasiddha@gmail.com`). Headline numbers are external-only; internal is shown as its own segment.
- **Nightly CEO runs never count toward the North Star.** Identify by routine title `NIGHTLY_CEO_ROUTINE_TITLE` (`core/services/agent_routine_provisioning.py`). Report them as their own bucket.
- **Autonomous** = the dispatching `routine_runs.source ∈ {schedule, webhook}`. `manual` ("Run now") and `api` are human.
- **Counts and named lists, not percentages** (spec: "at current scale cohort percentages are noise"). The single share shown is autonomous-vs-human share per week, labelled as such.
- **Computed on read, no new DynamoDB tables, no long-running workers** (operating-loops plan rule). 60s in-process TTL cache, same pattern as `core/services/ops/ecs_ops.py`.
- **Paperclip DB is the ledger; PostHog is the cross-check** (§0.1: PostHog recorded 0 `routine_schedule` where the DB has 2). Never derive a headline from PostHog.
- Weeks are **Monday-start** everywhere: Postgres `date_trunc('week', …)`, HogQL `toStartOfWeek(timestamp, 1)`, Python `d - timedelta(days=d.weekday())`.
- RDS Data API: response ≤ 1 MiB (10 MB with `formatRecordsAs=JSON`), writer-only, SELECT-only in our helper. Keep every statement aggregated or `LIMIT`ed.
- PostHog `/query`: 10s execution cap, 100-row default / 50k max, 2,400 req/hr team-wide (shared with the per-user Activity tab). Two queries per compute, behind the 60s cache.
- Any change under `apps/infra/lib/` MUST run `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the same PR (CLAUDE.md).
- Every ops read returns `{data, freshness_ts, source_state, errors}`; a failed source is `partial`/`unavailable`, never empty data that renders green (`core/services/ops/envelope.py` contract).
- Backend tests: `cd apps/backend && uv run pytest tests/ -v`. Frontend: `cd apps/frontend && pnpm test` and `pnpm run lint`. Never `sleep`/`tail`.
- Branch: `feat/admin-growth-dashboard` off `main` (never commit to main; open a PR).

---

## File map

| File | Responsibility |
|---|---|
| `apps/backend/core/services/paperclip_sql.py` (new) | SELECT-only RDS Data API helper: cluster ARN, cached secret-ARN lookup, `select_rows()` |
| `apps/backend/core/services/paperclip_user_delete.py` (modify) | import the shared helpers instead of its private copies |
| `apps/backend/core/repositories/billing_repo.py` (modify) | `scan_all()` |
| `apps/backend/core/repositories/paperclip_repo.py` (modify) | `PaperclipRepo.scan_all_items()` |
| `apps/backend/core/services/posthog_admin.py` (modify) | `hogql()` |
| `apps/backend/core/services/run_analytics.py` (modify) | public `is_internal_owner` alias |
| `apps/backend/core/services/ops/growth.py` (new) | SQL/HogQL constants, pure `derive()`, `compute()` with TTL cache |
| `apps/backend/core/services/ops/gate_blocked.py` (new) | CloudWatch `GetMetricData` read of `provision.gate.blocked` by code |
| `apps/backend/routers/ops.py` (modify) | `GET /ops/growth` |
| `apps/infra/lib/stacks/service-stack.ts` (modify) | `cloudwatch:GetMetricData` grant; regenerate `deploy.yml` |
| `apps/frontend/src/app/admin/_lib/api.ts` (modify) | `GrowthData` types + `getGrowth()` |
| `apps/frontend/src/app/admin/growth/page.tsx` (new) | Server Component page |
| `apps/frontend/src/app/admin/growth/sections.tsx` (new) | presentational section components |
| `apps/frontend/src/app/admin/layout.tsx` (modify) | "Growth" nav link |

Tests: `apps/backend/tests/unit/services/test_paperclip_sql.py`, `…/test_posthog_admin.py` (extend), `…/test_growth.py`, `…/test_gate_blocked.py`, `apps/infra/test/service-stack.test.ts` (extend).

---

### Task 1: SELECT-only Data API helper (`paperclip_sql.py`)

**Files:**
- Create: `apps/backend/core/services/paperclip_sql.py`
- Modify: `apps/backend/core/services/paperclip_user_delete.py:62-84` (replace the four private helpers with imports)
- Test: `apps/backend/tests/unit/services/test_paperclip_sql.py`

**Interfaces:**
- Produces: `async def select_rows(sql: str, *, env: str | None = None, parameters: list[dict] | None = None) -> list[dict]`; `def cluster_arn(env: str) -> str`; `def find_secret_arn(sm_client, env: str) -> str`; `def rds_data_client()`; `def secretsmanager_client()`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_paperclip_sql.py
"""select_rows: SELECT/WITH only, formatRecordsAs=JSON parsed, secret ARN cached per env."""
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import paperclip_sql  # noqa: E402


@pytest.mark.asyncio
async def test_rejects_non_select():
    with pytest.raises(ValueError):
        await paperclip_sql.select_rows("DELETE FROM routines", env="test")


@pytest.mark.asyncio
async def test_parses_formatted_records_and_caches_secret_arn(monkeypatch):
    paperclip_sql._secret_arn_cache.clear()
    sm = MagicMock()
    sm.get_paginator.return_value.paginate.return_value = [
        {"SecretList": [{"Name": "isol8-test-paperclip-db-credentials-abc", "ARN": "arn:secret:1"}]}
    ]
    rds = MagicMock()
    rds.execute_statement.return_value = {"formattedRecords": '[{"n": 2, "name": "Isol8"}]'}
    with (
        patch("core.services.paperclip_sql.secretsmanager_client", return_value=sm),
        patch("core.services.paperclip_sql.rds_data_client", return_value=rds),
    ):
        rows = await paperclip_sql.select_rows("  select count(*) as n from companies", env="test")
        rows2 = await paperclip_sql.select_rows("WITH x AS (select 1) select * from x", env="test")

    assert rows == [{"n": 2, "name": "Isol8"}]
    assert rows2 == rows
    assert sm.get_paginator.call_count == 1  # cached after the first lookup
    kwargs = rds.execute_statement.call_args.kwargs
    assert kwargs["resourceArn"].endswith(":cluster:isol8-test-paperclip-db")
    assert kwargs["secretArn"] == "arn:secret:1"
    assert kwargs["database"] == "paperclip"
    assert kwargs["formatRecordsAs"] == "JSON"


@pytest.mark.asyncio
async def test_empty_result_is_empty_list():
    paperclip_sql._secret_arn_cache["test"] = "arn:secret:cached"
    rds = MagicMock()
    rds.execute_statement.return_value = {}
    with patch("core.services.paperclip_sql.rds_data_client", return_value=rds):
        assert await paperclip_sql.select_rows("select 1", env="test") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_sql.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.paperclip_sql`

- [ ] **Step 3: Write the helper**

```python
# apps/backend/core/services/paperclip_sql.py
"""Read-only SQL against the Paperclip Aurora cluster via the RDS Data API.

The backend task role already carries ``rds-data:ExecuteStatement`` on the
cluster and ``secretsmanager:GetSecretValue`` on the DASH-named
``isol8-{env}-paperclip-db-credentials`` secret (service-stack.ts
``AccountWipePaperclipAurora*``), granted for the account-wipe user delete.
This module is the shared wiring for that grant: ``paperclip_user_delete``
imports the ARN/client helpers from here, and fleet aggregates
(``core/services/ops/growth.py``) use ``select_rows``.

Why SQL and not ``PaperclipAdminClient``: every Paperclip REST read is
company-scoped, so a fleet aggregate over N tenants is 3-4 calls per tenant;
one SQL statement covers all tenants and returns an aggregate that fits the
Data API's 1 MiB response cap (10 MB with ``formatRecordsAs=JSON``). Column
names come from the deployed fork's drizzle schema
(``~/dev/paperclip-fork/packages/db/src/schema``) — verify against the pinned
tag in ``paperclip-version.json`` before adding a statement.
"""

from __future__ import annotations

import json

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

# Same AWS account both envs share (see core.billing.bedrock_aip.OUR_ACCOUNT_ID).
_ACCOUNT_ID = "877352799272"

# env -> secret ARN. ListSecrets has no resource-level scoping and paginates;
# resolve once per process.
_secret_arn_cache: dict[str, str] = {}


def rds_data_client():
    return boto3.client("rds-data", region_name=settings.AWS_REGION)


def secretsmanager_client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


def cluster_arn(env: str) -> str:
    return f"arn:aws:rds:{settings.AWS_REGION}:{_ACCOUNT_ID}:cluster:isol8-{env}-paperclip-db"


def find_secret_arn(sm_client, env: str) -> str:
    """Paginated lookup: with enough secrets in the account the target can be
    on a later page, so a single unpaginated call falsely reports not-found."""
    needle = f"isol8-{env}-paperclip-db-credentials"
    for page in sm_client.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            if needle in s["Name"]:
                return s["ARN"]
    raise RuntimeError(f"Could not find paperclip-db-credentials secret for env={env}")


async def select_rows(
    sql: str,
    *,
    env: str | None = None,
    parameters: list[dict] | None = None,
) -> list[dict]:
    """Run one SELECT/WITH statement and return rows as dicts keyed by column alias.

    Refuses anything else — the master credentials can write, this door cannot.
    ``formatRecordsAs=JSON`` renders timestamps/uuids/jsonb as strings and
    ints/floats/bools natively (AWS data-api-json docs); duplicate column
    labels are a Data API error, so alias every expression.
    """
    head = sql.lstrip()[:6].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError("select_rows only runs SELECT/WITH statements")
    env = env or settings.ENVIRONMENT
    if env not in _secret_arn_cache:
        _secret_arn_cache[env] = await run_in_thread(find_secret_arn, secretsmanager_client(), env)
    resp = await run_in_thread(
        rds_data_client().execute_statement,
        resourceArn=cluster_arn(env),
        secretArn=_secret_arn_cache[env],
        database="paperclip",
        sql=sql,
        parameters=parameters or [],
        formatRecordsAs="JSON",
    )
    return json.loads(resp.get("formattedRecords") or "[]")
```

- [ ] **Step 4: Point `paperclip_user_delete.py` at the shared helpers**

Replace lines 62-84 (`_rds_data_client`, `_secretsmanager_client`, `_cluster_arn`, `_find_secret_arn` and the `_ACCOUNT_ID` constant just above them) with:

```python
# Shared with core/services/ops/growth.py — the private names are kept so the
# existing tests' patch targets keep working.
from core.services.paperclip_sql import (  # noqa: E402
    cluster_arn as _cluster_arn,
    find_secret_arn as _find_secret_arn,
    rds_data_client as _rds_data_client,
    secretsmanager_client as _secretsmanager_client,
)
```

Keep `BOOTSTRAP_EMAILS`, `BootstrapUserProtectedError`, and `delete_paperclip_user` untouched. Remove the now-unused `import boto3` if nothing else in the file uses it.

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_sql.py tests/unit/services/test_paperclip_user_delete.py tests/unit/services/test_account_wipe*.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_sql.py apps/backend/core/services/paperclip_user_delete.py apps/backend/tests/unit/services/test_paperclip_sql.py
git commit -m "feat(backend): shared SELECT-only RDS Data API helper for the Paperclip DB"
```

---

### Task 2: Fleet scans on the billing and paperclip-companies tables

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py` (append after `list_by_stripe_customer_id`, ~line 60)
- Modify: `apps/backend/core/repositories/paperclip_repo.py` (append a method to `PaperclipRepo`, after `scan_purge_due` ~line 2320)
- Test: `apps/backend/tests/unit/repositories/test_fleet_scans.py`

**Interfaces:**
- Produces: `billing_repo.scan_all() -> list[dict]` (every billing row, paginated); `PaperclipRepo.scan_all_items() -> list[dict]` (every raw row including `__marker__` rows — callers filter).

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_fleet_scans.py
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import billing_repo  # noqa: E402
from core.repositories.paperclip_repo import PaperclipRepo  # noqa: E402


def _two_page_table():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"owner_id": "a"}], "LastEvaluatedKey": {"owner_id": "a"}},
        {"Items": [{"owner_id": "b"}]},
    ]
    return table


@pytest.mark.asyncio
async def test_billing_scan_all_paginates():
    table = _two_page_table()
    with patch("core.repositories.billing_repo._get_table", return_value=table):
        rows = await billing_repo.scan_all()
    assert [r["owner_id"] for r in rows] == ["a", "b"]
    assert table.scan.call_args_list[1].kwargs == {"ExclusiveStartKey": {"owner_id": "a"}}


@pytest.mark.asyncio
async def test_paperclip_scan_all_items_paginates():
    table = _two_page_table()
    repo = PaperclipRepo(table_name="paperclip-companies")
    with patch.object(PaperclipRepo, "_table", return_value=table):
        rows = await repo.scan_all_items()
    assert [r["owner_id"] for r in rows] == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_fleet_scans.py -v`
Expected: FAIL with `AttributeError: module 'core.repositories.billing_repo' has no attribute 'scan_all'`

- [ ] **Step 3: Implement both scans**

`billing_repo.py`:

```python
async def scan_all() -> list[dict]:
    """Every billing row, paginated via LastEvaluatedKey.

    Fleet aggregates only (admin growth read). Not for hot paths — a DDB
    Scan is the whole table every call. Mirrors ``container_repo.scan_all``.
    """
    table = _get_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
```

`paperclip_repo.py` (inside `class PaperclipRepo`):

```python
    async def scan_all_items(self) -> list[dict]:
        """Every raw row, paginated — INCLUDING the ``__org_create_claim__:`` /
        ``__pending_brief__:`` marker rows (their ``user_id`` starts with
        ``__``); callers filter. Fleet aggregates only, never a hot path."""
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            page = await run_in_thread(self._table().scan, **kwargs)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_fleet_scans.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/billing_repo.py apps/backend/core/repositories/paperclip_repo.py apps/backend/tests/unit/repositories/test_fleet_scans.py
git commit -m "feat(backend): paginated fleet scans for billing and paperclip-companies"
```

---

### Task 3: PostHog HogQL read (`posthog_admin.hogql`)

**Files:**
- Modify: `apps/backend/core/services/posthog_admin.py` (append after `get_person_events`)
- Modify: `apps/backend/core/services/run_analytics.py` (one-line public alias after `_is_internal_owner`, ~line 284)
- Test: `apps/backend/tests/unit/services/test_posthog_admin.py` (append)

**Interfaces:**
- Produces: `async def hogql(query: str, *, timeout_s: float = 15.0) -> dict` → `{"columns": list[str], "results": list[list], "stubbed": bool, "error": str | None}`; `run_analytics.is_internal_owner(owner_id) -> bool` (alias of `_is_internal_owner`).

- [ ] **Step 1: Write the failing tests** (append to `test_posthog_admin.py`; reuse its `_make_response`, `_apply_posthog_settings`)

```python
@asynccontextmanager
async def _fake_post_client(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.post = AsyncMock(side_effect=response_or_exc)
    else:
        client.post = AsyncMock(return_value=response_or_exc)
    yield client


@pytest.mark.asyncio
async def test_hogql_stubbed_without_key(monkeypatch):
    _apply_posthog_settings(monkeypatch, api_key="")
    from core.services import posthog_admin

    out = await posthog_admin.hogql("select 1")
    assert out == {"columns": [], "results": [], "stubbed": True, "error": None}


@pytest.mark.asyncio
async def test_hogql_posts_query_kind_and_parses(monkeypatch):
    _apply_posthog_settings(monkeypatch, host="https://us.posthog.com")
    from core.services import posthog_admin

    resp = _make_response(200, {"columns": ["week", "n"], "results": [["2026-08-17", 3]]})
    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(resp)) as client_cls:
        out = await posthog_admin.hogql("select 1")
    assert out == {"columns": ["week", "n"], "results": [["2026-08-17", 3]], "stubbed": False, "error": None}
    fake = client_cls.return_value
    posted = fake.__aenter__.return_value.post.call_args
    assert posted.args[0] == "https://us.posthog.com/api/projects/12345/query/"
    assert posted.kwargs["json"] == {"query": {"kind": "HogQLQuery", "query": "select 1"}}
    assert posted.kwargs["headers"]["Authorization"] == "Bearer phc_xyz"


@pytest.mark.asyncio
async def test_hogql_403_scope(monkeypatch):
    _apply_posthog_settings(monkeypatch)
    from core.services import posthog_admin

    resp = _make_response(403, text_body='{"detail":"API key missing required scope"}')
    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(resp)):
        out = await posthog_admin.hogql("select 1")
    assert out["error"] == "insufficient_scope" and out["results"] == []


@pytest.mark.asyncio
async def test_hogql_timeout(monkeypatch):
    _apply_posthog_settings(monkeypatch)
    from core.services import posthog_admin

    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        return_value=_fake_post_client(httpx.TimeoutException("slow")),
    ):
        out = await posthog_admin.hogql("select 1")
    assert out["error"] == "timeout"
```

If `_fake_client` in that file is used as `patch(..., return_value=_fake_client(resp))` with `__aenter__` semantics, copy that exact patching style for `_fake_post_client` so the two helpers behave identically (read the existing `test_parses_events_from_results` first and mirror it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_posthog_admin.py -k hogql -v`
Expected: FAIL with `AttributeError: module 'core.services.posthog_admin' has no attribute 'hogql'`

- [ ] **Step 3: Implement `hogql`**

```python
async def hogql(query: str, *, timeout_s: float = 15.0) -> dict:
    """Run one HogQL statement via ``POST /api/projects/{id}/query/``.

    Needs the ``query:read`` scope on the personal ``phx_`` key (verified on
    the prod key 2026-08-26). PostHog caps execution at 10s and rows at 100
    by default / 50k with an explicit LIMIT; the ``/query`` bucket is 2,400
    requests/hour shared across the team, so callers cache.

    Returns ``{columns, results, stubbed, error}``; never raises.
    """
    empty: dict = {"columns": [], "results": []}
    if not settings.POSTHOG_PROJECT_API_KEY or not settings.POSTHOG_PROJECT_ID:
        return {**empty, "stubbed": True, "error": None}

    url = f"{settings.POSTHOG_HOST}/api/projects/{settings.POSTHOG_PROJECT_ID}/query/"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PROJECT_API_KEY}"}
    body = {"query": {"kind": "HogQLQuery", "query": query}}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        return {**empty, "stubbed": False, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        logger.warning("posthog_admin.hogql network error: %s", e)
        return {**empty, "stubbed": False, "error": str(e)}

    if response.status_code == 403 and "scope" in response.text:
        return {**empty, "stubbed": False, "error": "insufficient_scope"}
    if response.status_code != 200:
        return {**empty, "stubbed": False, "error": f"http_{response.status_code}"}

    data = response.json()
    return {
        "columns": list(data.get("columns") or []),
        "results": list(data.get("results") or []),
        "stubbed": False,
        "error": None,
    }
```

- [ ] **Step 4: Public alias in `run_analytics.py`** (directly after `_is_internal_owner`'s definition)

```python
# Public name for readers outside the emit path (admin growth aggregates).
is_internal_owner = _is_internal_owner
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_posthog_admin.py tests/unit/services/test_run_analytics.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/posthog_admin.py apps/backend/core/services/run_analytics.py apps/backend/tests/unit/services/test_posthog_admin.py
git commit -m "feat(backend): PostHog HogQL read + public is_internal_owner"
```

---

### Task 4: `growth.derive()` — the metric definitions, as pure code

**Files:**
- Create: `apps/backend/core/services/ops/growth.py` (constants + `derive` only; `compute` is Task 5)
- Test: `apps/backend/tests/unit/services/test_growth.py`

**Interfaces:**
- Consumes: `NIGHTLY_CEO_ROUTINE_TITLE` from `core.services.agent_routine_provisioning`.
- Produces: `derive(*, billing, containers, paperclip, integrations, companies, routines, runs, first_autonomous, gate_blocked, posthog_runs, posthog_funnel, internal, now, weeks=8) -> dict` and the row shapes below (used verbatim by Task 5's SQL):

```
companies:        {id, name, created_at}
routines:         {company_id, id, title, status, auto_paused: bool, created_at, updated_at,
                   has_enabled_trigger: bool, trigger_kinds: str|None}
runs:             {company_id, week: 'YYYY-MM-DD', kind: 'autonomous'|'human'|'nightly_ceo', ok: int, total: int}
first_autonomous: {company_id, first_ok: iso}
gate_blocked:     {code, n}
posthog_runs:     [[week, kind, n], ...]        (HogQL results)
posthog_funnel:   [[week, event, owners], ...]  (HogQL results)
```

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_growth.py
"""derive(): the DECIDED metric set over fixture rows.

Fixture: 3 external owners + 1 internal.
  ext_a  paid, container ready, 1 armed routine, autonomous successes in each of the
         last 4 completed weeks -> activated + counts toward the headline bar
  ext_b  paid, container ready, nightly-CEO only (must NOT count), one manual run
  ext_c  expired trial, never ready
  int_x  internal, paid, armed, running every week -> must be absent from external
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import growth  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # a Wednesday; week = 2026-08-24


def _week(n_back: int) -> str:
    monday = (NOW - timedelta(days=NOW.weekday())).date() - timedelta(weeks=n_back)
    return monday.isoformat()


def _fixture():
    billing = [
        {"owner_id": "ext_a", "subscription_status": "active", "plan": "standard", "provider_choice": "chatgpt_oauth",
         "created_at": "2026-07-01T00:00:00Z", "nightly_ceo_armed_company": "c_a"},
        {"owner_id": "ext_b", "subscription_status": "active", "plan": "ultra_pro", "provider_choice": "byo_key",
         "created_at": "2026-08-20T00:00:00Z", "nightly_ceo_opted_out": True},
        {"owner_id": "ext_c", "subscription_status": "trial_local", "trial_end": 1_700_000_000,
         "created_at": "2026-08-20T00:00:00Z"},
        {"owner_id": "int_x", "subscription_status": "active", "plan": "standard", "provider_choice": "chatgpt_oauth",
         "created_at": "2026-07-01T00:00:00Z"},
    ]
    containers = [
        {"owner_id": "ext_a", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
        {"owner_id": "ext_b", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
        {"owner_id": "ext_c", "status": "stopped"},
        {"owner_id": "int_x", "status": "running", "last_gateway_handshake_at": "2026-08-26T01:00:00Z"},
    ]
    paperclip = [
        {"user_id": "ext_a", "company_id": "c_a", "seeded_agent_ids": ["f"], "status": "active", "bootstrap_state": "delivered"},
        {"user_id": "ext_b", "company_id": "c_b", "seeded_agent_ids": ["f"], "status": "active", "bootstrap_state": "failed"},
        {"user_id": "ext_c", "company_id": "c_c", "seeded_agent_ids": ["f"], "status": "active", "bootstrap_state": "not_started"},
        {"user_id": "u_int", "org_id": "int_x", "company_id": "c_x", "seeded_agent_ids": ["f"], "status": "active", "bootstrap_state": "delivered"},
        {"user_id": "__pending_brief__:zzz"},
    ]
    integrations = [
        {"owner_id": "ext_a", "provider": "github", "status": "connected"},
        {"owner_id": "ext_a", "provider": "stripe", "status": "connected"},
        {"owner_id": "ext_b", "provider": "google", "status": "connected"},
    ]
    companies = [
        {"id": "c_a", "name": "Alpha"}, {"id": "c_b", "name": "Beta"}, {"id": "c_c", "name": "Gamma"}, {"id": "c_x", "name": "Isol8"},
    ]
    old = "2026-06-01T00:00:00Z"
    routines = [
        {"company_id": "c_a", "id": "r1", "title": "Daily report", "status": "active", "auto_paused": False,
         "created_at": old, "updated_at": old, "has_enabled_trigger": True, "trigger_kinds": "schedule"},
        {"company_id": "c_a", "id": "r2", "title": "Old one", "status": "archived", "auto_paused": False,
         "created_at": "2026-08-20T00:00:00Z", "updated_at": "2026-08-22T00:00:00Z", "has_enabled_trigger": False, "trigger_kinds": None},
        {"company_id": "c_b", "id": "r3", "title": growth.NIGHTLY_CEO_ROUTINE_TITLE, "status": "active", "auto_paused": False,
         "created_at": old, "updated_at": old, "has_enabled_trigger": True, "trigger_kinds": "schedule"},
        {"company_id": "c_b", "id": "r4", "title": "Broken", "status": "paused", "auto_paused": True,
         "created_at": old, "updated_at": "2026-08-10T00:00:00Z", "has_enabled_trigger": True, "trigger_kinds": "webhook"},
        {"company_id": "c_x", "id": "r5", "title": "Internal", "status": "active", "auto_paused": False,
         "created_at": old, "updated_at": old, "has_enabled_trigger": True, "trigger_kinds": "schedule"},
    ]
    runs = []
    for back in range(0, 6):  # current week + 5 previous
        runs.append({"company_id": "c_a", "week": _week(back), "kind": "autonomous", "ok": 2, "total": 3})
        runs.append({"company_id": "c_x", "week": _week(back), "kind": "autonomous", "ok": 9, "total": 9})
    runs.append({"company_id": "c_b", "week": _week(1), "kind": "nightly_ceo", "ok": 5, "total": 5})
    runs.append({"company_id": "c_b", "week": _week(1), "kind": "human", "ok": 1, "total": 1})
    first_autonomous = [{"company_id": "c_a", "first_ok": "2026-07-10T00:00:00Z"}, {"company_id": "c_x", "first_ok": "2026-07-01T00:00:00Z"}]
    gate_blocked = [{"code": "trial_expired", "n": 7}]
    posthog_runs = [[_week(1), "autonomous", 2], [_week(1), "human", 10]]
    posthog_funnel = [[_week(1), "trial_started", 4]]
    posthog_sources = [["reddit.com", 3], ["direct", 2]]
    return dict(billing=billing, containers=containers, paperclip=paperclip, integrations=integrations,
                companies=companies, routines=routines, runs=runs, first_autonomous=first_autonomous,
                gate_blocked=gate_blocked, posthog_runs=posthog_runs, posthog_funnel=posthog_funnel,
                posthog_sources=posthog_sources, internal={"int_x"}, now=NOW)


def test_week_of_is_monday():
    assert growth.week_of("2026-08-26T12:00:00Z") == "2026-08-24"
    assert growth.week_of("2026-08-24T00:00:00+00:00") == "2026-08-24"
    assert growth.week_of(None) is None


def test_segments_exclude_internal_from_external():
    out = growth.derive(**_fixture())
    ext, intl = out["segments"]["external"], out["segments"]["internal"]
    assert ext["owners"]["total"] == 3 and intl["owners"]["total"] == 1
    assert "int_x" not in [o["owner_id"] for o in ext["activation"]["owners"]]


def test_owner_counts_and_signups():
    ext = growth.derive(**_fixture())["segments"]["external"]["owners"]
    assert ext["paid"] == 2 and ext["paid_by_plan"] == {"standard": 1, "ultra_pro": 1}
    assert ext["trial_active"] == 0 and ext["trial_expired"] == 1
    assert ext["paid_by_provider"] == {"chatgpt_oauth": 1, "byo_key": 1}
    assert {"week": "2026-08-17", "n": 2} in ext["signups_by_week"]


def test_activation_stages():
    ext = growth.derive(**_fixture())["segments"]["external"]["activation"]
    by_owner = {o["owner_id"]: o for o in ext["owners"]}
    assert by_owner["ext_a"]["stage"] == "first_autonomous_success"
    assert by_owner["ext_b"]["stage"] == "container_ready"  # kickoff FAILED, and nightly CEO does not count as armed
    assert by_owner["ext_b"]["bootstrap_state"] == "failed"
    assert by_owner["ext_c"]["stage"] == "signed_up"
    assert by_owner["ext_a"]["name"] == "Alpha"
    assert ext["stages"] == {
        "signed_up": 3, "subscribed": 2, "container_ready": 2, "friday_first_run_delivered": 1,
        "automation_armed": 1, "first_autonomous_success": 1,
    }


def test_north_star_excludes_nightly_and_headline_four_weeks():
    out = growth.derive(**_fixture())
    ext = out["segments"]["external"]
    last_completed = next(w for w in ext["north_star"]["weeks"] if w["week"] == _week(1))
    assert last_completed == {"week": _week(1), "owners_active": 1, "autonomous_ok": 2, "human_ok": 1, "nightly_ok": 5,
                              "autonomous_share": 2 / 3}
    assert ext["headline"] == {"bar": 5, "count": 1, "owners": ["Alpha"], "weeks": [_week(4), _week(3), _week(2), _week(1)]}
    assert out["segments"]["internal"]["headline"]["owners"] == ["Isol8"]


def test_retention_survival_and_depth():
    ext = growth.derive(**_fixture())["segments"]["external"]
    assert ext["retention"]["automation_survival"] == {"armed": 1, "paused_manual": 0, "auto_paused": 1, "archived": 0}
    assert ext["depth"]["armed_per_owner"] == [{"owner_id": "ext_a", "name": "Alpha", "armed": 1, "trigger_kinds": ["schedule"]}]
    assert ext["depth"]["providers_per_owner"] == [
        {"owner_id": "ext_a", "name": "Alpha", "providers": ["github", "stripe"]},
        {"owner_id": "ext_b", "name": "Beta", "providers": ["google"]},
    ]


def test_counters_and_nightly():
    ext = growth.derive(**_fixture())["segments"]["external"]
    assert ext["counters"]["auto_paused"] == [{"owner_id": "ext_b", "name": "Beta", "title": "Broken", "since": "2026-08-10T00:00:00Z"}]
    assert ext["counters"]["archived_within_7d"] == [{"owner_id": "ext_a", "name": "Alpha", "title": "Old one"}]
    assert ext["nightly_ceo"] == {"armed": 1, "opted_out": 1, "never_armed": 0}


def test_top_level_crosschecks_pass_through():
    out = growth.derive(**_fixture())
    assert out["gate_blocked_7d"] == [{"code": "trial_expired", "n": 7}]
    assert out["posthog"]["runs"] == [{"week": _week(1), "kind": "autonomous", "n": 2}, {"week": _week(1), "kind": "human", "n": 10}]
    assert out["posthog"]["funnel"] == [{"week": _week(1), "event": "trial_started", "owners": 4}]
    assert out["posthog"]["signup_sources"] == [{"source": "reddit.com", "owners": 3}, {"source": "direct", "owners": 2}]
    assert out["window_weeks"] == 8 and out["generated_at"].startswith("2026-08-26")


def test_never_armed_counts_paid_owners_with_no_nightly_marker():
    fx = _fixture()
    fx["billing"].append({"owner_id": "ext_d", "subscription_status": "active", "plan": "standard", "created_at": "2026-08-01T00:00:00Z"})
    ext = growth.derive(**fx)["segments"]["external"]
    assert ext["nightly_ceo"] == {"armed": 1, "opted_out": 1, "never_armed": 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_growth.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.ops.growth`

- [ ] **Step 3: Write `growth.py` (constants + `derive`)**

```python
# apps/backend/core/services/ops/growth.py
"""Growth / GTM metrics for the admin console — computed on read.

Definitions are DECIDED in docs/growth/2026-08-17-blitzscaling-strategy.md §3;
data-source research is docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md §0.1.

Sources: DynamoDB scans (owners, plans, readiness, connected providers),
the Paperclip Aurora DB via the Data API (routines + runs — the ledger for
autonomous-run attribution), PostHog HogQL (client funnel and a lossy
cross-check — never a headline), CloudWatch (gate-blocked codes).

Everything is segmented internal/external BEFORE aggregation.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE

logger = logging.getLogger(__name__)

WINDOW_WEEKS = 8
HEADLINE_BAR = 5  # "five external owners with armed automations firing for four consecutive weeks"
HEADLINE_WEEKS = 4
_PAID = frozenset({"active", "trialing"})  # = provision_gate.is_paid_subscription

# --- SQL (Paperclip fork schema at the pinned tag; see paperclip_sql docstring) ---
SQL_COMPANIES = "SELECT id, name, created_at FROM companies"

SQL_ROUTINES = """
SELECT r.company_id, r.id, r.title, r.status,
       (r.auto_paused_at IS NOT NULL) AS auto_paused,
       r.created_at, r.updated_at,
       EXISTS (SELECT 1 FROM routine_triggers t WHERE t.routine_id = r.id AND t.enabled) AS has_enabled_trigger,
       (SELECT string_agg(DISTINCT t.kind, ',') FROM routine_triggers t WHERE t.routine_id = r.id) AS trigger_kinds
FROM routines r
LIMIT 5000
"""

# heartbeat_runs has no routine_id: attribution is issue -> dispatching routine_run -> source.
# ponytail: LEFT JOIN routine_runs on linked_issue_id can double-count an issue that
# two routine_runs link (coalesced runs carry no linked issue today); revisit if seen.
SQL_RUNS_BY_WEEK = f"""
SELECT hr.company_id,
       to_char(date_trunc('week', hr.created_at), 'YYYY-MM-DD') AS week,
       CASE WHEN r.title = '{NIGHTLY_CEO_ROUTINE_TITLE}' THEN 'nightly_ceo'
            WHEN rr.source IN ('schedule', 'webhook') THEN 'autonomous'
            ELSE 'human' END AS kind,
       count(*) FILTER (WHERE hr.status = 'succeeded') AS ok,
       count(*) AS total
FROM heartbeat_runs hr
LEFT JOIN issues i ON i.id::text = hr.context_snapshot->>'issueId'
LEFT JOIN routine_runs rr ON rr.linked_issue_id = i.id
LEFT JOIN routines r ON r.id = rr.routine_id
WHERE hr.created_at > now() - interval '{WINDOW_WEEKS + 1} weeks'
GROUP BY 1, 2, 3
"""

SQL_FIRST_AUTONOMOUS = f"""
SELECT hr.company_id, min(hr.created_at) AS first_ok
FROM heartbeat_runs hr
JOIN issues i ON i.id::text = hr.context_snapshot->>'issueId'
JOIN routine_runs rr ON rr.linked_issue_id = i.id
JOIN routines r ON r.id = rr.routine_id
WHERE hr.status = 'succeeded' AND rr.source IN ('schedule', 'webhook')
  AND r.title <> '{NIGHTLY_CEO_ROUTINE_TITLE}'
GROUP BY 1
"""

# --- HogQL (cross-check only). toStartOfWeek(., 1) = Monday, matching date_trunc('week'). ---
HOGQL_RUNS = f"""
select toStartOfWeek(timestamp, 1) as week, properties.initiation_type as kind, count() as n
from events
where event = 'agent_run_completed' and properties.outcome = 'success'
  and toString(properties.is_internal) != 'true'
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by week, kind order by week, kind limit 500
"""

HOGQL_FUNNEL = f"""
select toStartOfWeek(timestamp, 1) as week, event, uniq(distinct_id) as owners
from events
where event in ('landing_viewed', 'signup_started', 'trial_started', 'first_run_brief_submitted',
                'trial_first_agent_response', 'chat_completed', 'trial_converted',
                'subscription_activated', 'subscription_cancelled', 'automation_armed')
  and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by week, event order by week, event limit 500
"""

# Organic / referral share of signups (SEO plan §6). PostHog stamps first-touch
# person properties itself ($initial_*), so no frontend change is needed.
HOGQL_SIGNUP_SOURCES = f"""
select coalesce(nullIf(person.properties.$initial_utm_source, ''),
                nullIf(person.properties.$initial_referring_domain, ''), 'direct') as source,
       uniq(distinct_id) as owners
from events
where event = 'signup_started' and timestamp > now() - interval {WINDOW_WEEKS + 1} week
group by source order by owners desc limit 50
"""


def week_of(ts: str | None) -> str | None:
    """Monday-start ISO date for an ISO-8601 timestamp (or None)."""
    if not ts:
        return None
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _weeks(now: datetime, n: int) -> list[str]:
    """The last n Monday buckets, oldest first, ending with the current (partial) week."""
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday - timedelta(weeks=i)).isoformat() for i in range(n - 1, -1, -1)]


def _iso(v) -> str | None:
    return None if v is None else str(v)


def derive(
    *,
    billing: list[dict],
    containers: list[dict],
    paperclip: list[dict],
    integrations: list[dict],
    companies: list[dict],
    routines: list[dict],
    runs: list[dict],
    first_autonomous: list[dict],
    gate_blocked: list[dict],
    posthog_runs: list[list],
    posthog_funnel: list[list],
    posthog_sources: list[list],
    internal: set[str],
    now: datetime,
    weeks: int = WINDOW_WEEKS,
) -> dict:
    # company <-> owner. Org members share a company; prefer the seeded (owner) row.
    company_owner: dict[str, str] = {}
    for row in paperclip:
        uid = str(row.get("user_id") or "")
        cid = row.get("company_id")
        if uid.startswith("__") or not cid:
            continue
        owner = row.get("org_id") or uid
        if cid not in company_owner or row.get("seeded_agent_ids"):
            company_owner[cid] = owner
    owner_company = {o: c for c, o in company_owner.items()}
    company_name = {c["id"]: c.get("name") for c in companies}

    def name_of(owner_id: str) -> str:
        return company_name.get(owner_company.get(owner_id, ""), None) or owner_id

    container_by_owner = {c["owner_id"]: c for c in containers}
    # Friday's kickoff outcome lives on the OWNER's paperclip-companies row
    # (org rows: the one carrying seeded_agent_ids).
    bootstrap_by_owner: dict[str, str] = {}
    for row in paperclip:
        uid = str(row.get("user_id") or "")
        if uid.startswith("__"):
            continue
        owner = row.get("org_id") or uid
        if owner not in bootstrap_by_owner or row.get("seeded_agent_ids"):
            bootstrap_by_owner[owner] = row.get("bootstrap_state") or "not_started"
    routines_by_company: dict[str, list[dict]] = defaultdict(list)
    for r in routines:
        routines_by_company[r["company_id"]].append(r)
    first_ok_by_company = {r["company_id"]: _iso(r.get("first_ok")) for r in first_autonomous}
    providers_by_owner: dict[str, list[str]] = defaultdict(list)
    for it in integrations:
        if it.get("status", "connected") == "connected":
            providers_by_owner[it["owner_id"]].append(it["provider"])
    week_list = _weeks(now, weeks)
    headline_weeks = week_list[-HEADLINE_WEEKS - 1 : -1]  # last 4 COMPLETED weeks

    def is_armed(r: dict) -> bool:
        return r["status"] == "active" and bool(r["has_enabled_trigger"]) and r["title"] != NIGHTLY_CEO_ROUTINE_TITLE

    def segment(rows: list[dict]) -> dict:
        owner_ids = [b["owner_id"] for b in rows]
        owner_set = set(owner_ids)
        company_set = {owner_company[o] for o in owner_ids if o in owner_company}

        paid = [b for b in rows if b.get("subscription_status") in _PAID]
        trial = [b for b in rows if b.get("subscription_status") == "trial_local"]
        trial_expired = [b for b in trial if float(b.get("trial_end") or 0) < now.timestamp()]
        signups = Counter(week_of(b.get("created_at")) for b in rows)
        owners = {
            "total": len(rows),
            "paid": len(paid),
            "paid_by_plan": dict(Counter(b.get("plan") or "standard" for b in paid)),
            "paid_by_provider": dict(Counter(b.get("provider_choice") or "none" for b in paid)),
            "trial_active": len(trial) - len(trial_expired),
            "trial_expired": len(trial_expired),
            "signups_by_week": [{"week": w, "n": signups.get(w, 0)} for w in week_list],
        }

        # Activation gate — furthest stage reached, in order.
        stage_names = [
            "signed_up", "subscribed", "container_ready", "friday_first_run_delivered",
            "automation_armed", "first_autonomous_success",
        ]
        owner_rows, stage_counts = [], Counter()
        for b in rows:
            oid = b["owner_id"]
            cid = owner_company.get(oid, "")
            flags = [
                True,
                b.get("subscription_status") in _PAID,
                container_by_owner.get(oid, {}).get("last_gateway_handshake_at") is not None,
                bootstrap_by_owner.get(oid) == "delivered",
                any(is_armed(r) for r in routines_by_company.get(cid, [])),
                first_ok_by_company.get(cid) is not None,
            ]
            reached = 0
            for i, f in enumerate(flags):
                if not f:
                    break
                reached = i
            for s in stage_names[: reached + 1]:
                stage_counts[s] += 1
            owner_rows.append({
                "owner_id": oid, "name": name_of(oid), "stage": stage_names[reached],
                "plan": b.get("plan"), "status": b.get("subscription_status"),
                "bootstrap_state": bootstrap_by_owner.get(oid, "not_started"),
                "first_autonomous_at": first_ok_by_company.get(cid),
            })
        owner_rows.sort(key=lambda o: (-stage_names.index(o["stage"]), o["name"]))
        activation = {"stages": {s: stage_counts.get(s, 0) for s in stage_names}, "owners": owner_rows}

        # North Star — per week, external successful runs by kind; nightly CEO reported, never counted.
        seg_runs = [r for r in runs if r["company_id"] in company_set]
        active_by_week: dict[str, set[str]] = defaultdict(set)
        sums: dict[str, Counter] = defaultdict(Counter)
        for r in seg_runs:
            ok = int(r.get("ok") or 0)
            sums[r["week"]][r["kind"]] += ok
            if r["kind"] == "autonomous" and ok > 0:
                active_by_week[r["week"]].add(company_owner[r["company_id"]])
        ns_weeks = []
        for w in week_list:
            a, h = sums[w]["autonomous"], sums[w]["human"]
            ns_weeks.append({
                "week": w, "owners_active": len(active_by_week[w]),
                "autonomous_ok": a, "human_ok": h, "nightly_ok": sums[w]["nightly_ceo"],
                "autonomous_share": (a / (a + h)) if (a + h) else None,
            })
        consecutive = sorted(
            name_of(o) for o in owner_set if all(o in active_by_week[w] for w in headline_weeks)
        )
        headline = {"bar": HEADLINE_BAR, "count": len(consecutive), "owners": consecutive, "weeks": headline_weeks}

        # Retention — weekly active owners is ns_weeks.owners_active; survival over routines ≥30d old.
        cutoff = now - timedelta(days=30)
        survival = Counter()
        for cid in company_set:
            for r in routines_by_company.get(cid, []):
                if r["title"] == NIGHTLY_CEO_ROUTINE_TITLE:
                    continue
                if datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00").replace(" ", "T")) > cutoff:
                    continue
                if r["status"] == "archived":
                    survival["archived"] += 1
                elif r["status"] == "paused":
                    survival["auto_paused" if r["auto_paused"] else "paused_manual"] += 1
                elif is_armed(r):
                    survival["armed"] += 1
        retention = {
            "weekly_active_owners": [{"week": w["week"], "n": w["owners_active"]} for w in ns_weeks],
            "automation_survival": {k: survival.get(k, 0) for k in ("armed", "paused_manual", "auto_paused", "archived")},
        }

        # Depth
        armed_per_owner = []
        for oid in sorted(owner_set, key=name_of):
            armed = [r for r in routines_by_company.get(owner_company.get(oid, ""), []) if is_armed(r)]
            if armed:
                kinds = sorted({k for r in armed for k in (r.get("trigger_kinds") or "").split(",") if k})
                armed_per_owner.append({"owner_id": oid, "name": name_of(oid), "armed": len(armed), "trigger_kinds": kinds})
        providers_per_owner = [
            {"owner_id": oid, "name": name_of(oid), "providers": sorted(providers_by_owner[oid])}
            for oid in sorted(owner_set, key=name_of) if providers_by_owner.get(oid)
        ]
        depth = {"armed_per_owner": armed_per_owner, "providers_per_owner": providers_per_owner}

        # Counter-metrics
        auto_paused, archived_7d = [], []
        for cid in sorted(company_set, key=lambda c: name_of(company_owner[c])):
            oid = company_owner[cid]
            for r in routines_by_company.get(cid, []):
                if r["status"] == "paused" and r["auto_paused"]:
                    auto_paused.append({"owner_id": oid, "name": name_of(oid), "title": r["title"], "since": _iso(r.get("updated_at"))})
                if r["status"] == "archived":
                    created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00").replace(" ", "T"))
                    updated = datetime.fromisoformat(str(r["updated_at"]).replace("Z", "+00:00").replace(" ", "T"))
                    if updated - created <= timedelta(days=7):
                        archived_7d.append({"owner_id": oid, "name": name_of(oid), "title": r["title"]})
        counters = {"auto_paused": auto_paused, "archived_within_7d": archived_7d}

        # "never_armed" is the bug class found 2026-08-26 (paid owners the arm
        # never reached) — it must stay visible, not fold into "opted out".
        nightly = {
            "armed": sum(1 for b in paid if b.get("nightly_ceo_armed_company") and not b.get("nightly_ceo_opted_out")),
            "opted_out": sum(1 for b in paid if b.get("nightly_ceo_opted_out")),
            "never_armed": sum(
                1 for b in paid if not b.get("nightly_ceo_armed_company") and not b.get("nightly_ceo_opted_out")
            ),
        }
        return {
            "owners": owners, "activation": activation, "north_star": {"weeks": ns_weeks},
            "headline": headline, "retention": retention, "depth": depth, "counters": counters, "nightly_ceo": nightly,
        }

    ext_rows = [b for b in billing if b["owner_id"] not in internal]
    int_rows = [b for b in billing if b["owner_id"] in internal]
    return {
        "generated_at": now.isoformat(),
        "window_weeks": weeks,
        "weeks": week_list,
        "segments": {"external": segment(ext_rows), "internal": segment(int_rows)},
        "internal_owner_ids": sorted(internal),
        "gate_blocked_7d": gate_blocked,
        "posthog": {
            "runs": [{"week": str(r[0])[:10], "kind": r[1], "n": r[2]} for r in posthog_runs],
            "funnel": [{"week": str(r[0])[:10], "event": r[1], "owners": r[2]} for r in posthog_funnel],
            "signup_sources": [{"source": r[0], "owners": r[1]} for r in posthog_sources],
        },
    }
```


- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_growth.py -v`
Expected: PASS (if a timestamp assertion fails on `+00:00` vs `Z`, keep the fixture's string form: `_iso` returns the raw value untouched)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/ops/growth.py apps/backend/tests/unit/services/test_growth.py
git commit -m "feat(ops): growth metrics derive() — decided GTM metric set over fleet rows"
```

---

### Task 5: `growth.compute()` + `GET /ops/growth`

**Files:**
- Modify: `apps/backend/core/services/ops/growth.py` (append `compute`)
- Modify: `apps/backend/routers/ops.py` (append route)
- Test: `apps/backend/tests/unit/services/test_growth.py` (append), `apps/backend/tests/unit/routers/test_ops_growth.py`

**Interfaces:**
- Consumes: `paperclip_sql.select_rows`, `billing_repo.scan_all`, `container_repo.scan_all`, `PaperclipRepo.scan_all_items`, `integrations_repo.scan_all`, `posthog_admin.hogql`, `run_analytics.is_internal_owner`, `gate_blocked.last_7d` (Task 6 — until it lands, `compute` imports it lazily and treats `ImportError` as an unavailable source).
- Produces: `async def compute() -> tuple[dict | None, str, list[dict]]` → `(data, source_state, errors)`; route `GET /api/v1/ops/growth` returning `envelope(...)`.

- [ ] **Step 1: Write the failing tests** (append to `test_growth.py`)

```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_compute_partial_when_aurora_fails(monkeypatch):
    growth._cache = None
    fx = _fixture()

    async def boom(*a, **k):
        raise RuntimeError("data api down")

    with (
        patch("core.repositories.billing_repo.scan_all", AsyncMock(return_value=fx["billing"])),
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=fx["containers"])),
        patch("core.repositories.paperclip_repo.PaperclipRepo.scan_all_items", AsyncMock(return_value=fx["paperclip"])),
        patch("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=fx["integrations"])),
        patch("core.services.paperclip_sql.select_rows", boom),
        patch("core.services.posthog_admin.hogql", AsyncMock(return_value={"columns": [], "results": [], "stubbed": True, "error": None})),  # all three queries
        patch("core.services.ops.gate_blocked.last_7d", AsyncMock(return_value=[])),
        patch("core.services.run_analytics.is_internal_owner", AsyncMock(side_effect=lambda o: o == "int_x")),
    ):
        data, state, errors = await growth.compute()

    assert state == "partial"
    assert [e["domain"] for e in errors] == ["aurora"]
    assert data["segments"]["external"]["owners"]["total"] == 3
    assert data["segments"]["external"]["activation"]["stages"]["automation_armed"] == 0  # no routine rows


@pytest.mark.asyncio
async def test_compute_unavailable_when_ddb_fails():
    growth._cache = None

    async def boom(*a, **k):
        raise RuntimeError("ddb down")

    with patch("core.repositories.billing_repo.scan_all", boom):
        data, state, errors = await growth.compute()
    assert data is None and state == "unavailable" and errors[0]["domain"] == "ddb"


@pytest.mark.asyncio
async def test_compute_is_cached():
    growth._cache = (float("inf"), {"cached": True}, "ok", [])
    assert await growth.compute() == ({"cached": True}, "ok", [])
    growth._cache = None
```

And the router test:

```python
# apps/backend/tests/unit/routers/test_ops_growth.py
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, require_platform_admin  # noqa: E402
from routers import ops  # noqa: E402


@pytest.mark.asyncio
async def test_growth_route_wraps_envelope():
    app = FastAPI()
    app.include_router(ops.router, prefix="/api/v1/ops")
    app.dependency_overrides[require_platform_admin] = lambda: AuthContext(user_id="u", email="a@isol8.co")  # match the real ctor
    with patch("core.services.ops.growth.compute", AsyncMock(return_value=({"x": 1}, "partial", [{"domain": "aurora", "reason": "x"}]))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/ops/growth")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {"x": 1} and body["source_state"] == "partial" and body["errors"][0]["domain"] == "aurora"
```

Check `AuthContext`'s constructor in `core/auth.py` and pass the fields it requires (it is a dataclass — read it; if it needs more than `user_id`/`email`, supply them).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_growth.py -k compute tests/unit/routers/test_ops_growth.py -v`
Expected: FAIL with `AttributeError: module 'core.services.ops.growth' has no attribute 'compute'`

- [ ] **Step 3: Implement `compute()`** (append to `growth.py`)

```python
import asyncio
import time

from core.services.ops import envelope as env

_CACHE_TTL_S = 60
_cache: tuple[float, dict | None, str, list[dict]] | None = None  # (expires_at, data, state, errors)


async def _read_ddb() -> dict:
    from core.repositories import billing_repo, container_repo, integrations_repo
    from core.repositories.paperclip_repo import PaperclipRepo

    billing, containers, paperclip, integrations = await asyncio.gather(
        billing_repo.scan_all(),
        container_repo.scan_all(),
        PaperclipRepo(table_name="paperclip-companies").scan_all_items(),
        integrations_repo.scan_all(),
    )
    return {"billing": billing, "containers": containers, "paperclip": paperclip, "integrations": integrations}


async def _read_aurora() -> dict:
    from core.services.paperclip_sql import select_rows

    companies, routines, runs, first = await asyncio.gather(
        select_rows(SQL_COMPANIES), select_rows(SQL_ROUTINES), select_rows(SQL_RUNS_BY_WEEK), select_rows(SQL_FIRST_AUTONOMOUS)
    )
    return {"companies": companies, "routines": routines, "runs": runs, "first_autonomous": first}


async def _read_posthog() -> dict:
    from core.services.posthog_admin import hogql

    runs, funnel, sources = await asyncio.gather(hogql(HOGQL_RUNS), hogql(HOGQL_FUNNEL), hogql(HOGQL_SIGNUP_SOURCES))
    for name, res in (("runs", runs), ("funnel", funnel), ("sources", sources)):
        if res["error"]:
            raise RuntimeError(f"hogql {name}: {res['error']}")
    return {"runs": runs["results"], "funnel": funnel["results"], "sources": sources["results"]}


async def _read_gate_blocked() -> list[dict]:
    from core.services.ops import gate_blocked

    return await gate_blocked.last_7d()


async def _internal_set(owner_ids: list[str]) -> set[str]:
    from core.services.run_analytics import is_internal_owner

    flags = await asyncio.gather(*(is_internal_owner(o) for o in owner_ids))
    return {o for o, f in zip(owner_ids, flags) if f}


async def compute() -> tuple[dict | None, str, list[dict]]:
    """Read every source (each fail-safe via envelope.read_source), derive, cache 60s.

    DDB is load-bearing (no owners → nothing to segment) → ``unavailable``.
    Any other source failing → ``partial`` with the domain in errors, and that
    section renders empty on the page rather than green.
    """
    global _cache
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2], _cache[3]

    (ddb, ddb_state, ddb_err), (aurora, _, aurora_err), (ph, _, ph_err), (gate, _, gate_err) = await asyncio.gather(
        env.read_source("ddb", _read_ddb, timeout_s=10.0),
        env.read_source("aurora", _read_aurora, timeout_s=15.0),
        env.read_source("posthog", _read_posthog, timeout_s=20.0),
        env.read_source("cloudwatch", _read_gate_blocked, timeout_s=8.0),
    )
    if ddb is None:
        return None, env.UNAVAILABLE, ddb_err
    errors = aurora_err + ph_err + gate_err
    state = env.PARTIAL if errors else env.OK

    aurora = aurora or {"companies": [], "routines": [], "runs": [], "first_autonomous": []}
    ph = ph or {"runs": [], "funnel": [], "sources": []}
    internal = await _internal_set([b["owner_id"] for b in ddb["billing"]])
    data = derive(
        **ddb, **aurora, gate_blocked=gate or [], posthog_runs=ph["runs"], posthog_funnel=ph["funnel"],
        posthog_sources=ph["sources"], internal=internal, now=datetime.now(timezone.utc),
    )
    data["sources"] = {"ddb": ddb_state, "aurora": "ok" if not aurora_err else "unavailable",
                       "posthog": "ok" if not ph_err else "unavailable", "cloudwatch": "ok" if not gate_err else "unavailable"}
    _cache = (time.monotonic() + _CACHE_TTL_S, data, state, errors)
    return data, state, errors
```

Note `derive(**ddb, **aurora, ...)` relies on the dict keys matching `derive`'s keyword names exactly (`billing, containers, paperclip, integrations, companies, routines, runs, first_autonomous`).

- [ ] **Step 4: Add the route** (append to `routers/ops.py`)

```python
@router.get("/growth")
async def get_growth(_auth: AuthContext = Depends(require_platform_admin)):
    """Fleet GTM metrics (docs/growth §3), computed on read, 60s cache.
    Partial sources surface in errors[] — the page must never render a
    missing section as zero-and-green."""
    from core.services.ops import growth

    data, state, errors = await growth.compute()
    return env.envelope(data, source_state=state, errors=errors)
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_growth.py tests/unit/routers/test_ops_growth.py -v`
Expected: PASS. (Task 6's `gate_blocked` module does not exist yet: the compute test patches `core.services.ops.gate_blocked.last_7d`, so create Task 6's module before running, or run Task 6 first — the order below assumes Task 6 is executed immediately after this commit and the two are merged as one PR.)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/ops/growth.py apps/backend/routers/ops.py apps/backend/tests/unit/services/test_growth.py apps/backend/tests/unit/routers/test_ops_growth.py
git commit -m "feat(ops): GET /ops/growth — fleet GTM metrics computed on read"
```

---

### Task 6: `provision.gate.blocked` by code from CloudWatch + IAM grant

**Files:**
- Create: `apps/backend/core/services/ops/gate_blocked.py`
- Modify: `apps/infra/lib/stacks/service-stack.ts` (next to the `cloudwatch:DescribeAlarms` statement, ~line 882)
- Modify: `.github/workflows/deploy.yml` (regenerated by `cdk synth` — commit it)
- Test: `apps/backend/tests/unit/services/test_gate_blocked.py`, `apps/infra/test/service-stack.test.ts` (extend)

**Interfaces:**
- Produces: `async def last_7d() -> list[dict]` → `[{"code": str, "n": int}]` sorted by `n` desc, codes with 0 omitted.

- [ ] **Step 1: Write the failing backend test**

```python
# apps/backend/tests/unit/services/test_gate_blocked.py
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import gate_blocked  # noqa: E402


@pytest.mark.asyncio
async def test_last_7d_queries_each_code_with_full_dimension_set(monkeypatch):
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "prod")
    cw = MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "c_trial_expired", "Values": [4.0, 3.0]},
            {"Id": "c_oauth_required", "Values": []},
            {"Id": "c_subscription_required", "Values": [1.0]},
        ]
    }
    with patch("core.services.ops.gate_blocked._client", return_value=cw):
        out = await gate_blocked.last_7d()

    assert out == [{"code": "trial_expired", "n": 7}, {"code": "subscription_required", "n": 1}]
    queries = cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    assert len(queries) == len(gate_blocked.GATE_CODES)
    q = next(q for q in queries if q["Id"] == "c_trial_expired")
    assert q["MetricStat"]["Metric"] == {
        "Namespace": "Isol8",
        "MetricName": "provision.gate.blocked",
        "Dimensions": [
            {"Name": "env", "Value": "prod"},
            {"Name": "service", "Value": "isol8-backend"},
            {"Name": "code", "Value": "trial_expired"},
        ],
    }
    assert q["MetricStat"]["Stat"] == "Sum" and q["MetricStat"]["Period"] == 604800
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_gate_blocked.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.ops.gate_blocked`

- [ ] **Step 3: Implement**

```python
# apps/backend/core/services/ops/gate_blocked.py
"""``provision.gate.blocked`` by code over the last 7 days, from CloudWatch.

The gate emits the metric via EMF only (core/services/provision_gate.py:411,
``put_metric("provision.gate.blocked", dimensions={"code": ...})``); nothing
persists the code to DynamoDB, so CloudWatch is the only reader. The
emitter publishes the FULL dimension set ``[env, service, code]``
(core/observability/metrics.py), which is what GetMetricData must match
exactly. One MetricDataQuery per known code (≤500 per call).

ponytail: explicit code list — switch to a SEARCH() expression if codes
multiply; today there are 11 and they are literals in provision_gate.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

GATE_CODES: tuple[str, ...] = (
    "byo_provider_required", "credits_required", "invitation_pending", "oauth_required",
    "payment_past_due", "provider_required", "signup_required", "subscription_required",
    "team_billing_required", "trial_daily_cap", "trial_expired",
)
_PERIOD_S = 7 * 24 * 3600


def _client():
    return boto3.client("cloudwatch", region_name=settings.AWS_REGION)


async def last_7d() -> list[dict]:
    env_name = (settings.ENVIRONMENT or "dev").lower()  # mirrors metrics._get_env
    end = datetime.now(timezone.utc)
    queries = [
        {
            "Id": f"c_{code}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "Isol8",
                    "MetricName": "provision.gate.blocked",
                    "Dimensions": [
                        {"Name": "env", "Value": env_name},
                        {"Name": "service", "Value": "isol8-backend"},
                        {"Name": "code", "Value": code},
                    ],
                },
                "Period": _PERIOD_S,
                "Stat": "Sum",
            },
        }
        for code in GATE_CODES
    ]
    resp = await run_in_thread(
        _client().get_metric_data,
        MetricDataQueries=queries,
        StartTime=end - timedelta(seconds=_PERIOD_S),
        EndTime=end,
    )
    counts = {r["Id"][2:]: int(sum(r.get("Values") or [])) for r in resp.get("MetricDataResults", [])}
    return sorted(
        ({"code": c, "n": n} for c, n in counts.items() if n > 0),
        key=lambda x: (-x["n"], x["code"]),
    )
```

- [ ] **Step 4: Run backend tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_gate_blocked.py tests/unit/services/test_growth.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing infra assertion** — open `apps/infra/test/service-stack.test.ts`, find the existing test that asserts on `cloudwatch:DescribeAlarms` (grep it), and add beside it:

```ts
test("backend task role can read CloudWatch metric data for the admin growth read", () => {
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({ Action: "cloudwatch:GetMetricData", Effect: "Allow", Resource: "*" }),
      ]),
    }),
  });
});
```

(Use the same `template` fixture and `Match` import that file already uses.)

Run: `cd apps/infra && npx vitest run test/service-stack.test.ts` (or `npx jest test/service-stack.test.ts` — use whichever runner `apps/infra/package.json` `test` script names)
Expected: FAIL — no statement with `cloudwatch:GetMetricData`

- [ ] **Step 6: Add the grant** in `service-stack.ts`, directly after the `cloudwatch:DescribeAlarms` statement:

```ts
    // Admin growth read (routers/ops.py GET /growth): provision.gate.blocked
    // counts by code come from CloudWatch — the gate persists nothing else.
    // GetMetricData has no resource-level scoping, so "*" is the only shape.
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AdminGrowthCloudWatchRead",
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:GetMetricData"],
        resources: ["*"],
      }),
    );
```

- [ ] **Step 7: Regenerate `deploy.yml` and run infra tests**

Run: `cd apps/infra && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml && npm test`
Expected: tests PASS; `deploy.yml` shows modified only if the synth output changed (an IAM statement alone usually does not touch the workflow — commit it if it did, that is the CI drift guard).

- [ ] **Step 8: Commit**

```bash
git add apps/backend/core/services/ops/gate_blocked.py apps/backend/tests/unit/services/test_gate_blocked.py apps/infra/lib/stacks/service-stack.ts apps/infra/test/service-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(ops): gate-blocked codes from CloudWatch + GetMetricData grant"
```

---

### Task 7: Frontend — `getGrowth` + `/admin/growth` page + nav

**Files:**
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` (types after `FleetData`, helper after `getAlarms`)
- Create: `apps/frontend/src/app/admin/growth/sections.tsx`
- Create: `apps/frontend/src/app/admin/growth/page.tsx`
- Modify: `apps/frontend/src/app/admin/layout.tsx:59-93` (add a nav `<Link>` after Users)

**Interfaces:**
- Consumes: `OpsEnvelope<T>`, `adminFetch`, `Card`, `MetricStat`, `Tile`, `ErrorBanner` (existing).
- Produces: `getGrowth(token): Promise<OpsEnvelope<GrowthData> | null>`; `GrowthData` mirrors Task 4/5's dict exactly.

- [ ] **Step 1: Types + fetcher** (`api.ts`)

```ts
export interface GrowthWeekRow {
  week: string;
  owners_active: number;
  autonomous_ok: number;
  human_ok: number;
  nightly_ok: number;
  autonomous_share: number | null;
}
export interface GrowthOwnerStage {
  owner_id: string;
  name: string;
  stage:
    | "signed_up"
    | "subscribed"
    | "container_ready"
    | "friday_first_run_delivered"
    | "automation_armed"
    | "first_autonomous_success";
  plan: string | null;
  status: string | null;
  bootstrap_state: string;
  first_autonomous_at: string | null;
}
export interface GrowthSegment {
  owners: {
    total: number;
    paid: number;
    paid_by_plan: Record<string, number>;
    paid_by_provider: Record<string, number>;
    trial_active: number;
    trial_expired: number;
    signups_by_week: { week: string; n: number }[];
  };
  activation: { stages: Record<GrowthOwnerStage["stage"], number>; owners: GrowthOwnerStage[] };
  north_star: { weeks: GrowthWeekRow[] };
  headline: { bar: number; count: number; owners: string[]; weeks: string[] };
  retention: {
    weekly_active_owners: { week: string; n: number }[];
    automation_survival: { armed: number; paused_manual: number; auto_paused: number; archived: number };
  };
  depth: {
    armed_per_owner: { owner_id: string; name: string; armed: number; trigger_kinds: string[] }[];
    providers_per_owner: { owner_id: string; name: string; providers: string[] }[];
  };
  counters: {
    auto_paused: { owner_id: string; name: string; title: string; since: string | null }[];
    archived_within_7d: { owner_id: string; name: string; title: string }[];
  };
  nightly_ceo: { armed: number; opted_out: number; never_armed: number };
}
export interface GrowthData {
  generated_at: string;
  window_weeks: number;
  weeks: string[];
  segments: { external: GrowthSegment; internal: GrowthSegment };
  internal_owner_ids: string[];
  gate_blocked_7d: { code: string; n: number }[];
  posthog: {
    runs: { week: string; kind: string; n: number }[];
    funnel: { week: string; event: string; owners: number }[];
    signup_sources: { source: string; owners: number }[];
  };
  sources: Record<"ddb" | "aurora" | "posthog" | "cloudwatch", string>;
}

export async function getGrowth(token: string): Promise<OpsEnvelope<GrowthData> | null> {
  return adminFetch<OpsEnvelope<GrowthData>>(token, "/ops/growth");
}
```

- [ ] **Step 2: Sections** (`growth/sections.tsx`) — presentational only, no fetching

```tsx
import Link from "next/link";

import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import type { GrowthData, GrowthSegment, GrowthOwnerStage } from "../_lib/api";

const STAGE_LABEL: Record<GrowthOwnerStage["stage"], string> = {
  signed_up: "signed up",
  subscribed: "subscribed",
  container_ready: "container ready",
  friday_first_run_delivered: "Friday's first run delivered",
  automation_armed: "automation armed",
  first_autonomous_success: "activated",
};

function Table({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  if (rows.length === 0) return <div className="text-xs text-zinc-500">none</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-zinc-500">
            {head.map((h) => (
              <th key={h} className="pb-1 pr-4 font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-zinc-800 text-zinc-200">
              {r.map((c, j) => (
                <td key={j} className="py-1 pr-4 tabular-nums">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function owner(o: { owner_id: string; name: string }) {
  return (
    <Link href={`/admin/users/${encodeURIComponent(o.owner_id)}`} className="text-zinc-100 underline-offset-2 hover:underline">
      {o.name}
    </Link>
  );
}

export function Headline({ seg }: { seg: GrowthSegment }) {
  const h = seg.headline;
  return (
    <Card title={`External owners with automations firing ${h.weeks.length} consecutive weeks`}>
      <div className="flex items-baseline gap-6">
        <MetricStat label={`of ${h.bar} (the PMF bar)`} value={h.count} tone={h.count === 0 ? "unknown" : "default"} />
        <div className="text-xs text-zinc-400">
          weeks {h.weeks[0]} → {h.weeks[h.weeks.length - 1]} · {h.owners.length ? h.owners.join(", ") : "nobody yet"}
        </div>
      </div>
    </Card>
  );
}

export function Owners({ seg }: { seg: GrowthSegment }) {
  const o = seg.owners;
  const fmt = (m: Record<string, number>) => Object.entries(m).map(([k, v]) => `${k} ${v}`).join(" · ") || "—";
  return (
    <Card title="Owners">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <MetricStat label="owners" value={o.total} />
        <MetricStat label="paying" value={o.paid} hint={fmt(o.paid_by_plan)} />
        <MetricStat label="paid by provider" value={fmt(o.paid_by_provider)} />
        <MetricStat label="trials active" value={o.trial_active} />
        <MetricStat label="trials expired" value={o.trial_expired} tone={o.trial_expired ? "unknown" : "default"} />
      </div>
    </Card>
  );
}

export function Activation({ seg }: { seg: GrowthSegment }) {
  const a = seg.activation;
  return (
    <Card title="Activation gate (subscribed → ready → automation armed → first autonomous run succeeds)">
      <div className="grid grid-cols-3 gap-4 md:grid-cols-6">
        {(Object.keys(STAGE_LABEL) as GrowthOwnerStage["stage"][]).map((s) => (
          <MetricStat key={s} label={STAGE_LABEL[s]} value={a.stages[s]} />
        ))}
      </div>
      <div className="mt-4">
        <Table
          head={["owner", "stage", "status", "plan", "Friday kickoff", "first autonomous success"]}
          rows={a.owners.map((o) => [
            owner(o),
            STAGE_LABEL[o.stage],
            o.status ?? "—",
            o.plan ?? "—",
            o.bootstrap_state,
            o.first_autonomous_at ?? "—",
          ])}
        />
      </div>
    </Card>
  );
}

export function NorthStar({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="North Star — successful runs per week (nightly CEO shown, never counted)">
      <Table
        head={["week", "owners w/ autonomous run", "autonomous ok", "human ok", "autonomous share", "nightly CEO ok"]}
        rows={seg.north_star.weeks.map((w) => [
          w.week,
          w.owners_active,
          w.autonomous_ok,
          w.human_ok,
          w.autonomous_share === null ? "—" : `${Math.round(w.autonomous_share * 100)}%`,
          w.nightly_ok,
        ])}
      />
    </Card>
  );
}

export function RetentionDepth({ seg }: { seg: GrowthSegment }) {
  const s = seg.retention.automation_survival;
  return (
    <Card title="Automation survival (routines ≥30 days old) · depth">
      <div className="grid grid-cols-4 gap-4">
        <MetricStat label="still armed" value={s.armed} />
        <MetricStat label="paused by owner" value={s.paused_manual} />
        <MetricStat label="auto-paused (circuit breaker)" value={s.auto_paused} tone={s.auto_paused ? "error" : "default"} />
        <MetricStat label="archived" value={s.archived} />
      </div>
      <div className="mt-4 grid gap-6 md:grid-cols-2">
        <Table head={["owner", "armed automations", "trigger kinds"]} rows={seg.depth.armed_per_owner.map((o) => [owner(o), o.armed, o.trigger_kinds.join(", ")])} />
        <Table head={["owner", "connected providers"]} rows={seg.depth.providers_per_owner.map((o) => [owner(o), o.providers.join(", ")])} />
      </div>
    </Card>
  );
}

export function Counters({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  return (
    <Card title="Counter-metrics (falsifiers)">
      <div className="grid gap-6 md:grid-cols-3">
        <div>
          <div className="mb-1 text-xs text-zinc-500">auto-paused automations</div>
          <Table head={["owner", "routine", "since"]} rows={seg.counters.auto_paused.map((r) => [owner(r), r.title, r.since ?? "—"])} />
        </div>
        <div>
          <div className="mb-1 text-xs text-zinc-500">archived within 7 days of creation</div>
          <Table head={["owner", "routine"]} rows={seg.counters.archived_within_7d.map((r) => [owner(r), r.title])} />
        </div>
        <div>
          <div className="mb-1 text-xs text-zinc-500">provision.gate.blocked, last 7d (CloudWatch, all owners)</div>
          <Table head={["code", "count"]} rows={data.gate_blocked_7d.map((g) => [g.code, g.n])} />
          <div className="mt-3 text-xs text-zinc-500">
            nightly CEO (paid owners): {seg.nightly_ceo.armed} armed · {seg.nightly_ceo.opted_out} opted out ·{" "}
            <span className={seg.nightly_ceo.never_armed ? "text-red-300" : ""}>{seg.nightly_ceo.never_armed} never armed</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

const FUNNEL_EVENTS = [
  "landing_viewed",
  "signup_started",
  "trial_started",
  "first_run_brief_submitted",
  "trial_first_agent_response",
  "chat_completed",
  "trial_converted",
  "subscription_activated",
  "automation_armed",
  "subscription_cancelled",
];

function funnelByWeek(data: GrowthData) {
  const funnel = new Map<string, Record<string, number>>();
  for (const r of data.posthog.funnel) funnel.set(r.week, { ...(funnel.get(r.week) ?? {}), [r.event]: r.owners });
  return funnel;
}

export function Acquisition({ seg, data }: { seg: GrowthSegment; data: GrowthData }) {
  const funnel = funnelByWeek(data);
  return (
    <Card title="Acquisition — client funnel per week (PostHog, unique persons, unsegmented) · signups per week (billing rows)">
      <div className="grid gap-6 md:grid-cols-[1fr_auto_auto]">
        <Table head={["week", ...FUNNEL_EVENTS]} rows={data.weeks.map((w) => [w, ...FUNNEL_EVENTS.map((e) => funnel.get(w)?.[e] ?? 0)])} />
        <Table head={["week", "signups"]} rows={seg.owners.signups_by_week.map((w) => [w.week, w.n])} />
        <div>
          <div className="mb-1 text-xs text-zinc-500">signup_started by first touch</div>
          <Table head={["source", "owners"]} rows={data.posthog.signup_sources.map((s) => [s.source, s.owners])} />
        </div>
      </div>
      <div className="mt-2 text-xs text-zinc-500">
        trial → first agent response is the SEO plan&apos;s old activation read; it is a trial-stage signal only — trials cannot arm automations.
        Churn/NRR are not built (no cancel reason is stored); activated vs cancelled per week is the stand-in.
      </div>
    </Card>
  );
}

export function PosthogCrosscheck({ data }: { data: GrowthData }) {
  const byWeek = new Map<string, Record<string, number>>();
  for (const r of data.posthog.runs) byWeek.set(r.week, { ...(byWeek.get(r.week) ?? {}), [r.kind]: r.n });
  return (
    <Card title="PostHog cross-check — agent_run_completed by initiation (attribution there is lossy; the DB above is the ledger)">
      <Table
        head={["week", "autonomous", "human", "unknown"]}
        rows={data.weeks.map((w) => [w, byWeek.get(w)?.autonomous ?? 0, byWeek.get(w)?.human ?? 0, byWeek.get(w)?.unknown ?? 0])}
      />
    </Card>
  );
}
```

- [ ] **Step 3: Page** (`growth/page.tsx`)

```tsx
import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getGrowth } from "../_lib/api";
import { Acquisition, Activation, Counters, Headline, NorthStar, Owners, PosthogCrosscheck, RetentionDepth } from "./sections";

export const dynamic = "force-dynamic";

export default async function AdminGrowthPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getGrowth(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Growth</h1>
        <p className="text-xs text-zinc-500">
          Metric set from docs/growth/2026-08-17-blitzscaling-strategy.md §3. External owners only; internal accounts below.
          {data ? ` Generated ${data.generated_at}, ${data.window_weeks}-week window.` : ""}
        </p>
      </div>

      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          message={
            !env
              ? "Growth read failed — nothing below is trustworthy."
              : `Partial read (${env.source_state}): ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ")}. Sections fed by a failed source render empty, not zero.`
          }
        />
      ) : null}

      {data ? (
        <>
          <Headline seg={data.segments.external} />
          <Owners seg={data.segments.external} />
          <Acquisition seg={data.segments.external} data={data} />
          <Activation seg={data.segments.external} />
          <NorthStar seg={data.segments.external} />
          <RetentionDepth seg={data.segments.external} />
          <Counters seg={data.segments.external} data={data} />
          <PosthogCrosscheck data={data} />
          <Card title={`Internal accounts (${data.internal_owner_ids.length}) — excluded from everything above`}>
            <div className="space-y-6">
              <Headline seg={data.segments.internal} />
              <Activation seg={data.segments.internal} />
              <NorthStar seg={data.segments.internal} />
            </div>
          </Card>
        </>
      ) : null}
    </div>
  );
}
```

Check `ErrorBanner`'s prop name in `components/admin/ErrorBanner.tsx` (it may be `message` or `children`) and match it.

- [ ] **Step 4: Nav link** — in `layout.tsx` after the Users `<Link>`:

```tsx
              <Link
                href="/admin/growth"
                className="rounded-md px-3 py-1.5 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
              >
                Growth
              </Link>
```

- [ ] **Step 5: Typecheck, lint, unit tests**

Run: `cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint && pnpm test`
Expected: clean (tsc typechecks tests too — see memory `feedback_frontend_ci_tsc_typechecks_tests`).

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/app/admin/_lib/api.ts apps/frontend/src/app/admin/growth apps/frontend/src/app/admin/layout.tsx
git commit -m "feat(admin): /admin/growth — fleet GTM metrics page"
```

---

### Task 8: Verify on dev, then PR

**Files:** none new. Docs: delete the two planning docs once merged (CLAUDE.md rule) — `docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md` and this file — in the PR or immediately after.

- [ ] **Step 1: Full backend + frontend suites**

Run: `cd apps/backend && uv run pytest tests/ -q` and `cd apps/frontend && pnpm test`
Expected: green. If anything unrelated fails, it is probably this change (dependent systems — CLAUDE.md).

- [ ] **Step 2: Open the PR** (never push to main; branch `feat/admin-growth-dashboard`)

```bash
git push -u origin HEAD
gh pr create --title "feat(admin): /admin/growth — fleet GTM metrics (decided §3 set)" --body "$(cat <<'EOF'
Fleet-wide GTM dashboard on the admin site, computed on read from DynamoDB + the Paperclip DB (Data API, SELECT-only) + PostHog HogQL (cross-check) + CloudWatch (gate-blocked codes). Internal owners segmented out of every headline.

Metric definitions: docs/growth/2026-08-17-blitzscaling-strategy.md §3. Research + source decisions: docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md §0.1.

Infra: one IAM statement (cloudwatch:GetMetricData) on the backend task role; deploy.yml regenerated.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Watch CI**

Run: `gh pr checks --watch` (or `gh run view <id> --repo Isol8AI/isol8 --json status,jobs` — never `run watch` in a loop, it burns API quota).

- [ ] **Step 4: After merge, verify on DEV with Claude-in-Chrome** (memory: E2E = Claude-in-Chrome on dev, never prod): sign in to `https://admin.dev.isol8.co/admin/growth` with a dev admin account, confirm the page renders, `source_state` is `ok` (all four sources), and the dev fleet's numbers match a hand query (`aws rds-data execute-statement … --sql "SELECT count(*) FROM routines WHERE status='active'"` against `isol8-dev-paperclip-db`). Prod is promoted separately by `gh workflow run deploy.yml --ref main` — dispatch only into an EMPTY queue (memory: a pending dispatch is silently cancelled by any merge).

- [ ] **Step 5: Delete the planning docs** in a follow-up commit once the page is verified on dev.

---

## Self-review

- **Spec coverage:** acquisition funnel + signup sources (Task 4 `HOGQL_FUNNEL`/`HOGQL_SIGNUP_SOURCES`, Task 7 `Acquisition`), activation gate incl. the Friday-first-run stage (Task 4 `activation`), North Star split by initiation + nightly exclusion (`north_star`, `SQL_RUNS_BY_WEEK`), retention layers 1–2 (`weekly_active_owners`, `automation_survival`), depth (`depth`), counter-metrics: auto-pause (`counters.auto_paused`), deleted<7d (`archived_within_7d`), `provision.gate.blocked` (Task 6), cancellation (PostHog `subscription_cancelled` in the funnel table — no reason exists to show), internal segmentation (`segments`), headline bar (`headline`), counts-not-percentages (only `autonomous_share` is a ratio, labelled). Revenue layer 3 (logo churn / NRR / seat expansion) is **not** built: no cancellation reason is stored and NRR needs Stripe invoice history; the funnel table's `subscription_activated` vs `subscription_cancelled` per week is the v1 stand-in. Task 7's PosthogCrosscheck card title must say so — use the title `"PostHog cross-check — client funnel + activated/cancelled per week (revenue layer: churn/NRR not built; no cancel reason is stored)"` — and the PR body repeats it.
- **Placeholders:** none — every step has code. The `ErrorBanner` prop and `AuthContext` ctor are named as "check and match", which is a read, not a TODO.
- **Type consistency:** `derive` keyword names = `_read_ddb`/`_read_aurora` dict keys = SQL aliases (`company_id, id, title, status, auto_paused, created_at, updated_at, has_enabled_trigger, trigger_kinds`; `company_id, week, kind, ok, total`; `company_id, first_ok`; `code, n`). `GrowthData` TS mirrors the dict, including `sources` added in `compute`. Route path `/ops/growth` matches `getGrowth`.
- **Ordering note:** Task 5's compute test patches Task 6's module; execute 5 then 6 before running the combined suite, one PR.
