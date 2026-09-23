"""Container-provision dispatch: a stateless Step Functions submitter.

THE single provisioning funnel. A RUNNING execution named
``provision-{owner}-…`` owns the owner's provision; anything else is
re-driveable. There is no claim, lease, attempt token or row fence: every
provision step is idempotent at its source (credentials converge through
``if_not_exists`` + read-back, the IAM role / runtime / trust document adopt
on conflict), so two concurrent executions for one owner converge on the same
end state.

Return contract: ``True`` when an execution owns the work (started now or
already RUNNING), ``False`` when nothing does — start failed, or non-org
refusal. Callers with a recovery marker (notably the trial-conversion
reprovision) must retain it on ``False``. Never raises.

``dispatch_reconcile`` starts a ``reconcile-{owner}-…`` execution on the SAME
machine with ``"mode": "reconcile"`` in its input (absent mode = provision):
the provisioner Lambda runs ``reconcile_runtime`` instead of the runner. No
running-check and no row write -- a reconcile is idempotent and concurrency-safe,
and a second one must start because it reads newer billing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import boto3

from core.auth import is_org_owner_id
from core.config import settings
from core.observability.logging import bind_log_context
from core.repositories import container_repo
from core.services.container_provision import emit_provision_metric

logger = logging.getLogger(__name__)

_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=settings.AWS_REGION)
    return _sfn


def _name_prefix(owner_id: str) -> str:
    # [0-9A-Za-z-_] only; Clerk org ids (org_ + 27 chars) carry no dash, so the
    # trailing dash makes the prefix unique per owner. prefix + 8 hex = ~49 of
    # SFN's 80-char name limit.
    return f"provision-{owner_id}-"


def execution_input(owner_id: str) -> str:
    # mode is always present so the machine's failure Choice never reads a missing path.
    return json.dumps({"owner_id": owner_id, "mode": "provision"})


def _find_running(owner_id: str) -> dict | None:
    # ListExecutions has no name filter, so page RUNNING executions and match the
    # owner's prefix client-side. RUNNING is only in-flight provisions (~1 min
    # each), so this is normally a single page.
    prefix = _name_prefix(owner_id)
    pages = (
        _client()
        .get_paginator("list_executions")
        .paginate(stateMachineArn=settings.PROVISION_STATE_MACHINE_ARN, statusFilter="RUNNING")
    )
    for page in pages:
        for execution in page.get("executions", []):
            if execution.get("name", "").startswith(prefix):
                return execution
    return None


async def running_execution(owner_id: str) -> dict | None:
    """The owner's RUNNING provision execution summary (ListExecutions shape), or None."""
    return await asyncio.to_thread(_find_running, owner_id)


async def dispatch_provision(owner_id: str) -> bool:
    """Start provisioning unless an execution already owns it. Never raises."""
    # Re-bind correlation ids: dispatched via create_task, this can run in a
    # detached task where the HTTP-middleware binds did not propagate.
    bind_log_context(user_id=owner_id)
    if not is_org_owner_id(owner_id):
        # Personal workspaces no longer exist. Refuse here — the one funnel every
        # door goes through — before a row is written or an execution started (an
        # admin reprovision whose Clerk lookup failed open hands this a user_ id).
        # provision_runner step 0 refuses too, as a backstop.
        logger.error("dispatch_provision: %s is not a workspace (org) — refusing to provision", owner_id)
        emit_provision_metric("refused_non_org")
        return False
    try:
        # Racy by design: two dispatchers can both see nothing running and both
        # start. That is safe ONLY because every provision step is idempotent, so
        # the two executions converge — do not add a non-idempotent step.
        if await running_execution(owner_id):
            return True
        await container_repo.mark_provisioning(owner_id)
        await asyncio.to_thread(
            _client().start_execution,
            stateMachineArn=settings.PROVISION_STATE_MACHINE_ARN,
            name=f"{_name_prefix(owner_id)}{uuid.uuid4().hex[:8]}",
            input=execution_input(owner_id),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)
        emit_provision_metric("dispatch_start_failed")
        # Nothing re-drives a lost dispatch any more (the status poll is gone): release the
        # row to a retryable error so the user sees Retry, never a "provisioning" row that no
        # execution owns. Never overwrites a running row.
        try:
            await container_repo.mark_provision_error(
                owner_id, last_error=f"provision could not start: {type(exc).__name__}", retryable=True
            )
        except Exception:  # noqa: BLE001
            logger.exception("dispatch_provision: could not record the failed dispatch for %s", owner_id)
        return False


async def dispatch_reconcile(owner_id: str) -> bool:
    """Start a durable reconcile execution for the owner. Never raises."""
    bind_log_context(user_id=owner_id)
    if not is_org_owner_id(owner_id):
        logger.error("dispatch_reconcile: %s is not a workspace (org) — refusing to reconcile", owner_id)
        emit_provision_metric("refused_non_org")
        return False
    try:
        await asyncio.to_thread(
            _client().start_execution,
            stateMachineArn=settings.PROVISION_STATE_MACHINE_ARN,
            name=f"reconcile-{owner_id}-{uuid.uuid4().hex[:8]}",
            input=json.dumps({"owner_id": owner_id, "mode": "reconcile"}),
        )
        return True
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_reconcile: dispatch failed for owner %s", owner_id)
        emit_provision_metric("dispatch_start_failed")
        return False
