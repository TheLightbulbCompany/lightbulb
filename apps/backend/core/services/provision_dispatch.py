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
    try:
        await _dispatch_inner(owner_id)
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)


async def _dispatch_inner(owner_id: str) -> None:
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
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
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
