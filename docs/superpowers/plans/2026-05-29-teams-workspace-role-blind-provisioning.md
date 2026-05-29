# Teams Workspace Role-Blind Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every member of a Clerk org get a Paperclip "team workspace" (a `paperclip-companies` row in the org's single shared company), regardless of their Clerk role, so `/teams` stops 409-ing for co-admins who joined an already-provisioned org.

**Architecture:** Two independent concerns were tangled together via Clerk role. (1) **Paperclip workspace** — the bug was deciding *create-vs-join* by role. The fix: **JOIN is role-blind** (any org member, admin or not, joins the org's one shared company via `find_org_owner(org_id)`); **CREATE is admin-gated** (only an admin bootstraps a fresh org's company — a non-admin member waits for one, mirroring who bootstraps the shared container). An in-process per-org lock serializes concurrent first-pollers because Paperclip ignores `Idempotency-Key`. (2) **Container (ECS) provisioning** — stays gated to container-owners (personal user or org *admin*); members ride the shared box. CREATE-company and CREATE-container are thus symmetric (both admin/personal), so a member never creates a company whose seeded Friday/Atlas point at a container no member can provision.

**Tech Stack:** Python 3.12, FastAPI, pytest/pytest-asyncio, DynamoDB (boto3), `unittest.mock`.

**Decision (confirmed in conversation):** Uniform privileges, role-blind. Admins and members get the same Teams access (mirrors how chat already ignores role). Differentiated Paperclip privileges are explicitly **out of scope**.

**Key safety constraint (verified):** `ecs_manager.provision_user_container` (which `background_provision` calls) is NOT a no-op on a running container — scenario 3 is "Service exists and running → force new deployment (RESTART)" (`ecs_manager.py:1202-1228`). Therefore `background_provision` MUST NOT be dispatched on a running-container poll, or the org container would restart every ~3s. This is why container dispatch is decoupled from the (now role-blind) Paperclip path via a `provision_container` flag.

**Self-healing:** Once deployed, currently-stuck users (e.g. the co-admin who reported the bug) are fixed automatically on their next `/container/status` poll — the running-container branch dispatches `ensure_user_ready(provision_container=False)`, which finds the existing company and runs `provision_member`. No manual backfill, and no container restart.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `apps/backend/core/services/user_provision.py` | Readiness entry point; create-vs-join selector + container dispatch | **Modify**: role-blind paperclip selector; per-org create lock; `provision_container` flag gating the (owner-only) container dispatch |
| `apps/backend/routers/container.py` | `/container/status` dispatch | **Modify**: running-container branch dispatches `ensure_user_ready(provision_container=False)` for ALL org users (was: members only, default flag) |
| `apps/backend/tests/unit/services/test_user_provision.py` | Unit tests for `ensure_user_ready` | **Modify**: company-existence expectations; admin-joins-existing-company regression; `provision_container=False` skips bg dispatch |
| `apps/backend/tests/unit/routers/test_container_status_role_blind_dispatch.py` | Unit test for the dispatch guard | **Create** |

---

## Task 1: (Already verified by controller) Container-provision restart hazard

No code. Recorded for the implementer's context:

`background_provision(owner_id, clerk_user_id)` (`core/services/container_provision.py:32`) calls `ecs_manager.provision_user_container(owner_id, ...)`. That method's docstring (`core/containers/ecs_manager.py:1202-1228`) enumerates: *"3. Service exists and running -> force new deployment (restart)."* So it is **not** safe to call on every poll of a running container.

Consequence baked into Tasks 2 & 3: container dispatch is gated by BOTH (a) caller is a container-owner (personal OR `is_org_admin`) and (b) a new `provision_container` flag that the `/container/status` running-container branch sets to `False`. Paperclip provisioning is role-blind and always runs.

---

## Task 2: Role-blind paperclip selector + per-org lock + container-dispatch flag in `user_provision.py`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py` (`ensure_user_ready` ~lines 38-90; add helper + lock)
- Test: `apps/backend/tests/unit/services/test_user_provision.py`

- [ ] **Step 2.1: Update the unit tests (write the new expectations first)**

Open `apps/backend/tests/unit/services/test_user_provision.py`. Keep the `_personal_auth` / `_org_admin_auth` / `_org_member_auth` helpers (lines 11-46).

**(a)** Replace `test_ensure_paperclip_for_owner_org_admin_passes_org_id` (the test asserting an org admin always calls `provision_org`) with these two tests:

```python
@pytest.mark.asyncio
async def test_org_admin_with_no_company_creates_it():
    """Org admin + org has NO active Paperclip company yet → provision_org
    (create), and container provisioning is dispatched (admin owns the box).
    Decided by company existence, not role."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_xyz",
        owner_user_id="user_admin",
        owner_email="a@example.com",
    )
    fake_provisioning.provision_member.assert_not_awaited()
    assert captured == ["background_provision"], f"admin-create should dispatch container, got {captured}"


@pytest.mark.asyncio
async def test_org_admin_with_existing_company_joins_it():
    """REGRESSION: org admin + org ALREADY has an active company →
    provision_member (join), NOT provision_org. The multi-admin case that
    previously 409'd on /teams."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="owner@example.com")),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_second_admin", "org_xyz", "b@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_xyz",
        user_id="user_second_admin",
        email="b@example.com",
        owner_email="owner@example.com",
    )
    fake_provisioning.provision_org.assert_not_awaited()
```

**(b)** Update `test_ensure_user_ready_dispatches_container_for_org_admin` (currently patches `_ensure_paperclip_for_owner`). The org path no longer calls `_ensure_paperclip_for_owner`; patch `_ensure_paperclip_for_org` instead. Replace that whole test with:

```python
@pytest.mark.asyncio
async def test_ensure_user_ready_dispatches_container_for_org_admin():
    """Org-admin (no company yet) → background_provision dispatched (admin owns the box)."""
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    assert captured == ["background_provision"], (
        f"expected one create_task call dispatching background_provision, got {captured}"
    )
```

**(c)** Replace `test_ensure_user_ready_does_not_dispatch_container_for_member` with the corrected member contract (member still does NOT dispatch container — members ride the box — but DOES get paperclip provisioned via the join path):

```python
@pytest.mark.asyncio
async def test_member_joins_company_but_does_not_dispatch_container():
    """Org member → paperclip join runs, but NO container dispatch (members
    ride the org admin's shared container; they never spin one up)."""
    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch("core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_member.assert_awaited_once()
    assert captured == [], f"members must not dispatch container, got {captured}"
```

**(d)** Add the new flag test:

```python
@pytest.mark.asyncio
async def test_provision_container_false_skips_container_dispatch_for_admin():
    """RUNNING-container poll path: ensure_user_ready(provision_container=False)
    must run paperclip provisioning but NOT dispatch background_provision —
    otherwise provision_user_container would force-restart the running
    container on every ~3s poll."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch("core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"), provision_container=False)
    fake_provisioning.provision_member.assert_awaited_once()
    assert captured == [], f"provision_container=False must skip container dispatch, got {captured}"
```

The remaining member tests (`..._returns_when_owner_company_missing`, `..._threads_owner_user_id_to_lookup_owner_email`, `..._idempotent_on_duplicate_409`, `..._reraises_non_409_paperclip_error`) and the personal/owner tests stay valid. Leave them.

- [ ] **Step 2.2: Run the tests to verify they FAIL**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_user_provision.py -v`
Expected: FAIL — the new tests reference `_ensure_paperclip_for_org`, `find_org_owner`-based routing, and the `provision_container` kwarg, none of which exist yet.

- [ ] **Step 2.3: Implement the changes in `user_provision.py`**

Add to the imports near the top (after `import asyncio`):

```python
from collections import defaultdict
```

Add the per-org lock registry just below `logger = logging.getLogger(__name__)`:

```python
# Per-org in-process lock serializing the create-vs-join decision so two
# concurrent first-pollers in an all-admin org don't each create a separate
# Paperclip company. Paperclip ignores the Idempotency-Key header
# (see paperclip_admin_client.py module docstring), so dedup must happen
# here. Single-backend-instance scope: if the service scales to N tasks the
# window reopens across tasks; acceptable for current fleet size — the loser
# of a cross-instance race resolves to the join path on its next poll.
_org_provision_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
```

Change the `ensure_user_ready` signature to add the flag:

```python
async def ensure_user_ready(auth: AuthContext, *, provision_container: bool = True) -> None:
```

Update its docstring's flow section to note: the org path is role-blind for paperclip; container dispatch is owner-gated (personal/admin) AND skipped when `provision_container=False` (the running-container poll path). Then replace the selector + dispatch block (currently the `if auth.is_org_context and not auth.is_org_admin:` member branch through the trailing `asyncio.create_task(background_provision(...))`) with:

```python
    if auth.is_org_context:
        # JOIN is role-blind — a co-admin (or member) of an
        # already-provisioned org joins the org's ONE shared company, never
        # creates a second one. CREATE is admin-gated: bootstrapping the
        # org's shared company mirrors who bootstraps the shared container
        # (admins; members ride). A member polling a not-yet-bootstrapped
        # org waits for an admin.
        await _ensure_paperclip_for_org(
            org_id=auth.org_id,
            user_id=auth.user_id,
            email=auth.email or "",
            is_admin=auth.is_org_admin,
        )
    else:
        # Personal owner: always the sole owner of their own company.
        await _ensure_paperclip_for_owner(
            owner_id=owner_id,
            owner_user_id=auth.user_id,
            owner_email=auth.email or "",
            org_id=None,
        )

    # Container (ECS) provisioning is a SEPARATE concern from the Paperclip
    # workspace above. Dispatch it only when:
    #   (a) the caller is a container-owner — personal user or org ADMIN.
    #       Org members ride the shared org container; they never spin one
    #       up. (This role check is about WHO SPINS UP THE BOX, which is
    #       unrelated to Teams/Paperclip access — that is role-blind above.)
    #   (b) provision_container is True. The /container/status running-
    #       container branch passes False, because provision_user_container
    #       force-restarts an already-running service (ecs_manager docstring
    #       scenario 3) — calling it every ~3s poll would restart the box.
    # background_provision is idempotent on the absent/0-desired cases, so
    # repeated owner polls on a not-yet-running container are safe.
    if provision_container and (not auth.is_org_context or auth.is_org_admin):
        asyncio.create_task(background_provision(owner_id, auth.user_id))
```

Add the new helper immediately above `_ensure_paperclip_for_owner`:

```python
async def _ensure_paperclip_for_org(
    *,
    org_id: str,
    user_id: str,
    email: str,
    is_admin: bool,
) -> None:
    """Ensure this user has a row in the org's shared Paperclip company.

    Decided by company existence:
      - Company exists → JOIN it (provision_member), regardless of role
        (the role-blind fix).
      - No active company yet → only an ADMIN creates it (provision_org),
        under a per-org lock so concurrent first-pollers can't
        double-create. A member returns gracefully and waits.

    The underlying helpers treat a duplicate-409 as idempotent success, so
    this is safe to call on every /container/status poll.
    """
    async with _org_provision_locks[org_id]:
        provisioning = await build_paperclip_provisioning()
        try:
            owner = await provisioning.find_org_owner(org_id)
        finally:
            await close_paperclip_provisioning(provisioning)

        if owner is None:
            if not is_admin:
                # Member polling a not-yet-bootstrapped org: wait for an
                # admin to create the shared company. Resolves on next poll.
                logger.info(
                    "ensure_user_ready (org member): org=%s has no active "
                    "paperclip company yet — waiting for an admin to "
                    "bootstrap it; will retry on next /container/status poll",
                    org_id,
                )
                return
            # We win the create: become the org's company owner-of-record.
            # Held under the lock so a racing poller re-checks find_org_owner
            # AFTER we've written the active row and falls to the join path.
            await _ensure_paperclip_for_owner(
                owner_id=org_id,
                owner_user_id=user_id,
                owner_email=email,
                org_id=org_id,
            )
            return

    # Company already exists → join it. Outside the create lock so member
    # adds for an established org don't serialize behind one mutex.
    await _ensure_paperclip_for_member(
        org_id=org_id,
        member_user_id=user_id,
        member_email=email,
    )
```

- [ ] **Step 2.4: Run the tests to verify they PASS**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_user_provision.py -v`
Expected: PASS (all).

- [ ] **Step 2.5: Commit**

```bash
git add apps/backend/core/services/user_provision.py apps/backend/tests/unit/services/test_user_provision.py
git commit -m "fix(teams): decide paperclip create-vs-join by company existence, not clerk role

Decouple container provisioning from the paperclip path via a
provision_container flag so the running-container poll path never
force-restarts the box.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Dispatch `ensure_user_ready(provision_container=False)` for all org users on the running-container path

**Files:**
- Modify: `apps/backend/routers/container.py` (running-container dispatch guard, ~line 202)
- Test: `apps/backend/tests/unit/routers/test_container_status_role_blind_dispatch.py` (create)

- [ ] **Step 3.1: Write the failing test**

Create `apps/backend/tests/unit/routers/test_container_status_role_blind_dispatch.py`:

```python
"""The /container/status running-container branch must dispatch
ensure_user_ready for EVERY org-context user (admins included) so a co-admin
joining an already-running org gets their Paperclip workspace — and it must
pass provision_container=False so the running box is never force-restarted."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from routers import container as container_mod


def _org_admin_auth() -> AuthContext:
    return AuthContext(user_id="user_admin2", org_id="org_xyz", org_role="org:admin", email="a@example.com")


def _running():
    return {"service_name": "svc", "status": "running", "substatus": None,
            "created_at": None, "updated_at": None, "last_error": None, "last_error_at": None}


@pytest.mark.asyncio
async def test_running_container_dispatches_ensure_user_ready_for_org_admin():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(_running(), "10.0.0.1"))
    calls = {}

    def capture_task(coro):
        # ensure_user_ready(...) returns a coroutine; capture its name then close.
        calls["qualname"] = coro.__qualname__
        coro.close()

    with (
        patch("routers.container.get_ecs_manager", return_value=ecs),
        patch("routers.container.resolve_owner_id", return_value="org_xyz"),
        patch("routers.container._safe_phase", return_value="ready"),
        patch("routers.container.asyncio.create_task", side_effect=capture_task),
    ):
        await container_mod.container_status(auth=_org_admin_auth())

    assert calls.get("qualname") == "ensure_user_ready", (
        f"org admin on a running container must dispatch ensure_user_ready, got {calls}"
    )
```

- [ ] **Step 3.2: Run the test to verify it FAILS**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_container_status_role_blind_dispatch.py -v`
Expected: FAIL — today's guard `if auth.is_org_context and not auth.is_org_admin:` skips admins, so no task is captured.

- [ ] **Step 3.3: Change the dispatch guard**

In `apps/backend/routers/container.py`, in the running-container section (the comment block ending "...safe to invoke on every member status poll."), change:

```python
    if auth.is_org_context and not auth.is_org_admin:
        asyncio.create_task(ensure_user_ready(auth))
```

to:

```python
    # Role-blind paperclip: every org-context caller (admin or member)
    # re-runs ensure_user_ready so their per-user Paperclip workspace row is
    # ensured against the org's shared company. provision_container=False
    # because the container is already running here — ensure_user_ready must
    # NOT trigger provision_user_container (it force-restarts a running
    # service). Excluding admins here was the /teams "team workspace not
    # provisioned" bug for multi-admin orgs.
    if auth.is_org_context:
        asyncio.create_task(ensure_user_ready(auth, provision_container=False))
```

Also update the preceding comment block (starting "Org members: their paperclip-membership-add...") so it reads "Org members (and admins):" and no longer implies admins are excluded.

- [ ] **Step 3.4: Run the test to verify it PASSES**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_container_status_role_blind_dispatch.py -v`
Expected: PASS

- [ ] **Step 3.5: Commit**

```bash
git add apps/backend/routers/container.py apps/backend/tests/unit/routers/test_container_status_role_blind_dispatch.py
git commit -m "fix(teams): provision paperclip for org admins on running-container poll

Running-container branch now dispatches ensure_user_ready(provision_container=False)
for all org users, fixing the /teams 409 for co-admins without restarting the box.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Full verification

**Files:** none (verification only)

- [ ] **Step 4.1: Run the provisioning + container test files**

Run:
```bash
cd apps/backend && uv run pytest \
  tests/unit/services/test_user_provision.py \
  tests/unit/services/test_paperclip_provisioning.py \
  tests/unit/routers/test_container_provision_gating.py \
  tests/unit/routers/test_container_provision_metrics.py \
  tests/unit/routers/test_container_status_role_blind_dispatch.py \
  -v
```
Expected: all PASS.

- [ ] **Step 4.2: Run the full backend suite + lint**

Run: `cd apps/backend && uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .`
Expected: green. Fix any fallout (dependent systems — a failure elsewhere is likely caused by this change).

- [ ] **Step 4.3: Record the self-heal + no-restart reasoning in the PR description**

By re-reading the final `container.py` + `user_provision.py`, confirm and write up: a co-admin polling `/container/status` on a running org container → running-container branch → `ensure_user_ready(provision_container=False)` → `_ensure_paperclip_for_org` → `find_org_owner` returns the existing company → `provision_member` writes their row; `provision_container=False` means no `background_provision`, so the running box is never restarted. Next `/teams` request resolves a context.

---

## Self-Review

**Spec coverage:**
- Defect 1 (admins skipped on running-container poll) → Task 3.
- Defect 2 (role-routing fragments the company) → Task 2 (`_ensure_paperclip_for_org` joins when a company exists).
- Defect 3 (concurrent first-pollers double-create) → Task 2 (per-org lock + re-check inside lock).
- Restart hazard (the Task 1 finding) → Task 2 `provision_container` flag + owner-gated dispatch; Task 3 passes False.
- Self-heal for currently-stuck users → Task 4 Step 4.3.

**Placeholder scan:** No TBD/TODO; every code step shows complete code and exact commands.

**Type consistency:** New symbols: `_ensure_paperclip_for_org(*, org_id, user_id, email)` and the `provision_container: bool = True` kwarg on `ensure_user_ready`. Both used consistently across Tasks 2/3 and tests. `_ensure_paperclip_for_owner` / `_ensure_paperclip_for_member` / `find_org_owner` / `background_provision(owner_id, user_id)` keep existing signatures.

**Out of scope (intentional):** privilege differentiation, cross-instance create lock, Clerk webhook-driven provisioning.
```