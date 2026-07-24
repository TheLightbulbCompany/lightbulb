# Admin Provisioning Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators a "Provisioning" tab on `/admin/users/{id}` that surfaces the full onboarding/provisioning truth (readiness contract, containers-row error/lease fields, Paperclip company + funnel/bootstrap state, provision-gate code, SFN execution status) plus two safe levers: retry provision and reset bootstrap.

**Architecture:** One new read endpoint `GET /api/v1/admin/users/{user_id}/provisioning` composed in `admin_service` by REUSING the existing single-source composers — `readiness_state.compute_status` (INV-1: the sole legal composer), `provision_gate` (via a new metric-free probe), `paperclip_repo.get`, and `provision_dispatch.describe_dispatch_execution` (which was built as this integration point and has zero callers today). Two new write endpoints route through the existing provisioning doors (`dispatch_provision`, a new generation-conditioned `reset_bootstrap_for_retry` repo method). Frontend adds one tab + one server-component page + one client actions panel, following the existing Container-tab patterns exactly.

**Tech Stack:** FastAPI + DynamoDB repos (backend), Next.js App Router server components + server actions (frontend), pytest + vitest.

## Global Constraints

- All admin mutations: `Depends(require_platform_admin)` + `@idempotency()` + `@audit_admin_action("...")` (see `routers/admin.py:255-273` for the canonical block). Reads use `require_admin_read_access` so the `platform_ops` token can read them.
- Resolve `user_id → owner_id` via `resolve_admin_owner_id(user_id)` on every endpoint (org members must hit the org's rows).
- **Never return raw DDB rows on the admin wire.** The `containers` row carries `gateway_token`; the `paperclip-companies` row carries `paperclip_password_encrypted` + `service_token_encrypted`. Every payload is built from an explicit ALLOWLIST of fields (a deny-list rots).
- Diagnostic reads must not emit `provision.gate.blocked` — that metric stream feeds the W64 spike alarm.
- No behavior changes to owner-facing paths. This PR only adds admin surface + copy fixes.
- Match existing code style: repos are plain functions/methods over dict items; frontend server actions go through `_actions/_shared.adminPost`; pages fetch via `_lib/api.ts` helpers.
- Backend tests live in `apps/backend/tests/unit/...` mirroring source paths; run with `cd apps/backend && uv run pytest <file> -q`. Frontend tests are RAM-heavy — run only the touched test files until the final task.
- Commit after each task (conventional commits, no PR/bug refs in code comments).

---

### Task 1: Metric-free gate probe (`probe_provision_gate`)

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py` (add function after `evaluate_provision_gate`, ~line 360)
- Test: locate with `ls apps/backend/tests/unit/services/ | grep -i gate` — if `test_provision_gate.py` exists, append; otherwise `grep -rn "evaluate_provision_gate" apps/backend/tests/ -l` and append to the file that already tests the gate.

**Interfaces:**
- Consumes: `_evaluate_provision_gate_inner(owner_id=..., is_admin=...)` (existing, `provision_gate.py`)
- Produces: `async def probe_provision_gate(*, owner_id: str, is_admin: bool = True) -> Gate | None` — used by Task 3.

- [ ] **Step 1: Write the failing test** (append to the located gate test file, reusing its existing fixture/mock style for `_evaluate_provision_gate_inner` inputs):

```python
@pytest.mark.asyncio
async def test_probe_provision_gate_skips_metric_emission(monkeypatch):
    """probe_provision_gate returns the same Gate as evaluate_provision_gate
    but must NOT emit provision.gate.blocked (the W64 alarm stream)."""
    from core.services import provision_gate

    sentinel = provision_gate.Gate(
        code="subscription_required",
        title="t", message="m", action_label="a",
        action_href="/settings/billing", action_admin_only=False, owner_role="admin",
    )

    async def fake_inner(*, owner_id, is_admin):
        return sentinel

    emitted = []
    monkeypatch.setattr(provision_gate, "_evaluate_provision_gate_inner", fake_inner)
    # Patch the metric emitter the module uses. Find the exact name first:
    #   grep -n "provision.gate.blocked" apps/backend/core/services/provision_gate.py
    # and patch THAT callable (e.g. metrics.emit / put_metric) to append to `emitted`.

    gate = await provision_gate.probe_provision_gate(owner_id="user_x")
    assert gate is sentinel
    assert emitted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest <gate test file> -q -k probe_provision_gate`
Expected: FAIL with `AttributeError: ... has no attribute 'probe_provision_gate'`

- [ ] **Step 3: Implement**

```python
async def probe_provision_gate(*, owner_id: str, is_admin: bool = True) -> Gate | None:
    """Metric-free gate evaluation for diagnostic reads (the admin surface).

    ``evaluate_provision_gate``'s blocked-metric stream feeds the W64
    spike alarm; an operator inspecting an owner must not inflate it.
    Same decision logic, no emission.
    """
    return await _evaluate_provision_gate_inner(owner_id=owner_id, is_admin=is_admin)
```

- [ ] **Step 4: Run test to verify it passes** — same command, expected PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(admin): metric-free provision-gate probe for diagnostic reads"`

---

### Task 2: `PaperclipRepo.reset_bootstrap_for_retry`

**Files:**
- Modify: `apps/backend/core/repositories/paperclip_repo.py` (add method directly after `mark_bootstrap_failed`, ~line 1092)
- Test: `apps/backend/tests/unit/repositories/test_paperclip_repo.py` (locate: `ls apps/backend/tests/unit/repositories/ | grep -i paperclip`; append next to the existing `mark_bootstrap_failed` tests and reuse their table-mock fixture style)

**Interfaces:**
- Consumes: `self._table()`, `run_in_thread` (both already used by `mark_bootstrap_failed`, `paperclip_repo.py:1057-1090`)
- Produces: `async def reset_bootstrap_for_retry(self, user_id: str, *, onboarding_generation: str) -> bool` — True if the conditional write landed. Used by Task 4.

- [ ] **Step 1: Write the failing tests** (mirror the existing `mark_bootstrap_failed` test's mock-table pattern):

```python
@pytest.mark.asyncio
async def test_reset_bootstrap_for_retry_lands_on_failed_row(repo_with_mock_table):
    repo, table = repo_with_mock_table
    ok = await repo.reset_bootstrap_for_retry("user_1", onboarding_generation="GEN1")
    assert ok is True
    kwargs = table.update_item.call_args.kwargs
    assert "bootstrap_state = :ns" in kwargs["UpdateExpression"]
    assert "REMOVE bootstrap_dispatch_id" in kwargs["UpdateExpression"]
    cond = kwargs["ConditionExpression"]
    assert "onboarding_generation = :gen" in cond
    assert "funnel_state = :submitted" in cond
    assert "bootstrap_state = :failed" in cond
    assert kwargs["ExpressionAttributeValues"][":zero"] == 0


@pytest.mark.asyncio
async def test_reset_bootstrap_for_retry_returns_false_on_condition_fail(repo_with_mock_table):
    repo, table = repo_with_mock_table
    table.update_item.side_effect = table.meta.client.exceptions.ConditionalCheckFailedException(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    ok = await repo.reset_bootstrap_for_retry("user_1", onboarding_generation="GEN1")
    assert ok is False
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/unit/repositories/test_paperclip_repo.py -q -k reset_bootstrap` → `AttributeError`.

- [ ] **Step 3: Implement**

```python
async def reset_bootstrap_for_retry(self, user_id: str, *, onboarding_generation: str) -> bool:
    """Admin lever: conditional ``bootstrap_state: failed -> not_started``.

    ``mark_bootstrap_failed`` deliberately leaves the funnel ``submitted``
    so a later reset can re-arm the kickoff (its own docstring names the
    not-yet-built reconciliation_worker as that "later"). This is the
    manual stand-in: fenced on the SAME onboarding_generation the operator
    read, and only from a genuinely failed bootstrap on a still-submitted
    funnel — a superseded or already-recovered row is a no-op (False).
    ``bootstrap_last_error`` is kept as history; ``bootstrap_attempts``
    resets so MAX_BOOTSTRAP_ATTEMPTS re-arms. The next owner
    ``/container/status`` poll re-dispatches.

    Returns True if the write landed.
    """
    table = self._table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET bootstrap_state = :ns, bootstrap_attempts = :zero, updated_at = :u "
                "REMOVE bootstrap_dispatch_id"
            ),
            ConditionExpression=(
                "onboarding_generation = :gen AND funnel_state = :submitted AND bootstrap_state = :failed"
            ),
            ExpressionAttributeValues={
                ":ns": "not_started",
                ":zero": 0,
                ":u": datetime.now(timezone.utc).isoformat(),
                ":gen": onboarding_generation,
                ":submitted": "submitted",
                ":failed": "failed",
            },
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
```

- [ ] **Step 4: Run to verify PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(paperclip): generation-fenced bootstrap reset for admin retry"`

---

### Task 3: `admin_service.get_provisioning` composition

**Files:**
- Modify: `apps/backend/core/services/admin_service.py` (add `get_provisioning` + three `_narrow_*` helpers after `get_overview`, ~line 256)
- Test: `apps/backend/tests/unit/services/test_admin_service.py` (append; reuse its existing async-mock style for repo/service patches)

**Interfaces:**
- Consumes:
  - `resolve_admin_owner_id(user_id) -> (owner_id, org_context)` (`admin_service.py:41`)
  - `container_repo.get_by_owner_id(owner_id) -> dict | None`, `container_repo.is_provision_claim_stale(row) -> bool`
  - `compute_status(owner_id, *, container=..., onboarding=_NOT_GIVEN) -> dict` from `core.services.readiness_state` (pass the already-read row — including an explicit `None` — to avoid a second DDB read; this is the documented calling convention, `readiness_state.py:169-176`)
  - `probe_provision_gate(owner_id=..., is_admin=True)` (Task 1)
  - `PaperclipRepo(table_name="paperclip-companies").get(owner_id)` — instantiate via a module-local `_paperclip_repo()` accessor copied from `readiness_state.py:102-110` (local import keeps the existing `patch("core.repositories.paperclip_repo.PaperclipRepo", ...)` test idiom working)
  - `describe_dispatch_execution(owner_id, claim_id) -> dict | None` from `core.services.provision_dispatch`
  - The existing `_with_timeout` helper in `admin_service.py` (same call shape as `get_overview`, `admin_service.py:243-249`)
- Produces: `async def get_provisioning(user_id: str) -> dict` with keys `owner_id, org, status_contract, container, paperclip, gate, sfn_execution`. Used by Task 4.

- [ ] **Step 1: Write the failing tests** (key behaviors — secrets never leak, degraded reads never 500):

```python
@pytest.mark.asyncio
async def test_get_provisioning_narrows_and_excludes_secrets(...):
    # Arrange mocks: containers row includes gateway_token="SECRET-GW";
    # paperclip row (a PaperclipCompany dataclass instance) includes
    # paperclip_password_encrypted="SECRET-PW", service_token_encrypted="SECRET-ST".
    result = await admin_service.get_provisioning("user_1")
    flat = json.dumps(result)
    assert "SECRET-GW" not in flat
    assert "SECRET-PW" not in flat
    assert "SECRET-ST" not in flat
    assert result["container"]["status"] == "error"
    assert result["container"]["last_error"] == "boom"
    assert result["container"]["claim_stale"] in (True, False)
    assert result["paperclip"]["funnel_state"] == "submitted"
    assert result["paperclip"]["bootstrap_state"] == "failed"

@pytest.mark.asyncio
async def test_get_provisioning_no_rows_degrades_to_nones(...):
    # containers read -> None, paperclip read -> None, gate -> None, sfn -> None
    result = await admin_service.get_provisioning("user_1")
    assert result["container"] is None
    assert result["paperclip"] is None
    assert result["gate"] is None
    assert result["sfn_execution"] is None
    assert result["status_contract"] is not None  # composer answers even with no row

@pytest.mark.asyncio
async def test_get_provisioning_narrows_sfn_response(...):
    # describe_dispatch_execution returns a boto3-shaped dict with datetimes
    # + ResponseMetadata; assert only name/status/start_date/stop_date survive,
    # dates ISO-serialized, ResponseMetadata dropped.
    ...
    assert set(result["sfn_execution"]) == {"name", "status", "start_date", "stop_date"}

@pytest.mark.asyncio
async def test_get_provisioning_gate_payload_shape(...):
    # probe returns a Gate -> result["gate"] == gate.to_payload()["blocked"]
    assert result["gate"]["code"] == "subscription_required"
```

(Write these fully — pull the mock-wiring idiom from the existing `get_overview` tests in the same file; each test stubs `resolve_admin_owner_id`, `container_repo.get_by_owner_id`, `compute_status`, `_paperclip_repo`, `probe_provision_gate`, `describe_dispatch_execution`.)

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/unit/services/test_admin_service.py -q -k provisioning`.

- [ ] **Step 3: Implement**

```python
_CONTAINER_PROVISION_FIELDS = (
    "status", "substatus", "last_error", "last_error_at", "retryable",
    "provision_generation", "provision_claim_id", "provision_claimed_at",
    "provision_attempt_token", "provision_execution_name",
    "onboarded_at", "bedrock_revoked", "stop_reason",
    "service_name", "created_at", "updated_at",
)

_PAPERCLIP_PROVISION_FIELDS = (
    # ALLOWLIST — paperclip_password_encrypted / service_token_encrypted
    # must never cross the admin wire, even Fernet-encrypted.
    "status", "company_id", "org_id", "paperclip_user_id",
    "last_error", "seeded_agent_ids", "scheduled_purge_at",
    "onboarding_generation", "funnel_state", "funnel_submitted_at",
    "funnel_completed_at", "onboarding_goal_id", "onboarding_project_id",
    "onboarding_submitted_by", "bootstrap_state", "bootstrap_attempts",
    "bootstrap_dispatch_id", "bootstrap_dispatched_at",
    "bootstrap_delivered_at", "bootstrap_last_error",
)
# Before finalizing, verify every name against the dataclass at
# paperclip_repo.py:157-256 (e.g. confirm the exact company/org id field
# names) — drop any that don't exist, add created_at/updated_at if present.


def _narrow_container(row: dict | None) -> dict | None:
    if not row:
        return None
    out = {k: row.get(k) for k in _CONTAINER_PROVISION_FIELDS}
    out["claim_stale"] = container_repo.is_provision_claim_stale(row)
    return out


def _narrow_paperclip(company) -> dict | None:
    if company is None:
        return None
    return {k: getattr(company, k, None) for k in _PAPERCLIP_PROVISION_FIELDS}


def _narrow_execution(resp: dict | None) -> dict | None:
    if not resp:
        return None
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "name": resp.get("name"),
        "status": resp.get("status"),
        "start_date": _iso(resp.get("startDate")),
        "stop_date": _iso(resp.get("stopDate")),
    }


async def get_provisioning(user_id: str) -> dict:
    """Provisioning truth for one owner: the readiness contract, the raw
    containers-row provisioning fields, the Paperclip company + onboarding
    state machines, the provision-gate decision, and the SFN execution —
    each read best-effort so a degraded store can't blank the page."""
    owner_id, org_context = await resolve_admin_owner_id(user_id)

    container = await _with_timeout(container_repo.get_by_owner_id(owner_id), None)
    status_contract, paperclip_row, gate = await asyncio.gather(
        _with_timeout(compute_status(owner_id, container=container), None),
        _with_timeout(_paperclip_repo().get(owner_id), None),
        _with_timeout(probe_provision_gate(owner_id=owner_id), None),
    )

    sfn = None
    claim_id = (container or {}).get("provision_claim_id")
    if claim_id and (container or {}).get("provision_execution_name"):
        sfn = await _with_timeout(describe_dispatch_execution(owner_id, claim_id), None)

    return {
        "owner_id": owner_id,
        "org": org_context,
        "status_contract": status_contract,
        "container": _narrow_container(container),
        "paperclip": _narrow_paperclip(paperclip_row),
        "gate": gate.to_payload()["blocked"] if gate else None,
        "sfn_execution": _narrow_execution(sfn),
    }
```

(Adjust `_with_timeout`'s exact call shape / gather idiom to match the existing `get_overview` implementation in the same file — reuse, don't reinvent.)

- [ ] **Step 4: Run to verify PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(admin): provisioning-truth composition (readiness + paperclip + gate + SFN)"`

---

### Task 4: Router endpoints (1 read + 2 levers)

**Files:**
- Modify: `apps/backend/routers/admin.py` (insert after the container-actions block, ~line 470)
- Test: `apps/backend/tests/unit/routers/test_admin_users_reads.py` (GET) and `apps/backend/tests/unit/routers/test_admin_actions_writes.py` (POSTs) — reuse the `admin_env` / `non_admin_env` fixtures (`test_admin_actions_writes.py:55-80`)

**Interfaces:**
- Consumes: `admin_service.get_provisioning` (Task 3), `dispatch_provision(owner_id)` from `core.services.provision_dispatch` (never raises), `container_repo.get_by_owner_id` / `is_provision_claim_stale`, `_paperclip_repo().get` + `.reset_bootstrap_for_retry` (Task 2), existing `require_platform_admin`, `require_admin_read_access`, `resolve_admin_owner_id`, `@idempotency()`, `@audit_admin_action`.
- Produces routes:
  - `GET  /api/v1/admin/users/{user_id}/provisioning`
  - `POST /api/v1/admin/users/{user_id}/provisioning/retry`
  - `POST /api/v1/admin/users/{user_id}/provisioning/reset-bootstrap`

- [ ] **Step 1: Write the failing tests.** GET: 200 with composed payload for admin; 403 for `non_admin_env`. Retry: 403 non-admin; 404 no row; 409 when row status is `running`; 409 when `provisioning` with a FRESH claim; 200 + `dispatch_provision` awaited exactly once when status is `error` (and when `provisioning`+stale). Reset-bootstrap: 403 non-admin; 404 no paperclip row; 409 when `bootstrap_state != "failed"`; 409 when repo returns False (lost race); 200 when True. Both POSTs: assert an `admin-actions` audit row lands (copy the existing audit assertion from a neighboring write test).

- [ ] **Step 2: Run to verify FAIL** (404s — routes don't exist).

- [ ] **Step 3: Implement**

```python
@router.get(
    "/users/{user_id}/provisioning",
    description=(
        "Provisioning truth for one owner: readiness contract "
        "(compute_status), containers-row error/lease fields, Paperclip "
        "company + funnel/bootstrap state, provision-gate decision "
        "(metric-free probe), and the SFN execution status. Read-only."
    ),
)
async def admin_user_provisioning(
    user_id: str,
    auth: AuthContext = Depends(require_admin_read_access),
):
    return await admin_service.get_provisioning(user_id)


@router.post(
    "/users/{user_id}/provisioning/retry",
    description=(
        "Re-dispatch provisioning through the PROVISION executor. Covers the "
        "error-row retry AND the stale-claim wedge rescue (a 'provisioning' "
        "row whose SFN execution closed without a terminal write) — "
        "dispatch_provision's Branch A/B logic decides which. Unlike the "
        "owner /container/retry, no billing gate: this is an operator "
        "break-glass, audited. Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("provision.retry")
async def admin_provision_retry(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        raise HTTPException(status_code=404, detail="No container row for this owner")
    status = row.get("status")
    if status == "running":
        raise HTTPException(status_code=409, detail="Container is running — nothing to retry")
    if status == "provisioning" and not container_repo.is_provision_claim_stale(row):
        raise HTTPException(
            status_code=409,
            detail="A provision attempt is in flight (fresh claim); refusing a concurrent dispatch",
        )
    await dispatch_provision(owner_id)
    fresh = await container_repo.get_by_owner_id(owner_id)
    return {
        "ok": True,
        "status": (fresh or {}).get("status"),
        "substatus": (fresh or {}).get("substatus"),
        "provision_claim_id": (fresh or {}).get("provision_claim_id"),
    }


@router.post(
    "/users/{user_id}/provisioning/reset-bootstrap",
    description=(
        "Re-arm a permanently-failed onboarding kickoff: conditional "
        "bootstrap_state failed->not_started fenced on the row's current "
        "onboarding_generation, resetting the attempt counter. The next "
        "owner /container/status poll re-dispatches the kickoff (delivery "
        "needs the owner's chat surface online). Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("provision.reset_bootstrap")
async def admin_reset_bootstrap(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    repo = _paperclip_repo()
    company = await repo.get(owner_id)
    if company is None:
        raise HTTPException(status_code=404, detail="No Paperclip company row for this owner")
    if company.bootstrap_state != "failed" or not company.onboarding_generation:
        raise HTTPException(
            status_code=409,
            detail=f"Bootstrap is '{company.bootstrap_state}', not 'failed' — nothing to reset",
        )
    landed = await repo.reset_bootstrap_for_retry(
        owner_id, onboarding_generation=company.onboarding_generation
    )
    if not landed:
        raise HTTPException(
            status_code=409,
            detail="Reset did not land — row changed under us (new generation or state); re-read and retry",
        )
    return {"ok": True}
```

Imports to add at the top of `admin.py`: `dispatch_provision` from `core.services.provision_dispatch`, `container_repo`, and a module-local `_paperclip_repo()` accessor (same 6-line pattern as `readiness_state.py:102-110`). `get_provisioning` comes via the existing `admin_service` import.

- [ ] **Step 4: Run to verify PASS** — both router test files.
- [ ] **Step 5: Commit** — `git commit -m "feat(admin): provisioning read endpoint + retry/reset-bootstrap levers"`

---

### Task 5: Frontend — API helper + Provisioning page (read side)

**Files:**
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` (add types + `getProvisioning` next to `getOverview`, ~line 357)
- Modify: `apps/frontend/src/app/admin/users/[id]/layout.tsx:5-13` (add tab)
- Create: `apps/frontend/src/app/admin/users/[id]/provisioning/page.tsx`

**Interfaces:**
- Consumes: `GET /admin/users/{id}/provisioning` (Task 4), existing `adminHttp` (`_lib/api.ts:242`), existing card components `Card`, `DefRow`, `StatusChip`, `EmptyState`, `ErrorBanner` from `@/components/admin`.
- Produces: `getProvisioning(token: string, userId: string): Promise<AdminProvisioning | null>`; `AdminProvisioning` type; the `/admin/users/{id}/provisioning` route. Task 6 renders its actions panel inside this page.

- [ ] **Step 1: Add the type + fetcher** (shape mirrors Task 3's payload; follow `getOverview`'s implementation style exactly):

```ts
export interface AdminProvisioning {
  owner_id: string;
  org: unknown;
  status_contract: {
    runtime_phase?: string;
    lifecycle?: string;
    chat_available?: boolean;
    teams_state?: string | null;
  } & Record<string, unknown> | null;
  container: {
    status?: string; substatus?: string | null;
    last_error?: string | null; last_error_at?: string | null;
    retryable?: boolean | null; claim_stale?: boolean;
    provision_generation?: string | null; provision_claim_id?: string | null;
    provision_claimed_at?: number | null; provision_attempt_token?: string | null;
    provision_execution_name?: string | null;
    onboarded_at?: string | null; bedrock_revoked?: boolean | null;
    stop_reason?: string | null; service_name?: string | null;
    created_at?: string | null; updated_at?: string | null;
  } | null;
  paperclip: {
    status?: string | null; company_id?: string | null; org_id?: string | null;
    paperclip_user_id?: string | null; last_error?: string | null;
    seeded_agent_ids?: string[] | null; scheduled_purge_at?: string | null;
    onboarding_generation?: string | null;
    funnel_state?: string | null; funnel_submitted_at?: string | null;
    funnel_completed_at?: string | null;
    bootstrap_state?: string | null; bootstrap_attempts?: number | null;
    bootstrap_last_error?: string | null;
    bootstrap_dispatched_at?: string | null; bootstrap_delivered_at?: string | null;
  } | null;
  gate: { code?: string; title?: string; message?: string } | null;
  sfn_execution: { name?: string; status?: string; start_date?: string; stop_date?: string } | null;
}

export async function getProvisioning(
  token: string,
  userId: string,
): Promise<AdminProvisioning | null> {
  const { ok, data } = await adminHttp({
    method: "GET",
    path: `/admin/users/${encodeURIComponent(userId)}/provisioning`,
    token,
  });
  return ok ? (data as AdminProvisioning) : null;
}
```

(Verify `adminHttp`'s exact option names against `_lib/api.ts:242` before writing — copy `getOverview`'s call shape.)

- [ ] **Step 2: Register the tab** in `layout.tsx` `TABS`, between Container and Activity:

```ts
  { label: "Provisioning", segment: "provisioning" },
```

- [ ] **Step 3: Build the page** — server component modeled on `users/[id]/container/page.tsx` (same token fetch + not-found handling). Six cards, all read-only in this task:
  1. **Readiness** — `runtime_phase`, `lifecycle`, `chat_available`, `teams_state` (StatusChip where sensible).
  2. **Container row** — status/substatus, `last_error` (+`last_error_at`), `retryable`, and a "STALE CLAIM" warning chip when `claim_stale`.
  3. **Lease / generations** — `provision_generation`, `provision_claim_id`, `provision_claimed_at`, `provision_attempt_token`, `provision_execution_name` (mono, copyable text).
  4. **Paperclip company** — `status`, `company_id`, `paperclip_user_id`, `seeded_agent_ids` (count + ids; label element 0 "Friday (CEO)" per seed-order contract), `last_error`, `scheduled_purge_at`; EmptyState "No Paperclip company row" when null.
  5. **Onboarding** — `funnel_state`, `bootstrap_state`, `bootstrap_attempts`, `bootstrap_last_error`, dispatch/delivery timestamps, `onboarding_generation`.
  6. **Gate + SFN** — gate code/title/message or "Not blocked"; SFN name/status/start/stop or "No execution stamped".
- [ ] **Step 4: Verify** — `cd apps/frontend && pnpm exec tsc --noEmit` passes; `pnpm run lint` clean on the new files.
- [ ] **Step 5: Commit** — `git commit -m "feat(admin): provisioning tab — readiness, paperclip, onboarding, gate, SFN cards"`

---

### Task 6: Frontend — server actions + levers panel

**Files:**
- Create: `apps/frontend/src/app/admin/_actions/provisioning.ts`
- Create: `apps/frontend/src/app/admin/users/[id]/provisioning/ProvisioningActionsPanel.tsx`
- Modify: `apps/frontend/src/app/admin/users/[id]/provisioning/page.tsx` (render panel below the cards)
- Test: `apps/frontend/src/app/admin/users/[id]/provisioning/__tests__/ProvisioningActionsPanel.test.tsx` (model on the existing `WipeAccountDialog` test's render/interaction style)

**Interfaces:**
- Consumes: `adminPost` from `_actions/_shared` (`ActionResult` envelope), `ConfirmActionDialog` from `@/components/admin`, POST routes from Task 4.
- Produces: `retryProvision(userId: string): Promise<ActionResult>`, `resetBootstrap(userId: string): Promise<ActionResult>`.

- [ ] **Step 1: Server actions** (exact `_actions/container.ts` idiom):

```ts
"use server";

import { adminPost, type ActionResult } from "./_shared";

export async function retryProvision(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/provisioning/retry`);
}

export async function resetBootstrap(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/provisioning/reset-bootstrap`);
}
```

- [ ] **Step 2: Write the failing panel test** — renders both action blocks; "Retry provision" opens `ConfirmActionDialog` (typed confirm = `userId`, matching ContainerActionsPanel's convention) and calls `retryProvision` on confirm (mock the action module); "Reset bootstrap" button is DISABLED unless `bootstrapState === "failed"`; result/error banner renders from the `ActionResult`.
- [ ] **Step 3: Run test to verify FAIL** — `pnpm vitest run src/app/admin/users/\[id\]/provisioning` (targeted file only — frontend vitest is RAM-heavy).
- [ ] **Step 4: Implement the panel** — client component styled/structured exactly like `ContainerActionsPanel.tsx` (section blocks with title, explanatory copy, button, inline result). Props: `{ userId: string; bootstrapState: string | null; containerStatus: string | null }`. Copy for each block:
  - Retry: "Re-dispatches provisioning through the Step Functions executor. Safe for error rows and for 'provisioning' rows with a stale claim (wedge rescue); refuses (409) while a fresh attempt is in flight."
  - Reset bootstrap: "Re-arms a failed onboarding kickoff (attempts exhausted or chat.error). The kickoff re-dispatches on the user's next /container/status poll — delivery needs their chat surface online."
- [ ] **Step 5: Run test to verify PASS**, then `pnpm exec tsc --noEmit`.
- [ ] **Step 6: Commit** — `git commit -m "feat(admin): retry-provision + reset-bootstrap levers on provisioning tab"`

---

### Task 7: Stale sizing copy fixes (verified against `ecs_manager` constants)

Ground truth (`ecs_manager.py:53-54,72-73`): standard = **1 vCPU / 4 GB** (`1024`/`4096`), Ultra Pro = **2 vCPU / 8 GB**.

**Files:**
- Modify: `apps/backend/routers/admin.py:316` — description currently says "standard 0.5 vCPU / 2 GB"; change to "standard 1 vCPU / 4 GB, Ultra Pro 2 vCPU / 8 GB".
- Modify: `apps/frontend/src/app/admin/users/[id]/container/ContainerActionsPanel.tsx:52` and `:249` — both say "0.5 vCPU / 1 GB"; change the UI copy to "Forces the ECS service to roll its task definition back to the owner's plan-sized profile (standard 1 vCPU / 4 GB; Ultra Pro 2 vCPU / 8 GB)." and fix the header comment to match.

- [ ] **Step 1: Make the three edits.**
- [ ] **Step 2: Verify no other stale copies** — `grep -rn "0.5 vCPU" apps/` must return nothing.
- [ ] **Step 3: Commit** — `git commit -m "fix(admin): correct stale container sizing copy to plan-sized 1vCPU/4GB profile"`

---

### Task 8: Full verification

- [ ] **Step 1: Backend full suite** — `cd apps/backend && uv run pytest tests/ -q` (CI scope is `tests/`, NOT `tests/unit`). All pass.
- [ ] **Step 2: Frontend** — `cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint && pnpm test` (typecheck covers test files too — CI runs tsc over them).
- [ ] **Step 3: Fix anything red** (if a failure looks unrelated, assume it's ours first — dependent systems).
- [ ] **Step 4: Commit any linter auto-edits** before finishing (linters may rewrite files on commit hooks — re-add and re-commit).

---

## Out of scope (deliberate, documented follow-ups)

- **Repair-Paperclip lever (orphan-422 Aurora delete):** needs the `recover_orphan_paperclip_member.py` safety logic (email-matched single-orphan refusal rules) lifted in-process + Clerk email resolution. The IAM plumbing exists (account-wipe added `rds-data:ExecuteStatement`), but the decision logic deserves its own reviewed PR.
- **Raw "release lease" lever:** intentionally NOT built. `dispatch_provision` → `try_claim_provision` already claims over a stale lease; the retry lever covers the wedge rescue. A raw lease-delete would just be a footgun (don't over-fix).
- Trial-cap/$3 spend visibility, `provider_choice` display + set/clear, fleet-level provisioning dashboard, users-list filters, Overview alarm-noise cleanup — separate PRs from the audit backlog.
