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
    return json.dumps(
        {
            "attempt": 0,
            # The backend image has no ENTRYPOINT/PATH venv setup — its deps
            # live in a uv-managed venv (the image's own CMD is `uv run …`),
            # so a bare `python` cannot import the app. `uv run` is mandatory.
            # --no-sync: the venv was baked with `uv sync --frozen --no-dev`
            # at image build; a bare `uv run` re-syncs the environment WITH
            # the dev-dependency group at every provisioner task start.
            "command": ["uv", "run", "--no-sync", "python", "-m", "scripts.provision_runner", "--owner", owner_id],
            "owner_id": owner_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


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
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller; the next poll retries
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)
        emit_provision_metric("dispatch_start_failed")
        return False
