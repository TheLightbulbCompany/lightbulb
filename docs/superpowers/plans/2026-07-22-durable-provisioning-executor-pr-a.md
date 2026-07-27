# Durable Provisioning Executor — PR-A (SFN infra + runner, dark) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Step Functions provisioning executor dark — state machine + one-shot ECS provisioner task in CDK, a `provision_runner.py` CLI, and a `dispatch_provision` path behind `PROVISION_EXECUTOR` (default `"inline"` everywhere) — with zero behavior change until the flag flips.

**Architecture:** See `docs/superpowers/specs/2026-07-22-durable-provisioning-executor-design.md` (read §3, §4, §11 before starting). The runner is the existing `provision_user_container` code executed in a one-shot Fargate task (backend image, backend task role, backend env, `serviceSg`); the state machine retries transient failures and backstops SIGKILL with claim-conditioned native DynamoDB writes. PR-B (synchronous doors) and PR-C (deletion pass) are separate later plans.

**Tech Stack:** Python 3 / FastAPI / boto3 / DynamoDB repositories (no ORM); AWS CDK (TypeScript) `aws-stepfunctions` + `aws-stepfunctions-tasks` (bundled in `aws-cdk-lib`, no new deps). Backend tests: `cd apps/backend && uv run pytest tests/ -v`. Infra tests: `cd apps/infra && npm test`.

## Global Constraints

- **Dark PR: with `PROVISION_EXECUTOR="inline"` (the default), runtime behavior is byte-identical to main.** Every existing test must pass unchanged.
- **The runner emits the PR-1 status contract verbatim** — it reuses `provision_user_container` and the row-writing repos; do not add new status/substatus values beyond `error_retrying` (written by the state machine's MarkRetrying).
- **Execution names**: `provision-{owner_id}-{claim_id[:8]}`, charset `[0-9A-Za-z-_]`, ≤80 chars (SFN constraint; Clerk `user_…`/`org_…` ids are in-alphabet).
- **Execution input must be byte-deterministic** (`json.dumps(..., sort_keys=True, separators=(",", ":"))`) — StartExecution idempotency depends on identical name+input.
- **Exit codes are the runner↔machine ABI**: `0` ok, `10` deterministic failure, `11` transient failure, `20` superseded. Never renumber.
- **Provisioner task MUST use `serviceSg`** — the per-user container 18789 ingress rule (`service-stack.ts:197-204`) is keyed to that SG.
- **No new cross-stack exports** — everything lands in `service-stack.ts` (see `feedback_cdk_crossstack_export_removal_two_phase`).
- Backend CI test scope is `tests/`, not `tests/unit`. Run the full backend suite + full infra suite at the end.
- Commits must not reference PR numbers in code comments.

---

### Task 1: Claim-stamping — `try_claim_provision` learns an execution name

**Files:**
- Modify: `apps/backend/core/repositories/container_repo.py:130-199` (`try_claim_provision`)
- Test: `apps/backend/tests/unit/repositories/test_container_repo.py` (existing moto-backed suite — add cases)

**Interfaces:**
- Consumes: nothing new.
- Produces: `try_claim_provision(owner_id: str, *, claim_id: str | None = None, execution_name: str | None = None) -> str | None` — same return (the winning fencing token or None). When `execution_name` is given, the winning conditional write also sets `provision_execution_name`; when `claim_id` is given, that exact token is used instead of a fresh uuid. Task 2 depends on both parameters.

- [ ] **Step 1: Write the failing tests** (append to the existing moto-backed suite; mirror its fixture/setup style):

```python
async def test_try_claim_provision_stamps_execution_name(containers_table):
    claim = await container_repo.try_claim_provision(
        "user_abc", claim_id="fixed-claim-uuid", execution_name="provision-user_abc-fixedcla"
    )
    assert claim == "fixed-claim-uuid"
    row = await container_repo.get_by_owner_id("user_abc")
    assert row["provision_execution_name"] == "provision-user_abc-fixedcla"
    assert row["provision_claim_id"] == "fixed-claim-uuid"
    assert row["status"] == "provisioning"


async def test_try_claim_provision_without_name_leaves_field_absent(containers_table):
    claim = await container_repo.try_claim_provision("user_def")
    assert claim is not None
    row = await container_repo.get_by_owner_id("user_def")
    assert "provision_execution_name" not in row
```

- [ ] **Step 2: Run them, confirm FAIL** (unexpected-kwarg TypeError):

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo.py -k execution_name -v`
Expected: FAIL with `TypeError: try_claim_provision() got an unexpected keyword argument`

- [ ] **Step 3: Extend the function.** Change the signature and thread the two optionals into the existing UpdateExpression (the ConditionExpression is untouched — claimability semantics do not change):

```python
async def try_claim_provision(
    owner_id: str,
    *,
    claim_id: str | None = None,
    execution_name: str | None = None,
) -> str | None:
```

Inside, replace `claim_id = str(uuid.uuid4())` with `claim_id = claim_id or str(uuid.uuid4())`, and build the expression conditionally:

```python
    update_expr = (
        "SET #s = :prov, substatus = :sub, provision_claim_id = :cid, "
        "provision_claimed_at = :now, updated_at = :iso, "
        "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
    )
    values = {
        ":prov": "provisioning",
        ":sub": "claimed",
        ":cid": claim_id,
        ":now": now_epoch,
        ":iso": now_iso,
        ":newid": str(uuid.uuid4()),
        ":error": "error",
        ":stopped": "stopped",
        ":stale": now_epoch - PROVISION_CLAIM_STALE_S,
    }
    if execution_name is not None:
        update_expr += ", provision_execution_name = :ename"
        values[":ename"] = execution_name
```

and pass `UpdateExpression=update_expr`, `ExpressionAttributeValues=values` to the existing `update_item` call. Extend the docstring with one sentence: the SFN dispatcher stamps the execution name in the same atomic write so a crash between claim and StartExecution is healable by name.

- [ ] **Step 4: Run the whole repo suite, confirm PASS** (new cases + no regression):

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/container_repo.py apps/backend/tests/unit/repositories/test_container_repo.py
git commit -m "feat(provision): allow claim to stamp an SFN execution name atomically"
```

---

### Task 2: `provision_dispatch` — StartExecution with crash-safe idempotent dispatch

**Files:**
- Create: `apps/backend/core/services/provision_dispatch.py`
- Modify: `apps/backend/core/config.py` (two settings fields)
- Test: `apps/backend/tests/unit/services/test_provision_dispatch.py` (create)

**Interfaces:**
- Consumes: `container_repo.get_by_owner_id`, `container_repo.try_claim_provision(claim_id=…, execution_name=…)` (Task 1), `container_repo.release_provision_to_error`, `settings.PROVISION_STATE_MACHINE_ARN`, `settings.AWS_REGION`.
- Produces: `async def dispatch_provision(owner_id: str) -> None` (never raises); helpers `execution_name(owner_id, claim_id) -> str` and `execution_input(owner_id, claim_id) -> str`. Task 4 calls `dispatch_provision`; Task 6's state machine consumes the input shape `{"attempt": 0, "claim_id": …, "command": […], "owner_id": …}`.

- [ ] **Step 1: Add the settings fields** in `apps/backend/core/config.py` next to the other container settings, matching the file's `os.getenv` style:

```python
    # Provisioning executor: "inline" (asyncio task in this process, default)
    # or "sfn" (dispatch to the isol8-{env}-provision state machine).
    PROVISION_EXECUTOR: str = os.getenv("PROVISION_EXECUTOR", "inline")
    PROVISION_STATE_MACHINE_ARN: str = os.getenv("PROVISION_STATE_MACHINE_ARN", "")
```

- [ ] **Step 2: Write the failing tests:**

```python
"""Tests for core/services/provision_dispatch.py (SFN dispatch path)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import provision_dispatch


def _already_exists_error():
    return ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "closed"}},
        "StartExecution",
    )


@pytest.fixture
def sfn(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(provision_dispatch, "_client", lambda: client)
    return client


async def test_fresh_dispatch_claims_stamps_and_starts(monkeypatch, sfn):
    monkeypatch.setattr(
        provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None)
    )
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(owner=owner_id, claim=claim_id, name=execution_name)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    await provision_dispatch.dispatch_provision("user_abc")
    assert claim_calls["name"] == provision_dispatch.execution_name("user_abc", claim_calls["claim"])
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == claim_calls["name"]
    payload = json.loads(kwargs["input"])
    assert payload["owner_id"] == "user_abc" and payload["attempt"] == 0
    assert payload["command"][:3] == ["python", "-m", "scripts.provision_runner"]


async def test_claim_lost_starts_nothing(monkeypatch, sfn):
    monkeypatch.setattr(
        provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        provision_dispatch.container_repo, "try_claim_provision", AsyncMock(return_value=None)
    )
    await provision_dispatch.dispatch_provision("user_abc")
    sfn.start_execution.assert_not_called()


async def test_stamped_running_row_reissues_same_start(monkeypatch, sfn):
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(
        provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row)
    )
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    await provision_dispatch.dispatch_provision("user_abc")
    claim.assert_not_awaited()  # branch A never mints a new claim
    assert sfn.start_execution.call_args.kwargs["name"] == name


async def test_stamped_but_closed_execution_is_logged_not_raised(monkeypatch, sfn):
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(
        provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row)
    )
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("user_abc")  # must not raise


async def test_start_failure_releases_claim_to_error(monkeypatch, sfn):
    monkeypatch.setattr(
        provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None)
    )

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(
        provision_dispatch.container_repo, "release_provision_to_error", release
    )
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )
    await provision_dispatch.dispatch_provision("user_abc")
    release.assert_awaited_once()
```

- [ ] **Step 3: Run, confirm FAIL** (`ModuleNotFoundError: core.services.provision_dispatch`):

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_dispatch.py -v`

- [ ] **Step 4: Implement the module:**

```python
"""SFN dispatch for container provisioning (PROVISION_EXECUTOR="sfn").

Replaces only the EXECUTOR of background_provision — the trigger (the
/container/status poll and everything funneling through background_provision)
and the status contract are unchanged. Dispatch is crash-safe by execution
NAME: the claim write stamps the name atomically (container_repo), and
StartExecution is idempotent for STANDARD workflows on identical name+input,
so any later poll can safely re-issue the same call. See
docs/superpowers/specs/2026-07-22-durable-provisioning-executor-design.md §3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import container_repo

logger = logging.getLogger(__name__)

_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=settings.AWS_REGION)
    return _sfn


def execution_name(owner_id: str, claim_id: str) -> str:
    # <=80 chars, [0-9A-Za-z-_] only; owner ids (user_…/org_…) are in-alphabet.
    return f"provision-{owner_id}-{claim_id[:8]}"


def execution_input(owner_id: str, claim_id: str) -> str:
    # Byte-deterministic: idempotent StartExecution requires identical input.
    return json.dumps(
        {
            "attempt": 0,
            "claim_id": claim_id,
            "command": [
                "python",
                "-m",
                "scripts.provision_runner",
                "--owner",
                owner_id,
                "--claim",
                claim_id,
            ],
            "owner_id": owner_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def dispatch_provision(owner_id: str) -> None:
    """Start (or idempotently re-start) the provisioning state machine for
    this owner. Never raises — dispatch failure releases the claim to
    ``error`` so the status poll's normal retry re-dispatches."""
    row = await container_repo.get_by_owner_id(owner_id)

    # Branch A: a provisioning row with a self-consistent stamped execution
    # name. Re-issue StartExecution with the SAME name+input: running → same
    # ARN (cross-instance single-flight); never-started (crash between claim
    # and start) → starts now; closed → ExecutionAlreadyExists, benign (the
    # row reconciles via the runner's writes or the stale clause).
    if row and row.get("status") == "provisioning":
        stamped = row.get("provision_execution_name")
        claim = row.get("provision_claim_id")
        if stamped and claim and stamped == execution_name(owner_id, claim):
            await _start(owner_id, claim, healing=True)
        return

    # Branch B: fresh dispatch — mint claim + name in one atomic write.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(
        owner_id, claim_id=claim, execution_name=name
    )
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        return
    started = await _start(owner_id, claim, healing=False)
    if not started:
        try:
            await container_repo.release_provision_to_error(owner_id, claim)
        except Exception:  # noqa: BLE001 — release is best-effort
            logger.exception("dispatch_provision: release failed for owner %s", owner_id)


async def _start(owner_id: str, claim_id: str, *, healing: bool) -> bool:
    name = execution_name(owner_id, claim_id)
    try:
        await asyncio.to_thread(
            _client().start_execution,
            stateMachineArn=settings.PROVISION_STATE_MACHINE_ARN,
            name=name,
            input=execution_input(owner_id, claim_id),
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
            # Closed execution with this name: it already ran to completion.
            logger.info(
                "dispatch_provision: execution %s already closed (healing=%s) — no-op",
                name,
                healing,
            )
            return True
        logger.exception("dispatch_provision: StartExecution failed for %s", name)
        return False
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: StartExecution failed for %s", name)
        return False
```

- [ ] **Step 5: Run, confirm PASS:**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_dispatch.py -v`

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/provision_dispatch.py apps/backend/core/config.py apps/backend/tests/unit/services/test_provision_dispatch.py
git commit -m "feat(provision): SFN dispatch with crash-safe idempotent StartExecution"
```

---

### Task 3: The provisioning runner (one-shot process)

**Files:**
- Create: `apps/backend/scripts/provision_runner.py`
- Modify: `apps/backend/core/containers/ecs_manager.py` (add a public `wait_for_running` wrapper next to `_await_running_transition`, ~line 2325)
- Modify: `apps/backend/core/services/container_provision.py` (add `is_retryable_provision_error` — **if the honest-failure-state PR already merged an equivalent classifier here, reuse/rename to that one instead of duplicating**)
- Test: `apps/backend/tests/unit/scripts/test_provision_runner.py` (create)

**Interfaces:**
- Consumes: `resolve_provider_choice`, `get_ecs_manager().provision_user_container(owner_id, provider_choice=…, byo_provider=…, owner_type=…, claim_id=…)`, `ProvisionSuperseded`, `container_repo.get_by_owner_id`.
- Produces: module runnable as `python -m scripts.provision_runner --owner X --claim Y`; `async def run(owner_id, claim_id) -> int` returning the exit-code ABI (`0/10/11/20`); `EcsManager.wait_for_running(owner_id) -> None` (public alias for `_await_running_transition`); `is_retryable_provision_error(exc) -> bool` in `container_provision.py`. Task 6's state machine consumes the exit codes.

- [ ] **Step 1: Add the two small helpers.** In `ecs_manager.py`, directly above `_await_running_transition`:

```python
    async def wait_for_running(self, user_id: str) -> None:
        """Public foreground wait for the provisioning -> running transition.

        The one-shot provision runner awaits this so the transition is owned
        by a process the state machine supervises, instead of a detached
        create_task that dies with its host process."""
        await self._await_running_transition(user_id)
```

In `container_provision.py` (module level, below `provider_choice_from_row`):

```python
def is_retryable_provision_error(exc: BaseException) -> bool:
    """Transient (retry helps: ECS 5xx, throttle, Spot, network) vs
    deterministic (retry cannot help: bad config, unknown provider, 4xx).
    Defaults to retryable — attempts are bounded by the state machine, and
    surfacing a transient as terminal is the worse failure mode."""
    from core.services.paperclip_admin_client import PaperclipApiError

    if isinstance(exc, PaperclipApiError):
        return not (400 <= (exc.status_code or 0) < 500)
    message = str(exc).lower()
    deterministic_markers = (
        "byo_provider is required",
        "unknown provider_choice",
        "validationexception",
        "invalidparameter",
        "accessdenied",
    )
    return not any(marker in message for marker in deterministic_markers)
```

- [ ] **Step 2: Write the failing runner tests:**

```python
"""Tests for scripts/provision_runner.py exit-code mapping."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories.container_repo import ProvisionSuperseded
from scripts import provision_runner


@pytest.fixture
def manager(monkeypatch):
    mgr = MagicMock()
    mgr.provision_user_container = AsyncMock(return_value="svc-name")
    mgr.wait_for_running = AsyncMock()
    monkeypatch.setattr(provision_runner, "_get_manager", lambda: mgr)
    monkeypatch.setattr(
        provision_runner,
        "_resolve_provider",
        AsyncMock(return_value=("bedrock_claude", None)),
    )
    return mgr


def _row(monkeypatch, row):
    monkeypatch.setattr(provision_runner, "_get_row", AsyncMock(return_value=row))


async def test_running_row_exits_zero(monkeypatch, manager):
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.wait_for_running.assert_awaited_once_with("user_a")


async def test_superseded_exits_20_without_row_read(monkeypatch, manager):
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_deterministic_error_exits_10(monkeypatch, manager):
    manager.provision_user_container.side_effect = Exception(
        "byo_provider is required when provider_choice == 'byo_key'"
    )
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC


async def test_transient_error_exits_11(monkeypatch, manager):
    manager.provision_user_container.side_effect = Exception("ECS ThrottlingException")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_error_row_maps_by_retryable_field(monkeypatch, manager):
    _row(monkeypatch, {"status": "error", "retryable": False})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    _row(monkeypatch, {"status": "error", "retryable": True})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_external_state_change_exits_zero(monkeypatch, manager):
    _row(monkeypatch, {"status": "stopped"})  # admin stop mid-wait: not ours to fight
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
```

- [ ] **Step 3: Run, confirm FAIL** (`ModuleNotFoundError: scripts.provision_runner`):

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_provision_runner.py -v`

- [ ] **Step 4: Implement the runner:**

```python
"""One-shot container-provision runner, executed as an ECS task by the
isol8-{env}-provision Step Functions state machine (never by the backend).

Exit codes are the machine's ABI (design doc §3): 0 provisioned+healthy,
10 deterministic failure (no retry), 11 transient failure (machine retries),
20 superseded by a deliberate reprovision (machine stops quietly).

The runner owns the provisioning -> running transition in the FOREGROUND
(wait_for_running) — the detached create_task pollers ecs_manager fires
internally die with this process, and that is fine: the foreground wait is
the one that matters, and every row write is condition-guarded.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

EXIT_OK = 0
EXIT_DETERMINISTIC = 10
EXIT_TRANSIENT = 11
EXIT_SUPERSEDED = 20

logger = logging.getLogger("provision_runner")


def _get_manager():
    from core.containers import get_ecs_manager

    return get_ecs_manager()


async def _resolve_provider(owner_id: str):
    from core.services.container_provision import resolve_provider_choice

    return await resolve_provider_choice(owner_id)


async def _get_row(owner_id: str):
    from core.repositories import container_repo

    return await container_repo.get_by_owner_id(owner_id)


async def run(owner_id: str, claim_id: str) -> int:
    from core.auth import is_org_owner_id
    from core.observability.logging import bind_log_context
    from core.repositories.container_repo import ProvisionSuperseded
    from core.services.container_provision import is_retryable_provision_error

    bind_log_context(user_id=owner_id)
    manager = _get_manager()
    try:
        provider_choice, byo_provider = await _resolve_provider(owner_id)
        owner_type = "org" if is_org_owner_id(owner_id) else "personal"
        await manager.provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            owner_type=owner_type,
            claim_id=claim_id,
        )
        await manager.wait_for_running(owner_id)
    except ProvisionSuperseded:
        logger.info("provision superseded for owner %s — standing down", owner_id)
        return EXIT_SUPERSEDED
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        # provision_user_container already released the lease to error with
        # cause fields; the exit code only tells the machine whether to retry.
        logger.exception("provision failed for owner %s", owner_id)
        return EXIT_TRANSIENT if is_retryable_provision_error(exc) else EXIT_DETERMINISTIC

    row = await _get_row(owner_id)
    status = (row or {}).get("status")
    if status == "running":
        return EXIT_OK
    if status == "error":
        return EXIT_TRANSIENT if (row or {}).get("retryable", True) else EXIT_DETERMINISTIC
    # Any other status (stopped/deleted/provisioning-under-new-lease): an
    # external actor owns the row now — succeed quietly, write nothing.
    logger.info("owner %s left provisioning externally (status=%s)", owner_id, status)
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--claim", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    sys.exit(asyncio.run(run(args.owner, args.claim)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run, confirm PASS:**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_provision_runner.py -v`

- [ ] **Step 6: Commit**

```bash
git add apps/backend/scripts/provision_runner.py apps/backend/core/containers/ecs_manager.py apps/backend/core/services/container_provision.py apps/backend/tests/unit/scripts/test_provision_runner.py
git commit -m "feat(provision): one-shot provision runner with exit-code ABI for the state machine"
```

---

### Task 4: Route `background_provision` through the flag

**Files:**
- Modify: `apps/backend/core/services/container_provision.py:58-96` (`background_provision`, top of function)
- Test: `apps/backend/tests/unit/services/test_container_provision.py` (add cases; create if absent)

**Interfaces:**
- Consumes: `provision_dispatch.dispatch_provision` (Task 2), `settings.PROVISION_EXECUTOR`.
- Produces: unchanged signature `background_provision(owner_id, clerk_user_id)`; every existing `create_task(background_provision(...))` caller (status poll, wake, silent wake, ensure_user_ready, provider change, reprovision_for_user) rides the flag with zero call-site changes.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_background_provision_sfn_flag_dispatches(monkeypatch):
    from core.services import container_provision

    monkeypatch.setattr(container_provision.settings, "PROVISION_EXECUTOR", "sfn")
    dispatched = AsyncMock()
    monkeypatch.setattr(
        "core.services.provision_dispatch.dispatch_provision", dispatched
    )
    claimed = AsyncMock()
    monkeypatch.setattr(container_provision.container_repo, "try_claim_provision", claimed)
    await container_provision.background_provision("user_a", "user_a")
    dispatched.assert_awaited_once_with("user_a")
    claimed.assert_not_awaited()  # inline path never entered


async def test_background_provision_inline_flag_unchanged(monkeypatch):
    from core.services import container_provision

    monkeypatch.setattr(container_provision.settings, "PROVISION_EXECUTOR", "inline")
    dispatched = AsyncMock()
    monkeypatch.setattr(
        "core.services.provision_dispatch.dispatch_provision", dispatched
    )
    monkeypatch.setattr(
        container_provision.container_repo, "try_claim_provision", AsyncMock(return_value=None)
    )
    await container_provision.background_provision("user_a", "user_a")
    dispatched.assert_not_awaited()
```

- [ ] **Step 2: Run, confirm FAIL.**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_provision.py -k executor -v`
(Adjust `-k` to the test names; expected: the sfn test fails because dispatch is never called.)

- [ ] **Step 3: Implement.** At the top of `background_provision`, before `bind_log_context` (import `settings` from `core.config` at module top; keep the dispatch import lazy to avoid a boto3 stepfunctions client at module load):

```python
    if settings.PROVISION_EXECUTOR == "sfn":
        from core.services.provision_dispatch import dispatch_provision

        # The SFN executor owns claim + run + retry end-to-end; this function
        # remains the single funnel so every existing create_task caller
        # (status poll, wake, ensure_user_ready, provider change,
        # reprovision_for_user) rides the flag with no call-site changes.
        await dispatch_provision(owner_id)
        return
```

- [ ] **Step 4: Run, confirm PASS** (both new tests + existing container_provision tests):

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_provision.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/container_provision.py apps/backend/tests/unit/services/test_container_provision.py
git commit -m "feat(provision): route background_provision through PROVISION_EXECUTOR flag"
```

---

### Task 5: CDK — extract the shared backend env/secrets (pure refactor)

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts:1046-1448` (backend task-def/container block)
- Test: `apps/infra/test/service-stack.test.ts` (existing suite is the no-diff proof)

**Interfaces:**
- Produces: two consts declared ABOVE the backend task-def (`const backendContainerEnvironment: Record<string, string>` and `const backendContainerSecrets: Record<string, ecs.Secret>`), consumed by the backend `addContainer` and by Task 6's provisioner container. `core.config.Settings` reads env only (no boot-time Secrets Manager fetch), so identical env ⇒ identical runtime config for both processes.

- [ ] **Step 1: Extract.** Cut the object literal currently passed as `environment:` in the backend `taskDef.addContainer` call (the block spanning ~1070-1320, from `ENVIRONMENT:` through the conditional `ALERT_PAGE_TOPIC_ARN` spread) into `const backendContainerEnvironment = { …exact same content… }` declared immediately above the `FargateTaskDefinition` at line 1046. Do the same for the `secrets:` literal (~1321-1432) into `const backendContainerSecrets`. Pass both by reference: `environment: backendContainerEnvironment, secrets: backendContainerSecrets`. **No key added, removed, or reordered.**

- [ ] **Step 2: Prove no template change:**

Run: `cd apps/infra && npm test`
Expected: PASS — every existing service-stack assertion/snapshot unchanged.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts
git commit -m "refactor(infra): extract backend container env/secrets for reuse by the provisioner task-def"
```

---

### Task 6: CDK — provisioner task-def, state machine, grants, alarms

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (new section between the Task 5 consts and the backend task-def; plus 2 env keys on the backend container and 1 IAM statement on `this.taskRole`)
- Test: `apps/infra/test/service-stack.test.ts` (add assertions)

**Interfaces:**
- Consumes: `backendContainerEnvironment` / `backendContainerSecrets` (Task 5), `backendImage` (line 162), `this.taskRole`, `taskExecutionRole`, `serviceSg`, `logGroup`, `props.container.cluster`, `props.container.efsFileSystem`, `props.database.containersTable`.
- Produces: state machine `isol8-{env}-provision` whose input is Task 2's shape and whose ECS override command is `$.command`; backend env `PROVISION_EXECUTOR="inline"` + `PROVISION_STATE_MACHINE_ARN`; `states:StartExecution/DescribeExecution/StopExecution` on the backend task role (StopExecution is unused until PR-B; granting now avoids a second IAM PR).

- [ ] **Step 1: Add imports** at the top of `service-stack.ts`:

```ts
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
```

- [ ] **Step 2: Write failing infra tests** (append to `service-stack.test.ts`, matching its existing Template-assertion style):

```ts
test("provision state machine and provisioner task-def exist", () => {
  template.resourceCountIs("AWS::StepFunctions::StateMachine", 1);
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    Family: `isol8-${env}-provisioner`,
  });
});

test("backend task role may start provision executions", () => {
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: ["states:StartExecution", "states:DescribeExecution", "states:StopExecution"],
        }),
      ]),
    }),
  });
});
```

Run: `cd apps/infra && npm test` — expected: the two new tests FAIL.

- [ ] **Step 3: Add the provisioner task definition** (after the Task 5 consts, before the backend `FargateTaskDefinition`):

```ts
    // ---------------------------------------------------------------------
    // Provisioning executor: one-shot provisioner task + state machine.
    // Same image / roles / env / SG as the backend so provision_user_container
    // runs with identical capability, but OUTSIDE the deployable web process.
    // serviceSg is mandatory: the per-user container 18789 ingress rule
    // (ContainerFromServiceIngress above) is keyed to it.
    // ---------------------------------------------------------------------
    const provisionerTaskDef = new ecs.FargateTaskDefinition(this, "ProvisionerTaskDef", {
      family: `isol8-${env}-provisioner`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      taskRole: this.taskRole,
      executionRole: taskExecutionRole,
    });
    provisionerTaskDef.addVolume({
      name: "efs-users",
      efsVolumeConfiguration: {
        fileSystemId: props.container.efsFileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { iam: "ENABLED" },
      },
    });
    const provisionerContainer = provisionerTaskDef.addContainer("provisioner", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      environment: backendContainerEnvironment,
      secrets: backendContainerSecrets,
      // No healthCheck and no port mappings: a batch job must not be killed
      // for not serving HTTP. Command is overridden per-execution by the
      // state machine ($.command).
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "provisioner", logGroup }),
    });
    provisionerContainer.addMountPoints({
      containerPath: "/mnt/efs",
      sourceVolume: "efs-users",
      readOnly: false,
    });
```

Match the backend volume's exact `efsVolumeConfiguration` shape from lines 1055-1062 (copy it verbatim — if it specifies an access point or root directory, mirror it).

- [ ] **Step 4: Add the state machine** (directly below Step 3's code). Exit-code ABI: 0 ok / 10 deterministic / 11 transient / 20 superseded (Task 3). Three total attempts. All DDB writes are claim-conditioned so a superseding run's row is never clobbered:

```ts
    const runProvisioner = new tasks.EcsRunTask(this, "RunProvisioner", {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: props.container.cluster,
      taskDefinition: provisionerTaskDef,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.VERSION1_4, // EFS requires >= 1.4
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      assignPublicIp: false,
      containerOverrides: [
        {
          containerDefinition: provisionerContainer,
          command: sfn.JsonPath.listAt("$.command"),
        },
      ],
      resultPath: sfn.JsonPath.DISCARD,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(30)),
    });

    const succeedProvisioned = new sfn.Succeed(this, "Provisioned");
    const succeedSuperseded = new sfn.Succeed(this, "Superseded");
    const failProvision = new sfn.Fail(this, "ProvisionFailed", {
      error: "ProvisionFailed",
      cause: "provisioner exhausted retries or failed deterministically",
    });

    // Claim-conditioned row writes — the SIGKILL backstop. On a clean runner
    // exit the runner already wrote these values; these writes are idempotent
    // re-assertions that also cover the case where the runner died mid-write.
    // Condition accepts BOTH "provisioning" (runner died before its own
    // release) AND "error" (runner released cleanly, this re-asserts) — a
    // clean transient failure leaves the row at "error", and conditioning on
    // "provisioning" alone would fail every write after attempt 1 and kill
    // the retry loop. Rows at "running"/"stopped", or under a different
    // claim, are never touched (routes to Superseded via the catch below).
    const ddbWrite = (id: string, updateExpr: string, values: Record<string, tasks.DynamoAttributeValue>) =>
      new tasks.DynamoUpdateItem(this, id, {
        table: props.database.containersTable,
        key: {
          owner_id: tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.owner_id")),
        },
        updateExpression: updateExpr,
        conditionExpression: "provision_claim_id = :cid AND (#s = :prov OR #s = :err)",
        expressionAttributeNames: { "#s": "status" },
        expressionAttributeValues: {
          ...values,
          ":cid": tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$.claim_id")),
          ":prov": tasks.DynamoAttributeValue.fromString("provisioning"),
          ":ts": tasks.DynamoAttributeValue.fromString(sfn.JsonPath.stringAt("$$.State.EnteredTime")),
        },
        resultPath: sfn.JsonPath.DISCARD,
      });

    const markFailedDeterministic = ddbWrite(
      "MarkFailedDeterministic",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("provision_failed"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(false),
      },
    );
    const markFailedExhausted = ddbWrite(
      "MarkFailedExhausted",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("provision_failed"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(true),
      },
    );
    const markRetrying = ddbWrite(
      "MarkRetrying",
      "SET #s = :err, substatus = :sub, retryable = :r, updated_at = :ts",
      {
        ":err": tasks.DynamoAttributeValue.fromString("error"),
        ":sub": tasks.DynamoAttributeValue.fromString("error_retrying"),
        ":r": tasks.DynamoAttributeValue.fromBoolean(true),
      },
    );
    // ConditionalCheckFailed on any write means a deliberate reprovision took
    // the row over — stop quietly, exactly the runner's superseded rule.
    // (Optimized-integration exception prefix is "DynamoDB", uppercase DB.)
    for (const write of [markFailedDeterministic, markFailedExhausted, markRetrying]) {
      write.addCatch(succeedSuperseded, {
        errors: ["DynamoDB.ConditionalCheckFailedException"],
        resultPath: sfn.JsonPath.DISCARD,
      });
    }
    markFailedDeterministic.next(failProvision);
    markFailedExhausted.next(failProvision);

    const bumpAttempt = new sfn.Pass(this, "BumpAttempt", {
      parameters: {
        "attempt.$": "States.MathAdd($.attempt, 1)",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    const wait60 = new sfn.Wait(this, "WaitBeforeRetry", {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
    });
    markRetrying.next(wait60);
    wait60.next(bumpAttempt);
    bumpAttempt.next(runProvisioner);

    const transientGate = new sfn.Choice(this, "AttemptsRemain")
      .when(sfn.Condition.numberLessThan("$.attempt", 2), markRetrying)
      .otherwise(markFailedExhausted);

    // Non-TaskFailed catches (timeout, launch failure): the Cause is not the
    // ECS task JSON, so skip parsing and treat as transient.
    const shapeForRetry = new sfn.Pass(this, "ShapeForRetry", {
      parameters: {
        "attempt.$": "$.attempt",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    shapeForRetry.next(transientGate);

    // TaskFailed: Cause is the stringified ECS task object — parse it and
    // classify on the container exit code (design doc §11: a non-zero
    // essential-container exit fails the .sync state with States.TaskFailed).
    const parseCause = new sfn.Pass(this, "ParseFailureCause", {
      parameters: {
        "attempt.$": "$.attempt",
        "cause.$": "States.StringToJson($.error.Cause)",
        "claim_id.$": "$.claim_id",
        "command.$": "$.command",
        "owner_id.$": "$.owner_id",
      },
    });
    const classify = new sfn.Choice(this, "ClassifyFailure")
      .when(sfn.Condition.numberEquals("$.cause.Containers[0].ExitCode", 20), succeedSuperseded)
      .when(sfn.Condition.numberEquals("$.cause.Containers[0].ExitCode", 10), markFailedDeterministic)
      .otherwise(transientGate); // 11, SIGKILL (no exit code), Spot reclaim
    parseCause.next(classify);

    runProvisioner.addCatch(shapeForRetry, {
      errors: ["States.Timeout"],
      resultPath: "$.error",
    });
    runProvisioner.addCatch(parseCause, {
      errors: ["States.TaskFailed"],
      resultPath: "$.error",
    });
    runProvisioner.addCatch(shapeForRetry, {
      errors: ["States.ALL"], // AmazonECS.Unknown launch failures etc.
      resultPath: "$.error",
    });

    const provisionStateMachine = new sfn.StateMachine(this, "ProvisionStateMachine", {
      stateMachineName: `isol8-${env}-provision`,
      definitionBody: sfn.DefinitionBody.fromChainable(runProvisioner.next(succeedProvisioned)),
      timeout: cdk.Duration.hours(2), // hard ceiling across all attempts
    });

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ProvisionStateMachineDispatch",
        actions: ["states:StartExecution", "states:DescribeExecution", "states:StopExecution"],
        resources: [
          provisionStateMachine.stateMachineArn,
          // DescribeExecution/StopExecution operate on execution ARNs.
          `arn:aws:states:${this.region}:${this.account}:execution:isol8-${env}-provision:*`,
        ],
      }),
    );

    new cloudwatch.Alarm(this, "ProvisionExecutionsFailedAlarm", {
      alarmName: `isol8-${env}-provision-executions-failed`,
      metric: provisionStateMachine.metricFailed({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, "ProvisionExecutionsTimedOutAlarm", {
      alarmName: `isol8-${env}-provision-executions-timedout`,
      metric: provisionStateMachine.metricTimedOut({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
```

Note: `addCatch` order matters — CDK emits catches in call order and SFN matches first-match, so `States.Timeout` and `States.TaskFailed` must be added before `States.ALL` (as written above).

- [ ] **Step 5: Wire the backend container.** On the backend `addContainer` call, change `environment:` to spread the shared const plus the two new keys (declaration order: the provisioner/state-machine block from Steps 3-4 sits above the backend task-def, so `provisionStateMachine` is in scope):

```ts
      environment: {
        ...backendContainerEnvironment,
        // Provisioning executor flag: "inline" until the per-env flip
        // (dev first, then prod) documented in the durable-provisioning
        // design doc. The state machine deploys dark alongside it.
        PROVISION_EXECUTOR: "inline",
        PROVISION_STATE_MACHINE_ARN: provisionStateMachine.stateMachineArn,
      },
```

- [ ] **Step 6: Run infra tests, confirm PASS:**

Run: `cd apps/infra && npm test`
Expected: the two new tests pass; all pre-existing service-stack tests still pass.

- [ ] **Step 7: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/test/service-stack.test.ts
git commit -m "feat(infra): provisioning state machine + one-shot provisioner task (dark)"
```

---

### Task 7: Local synth check + full suites

**Files:**
- No source changes expected (fix-forward if either check fails).

- [ ] **Step 1: cdklocal-app synth** (LocalStage deploys ServiceStack, so the new constructs must synthesize under the local app; runtime SFN emulation is NOT required — local keeps `PROVISION_EXECUTOR="inline"`):

Run: `cd apps/infra && npx cdk synth --app "npx ts-node --prefer-ts-exts lib/local.ts" "local/*" > /dev/null`
Expected: exit 0. If the StateMachine construct fails local synth, gate it the repo way — whole-construct omission driven by a stage prop (mirror the DnsStack omission precedent in `local-stage.ts:13-16`), and re-run.

- [ ] **Step 2: Full backend suite:**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: PASS (scope is `tests/`, not `tests/unit` — CI runs the full tree).

- [ ] **Step 3: Full infra suite:**

Run: `cd apps/infra && npm test`
Expected: PASS.

- [ ] **Step 4: Commit anything the checks forced, then final commit if dirty**

```bash
git status --short   # expect clean; commit fix-forward changes if any
```

---

## Post-merge validation (dev flip — NOT part of this PR)

Recorded here so the checklist survives: flip `PROVISION_EXECUTOR` to `"sfn"` for dev in service-stack, deploy, then validate per design doc §9.2 — fresh signup; injected transient failure (verify in-machine retry + `error_retrying` surfaced); deterministic failure (bad config → immediate terminal error, no retry); **deploy the backend mid-provision and watch the execution complete anyway** (the headline scenario); parked-trial wake; deliberate door supersede (`POST /recover` mid-provision → runner exits 20, machine ends Superseded). PR-B (synchronous doors) and PR-C (deletion pass: staleness heuristic, `has_live_service`, `_resume_provisioning_transitions`, inline executor) get their own plans after a multi-day dev soak.
