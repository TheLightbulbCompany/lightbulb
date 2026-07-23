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


def execution_name(owner_id: str, claim_id: str) -> str:
    # <=80 chars, [0-9A-Za-z-_] only; owner ids (user_…/org_…) are in-alphabet.
    return f"provision-{owner_id}-{claim_id[:8]}"


def execution_input(owner_id: str, claim_id: str) -> str:
    # Byte-deterministic: idempotent StartExecution requires identical input.
    return json.dumps(
        {
            "attempt": 0,
            "claim_id": claim_id,
            # The backend image has no ENTRYPOINT/PATH venv setup — its deps
            # live in a uv-managed venv (the image's own CMD is `uv run …`),
            # so a bare `python` cannot import the app. `uv run` is mandatory.
            "command": [
                "uv",
                "run",
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
    # Re-bind correlation ids: like background_provision, this can run in a
    # detached task where the HTTP-middleware binds did not propagate.
    bind_log_context(user_id=owner_id)
    try:
        await _dispatch_inner(owner_id)
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)


async def _dispatch_inner(owner_id: str) -> None:
    row = await container_repo.get_by_owner_id(owner_id)

    # Branch A (design §3): a row with a self-consistent stamped execution
    # name, in a status the machine legitimately owns. That is "provisioning"
    # OR "error" — during in-machine retries the runner/SFN honestly writes
    # error + error_retrying, and treating that as fresh-dispatch would mint a
    # SECOND concurrent execution whose stale twin re-runs the AIP/role/
    # gate-token mint (a rotation — the duplicate-mint prod incident class).
    # Re-issue StartExecution with the SAME name+input:
    #   • RUNNING → idempotent success, same ARN (cross-instance single-flight)
    #   • never started (crash between claim and start) → starts now
    #   • CLOSED → ExecutionAlreadyExists → fall through to Branch B (the
    #     design's "execution CLOSED → mint fresh claim" leg; without it a
    #     "provisioning" row whose execution closed without a terminal row
    #     write is wedged forever). Branch B's claim conditions bound this:
    #     a fresh provisioning lease refuses, error/stale rows admit.
    if row and row.get("status") in ("provisioning", "error"):
        stamped = row.get("provision_execution_name")
        claim = row.get("provision_claim_id")
        if stamped and claim and stamped == execution_name(owner_id, claim):
            outcome = await _start(owner_id, claim)
            if outcome != "closed":
                # "ok" → done. "failed" → return WITHOUT touching the row:
                # the claim is not ours to release, and a later poll retries.
                return

    # Branch B: fresh dispatch — mint claim + name in one atomic write.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        emit_provision_metric("claim_lost")
        return
    outcome = await _start(owner_id, claim)
    if outcome == "ok":
        return
    # "failed": transient StartExecution error. "closed": an 8-hex name
    # collision on a FRESH claim — the name embeds claim_id[:8], so a closed
    # execution already holds it and this claim can never run. Either way,
    # release to error (retryable — the next poll mints a new claim and name)
    # with a cause naming which dispatch leg failed.
    last_error = (
        "provision dispatch: execution name collision"
        if outcome == "closed"
        else "provision dispatch: StartExecution failed"
    )
    try:
        await container_repo.release_provision_to_error(owner_id, claim, last_error=last_error, retryable=True)
    except Exception:  # noqa: BLE001 — release is best-effort
        logger.exception("dispatch_provision: release failed for owner %s", owner_id)
    emit_provision_metric("error")


async def _start(owner_id: str, claim_id: str) -> str:
    """StartExecution with the deterministic name+input for this claim.

    Returns one of:
      • "ok"     — started, or idempotently matched a RUNNING execution
      • "closed" — ExecutionAlreadyExists: a CLOSED execution holds the name
      • "failed" — transient StartExecution failure
    """
    name = execution_name(owner_id, claim_id)
    try:
        await asyncio.to_thread(
            _client().start_execution,
            stateMachineArn=settings.PROVISION_STATE_MACHINE_ARN,
            name=name,
            input=execution_input(owner_id, claim_id),
        )
        return "ok"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
            logger.info("dispatch_provision: execution %s already closed", name)
            return "closed"
        logger.exception("dispatch_provision: StartExecution failed for %s", name)
        return "failed"
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: StartExecution failed for %s", name)
        return "failed"
