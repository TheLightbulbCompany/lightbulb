"""SFN dispatch for container provisioning.

THE executor for ``background_provision`` — there is no flag and no inline
fallback (spec §7, PR-10: "no dual-run window"). Dispatch is crash-safe by
execution NAME: the claim write stamps the name atomically (container_repo),
and StartExecution is idempotent for STANDARD workflows on identical
name+input, so any later poll can safely re-issue the same call. See
docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §7 and
the (#1388) docs/superpowers/specs/2026-07-22-durable-provisioning-executor-design.md
§3 for the original crash-safety argument, which is unchanged by the reshape.

Two hazards #1388 left open, both closed here (spec §7 "Required reshape"):

1. Per-retry attempt fencing: the state machine's ``BumpAttempt`` state mints
   a FRESH ``attempt_token`` (via the ASL intrinsic ``States.UUID()``) on
   every retry while the execution NAME — and this row's ``claim_id`` — stay
   stable across the whole execution. ``scripts.provision_runner`` re-stamps
   the row's ``provision_attempt_token`` to that fresh value as its first
   action every attempt (``container_repo.claim_attempt``), fencing out a
   still-running straggler from a superseded attempt at its next checkpoint.
   See ``container_repo.claim_attempt`` docstring for the full hazard.
2. Branch-B wedge (execution CLOSED without a terminal row write): the
   design hands this to a ``reconciliation_worker`` (SWEEP) that
   disambiguates via ``DescribeExecution`` — NOT built in this PR (SWEEP is
   Wave 5). ``describe_dispatch_execution`` below is the clean query
   interface SWEEP will call; until it lands, the wall-clock stale-lease
   heuristic in ``container_repo.is_provision_claim_stale`` remains the
   interim bridge (see its docstring) — do not delete it as part of this PR.
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
    # Byte-deterministic: idempotent StartExecution requires identical input
    # on a re-issued Branch-A call, so nothing here may be freshly randomized
    # per call — that is exactly why attempt_token below is `claim_id`
    # itself (deterministic, already-fresh-per-claim) rather than a new
    # uuid4(): the ASL's BumpAttempt state is what mints a genuinely fresh
    # per-retry token, entirely inside the running execution, never touching
    # this input.
    return json.dumps(
        {
            "attempt": 0,
            "attempt_token": claim_id,
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


async def dispatch_provision(owner_id: str) -> str:
    """Start (or idempotently re-start) the provisioning state machine for
    this owner. Never raises — dispatch failure releases the claim to
    ``error`` so the status poll's normal retry re-dispatches.

    Returns the outcome so callers can report WHY nothing started:

      * ``"dispatched"``  — execution started, or idempotently re-affirmed an
        already-RUNNING one (both mean "this owner has an execution").
      * ``"claim_lost"``  — ``try_claim_provision`` refused: the row is
        ``running``, or another caller holds a fresh lease. Not an error —
        it is the single-flight working.
      * ``"error"``       — transient StartExecution failure or name
        collision; the row was released to ``error`` and a later trigger
        retries.

    Every pre-existing caller ignores the return value; it exists so the
    event triggers in ``user_provision`` can emit an honest
    ``provision.trigger{result}`` instead of reporting "dispatched" for a
    call that actually stood down.
    """
    # Re-bind correlation ids: like background_provision, this can run in a
    # detached task where the HTTP-middleware binds did not propagate.
    bind_log_context(user_id=owner_id)
    try:
        return await _dispatch_inner(owner_id)
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)
        return "error"


async def _dispatch_inner(owner_id: str) -> str:
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
                return "dispatched" if outcome == "ok" else "error"

    # Branch B: fresh dispatch — mint claim + name in one atomic write.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        emit_provision_metric("claim_lost")
        return "claim_lost"
    outcome = await _start(owner_id, claim)
    if outcome == "ok":
        return "dispatched"
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
    return "error"


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


async def describe_dispatch_execution(owner_id: str, claim_id: str) -> dict | None:
    """Return the SFN ``DescribeExecution`` response for this owner's stamped
    execution, or ``None`` if the row carries no execution name.

    # SWEEP: integration point for the reconciliation worker (spec §7,
    # Wave 5, not built in this PR). SWEEP's job on a stuck "provisioning"
    # row is to call this and disambiguate:
    #   - status RUNNING           → still legitimately in flight, leave it
    #   - status SUCCEEDED/FAILED/TIMED_OUT/ABORTED (a "CLOSED without a
    #     terminal row write" wedge — Branch B's target case) → call
    #     dispatch_provision(owner_id) again, which mints a fresh claim once
    #     Branch A's re-issue hits ExecutionAlreadyExists and falls through.
    # Deliberately just the read primitive — no polling loop, no retry
    # policy, no owner iteration here; that orchestration belongs to SWEEP
    # itself, not to the dispatcher.
    """
    row = await container_repo.get_by_owner_id(owner_id)
    stamped = (row or {}).get("provision_execution_name")
    if not stamped or (row or {}).get("provision_claim_id") != claim_id:
        return None
    execution_arn = (
        f"arn:aws:states:{settings.AWS_REGION}:{await _account_id()}:execution:{_state_machine_name()}:{stamped}"
    )
    try:
        return await asyncio.to_thread(_client().describe_execution, executionArn=execution_arn)
    except ClientError:
        logger.exception("describe_dispatch_execution: DescribeExecution failed for %s", stamped)
        return None


def _state_machine_name() -> str:
    # The state machine ARN's last path segment is its name; StartExecution
    # is called with the ARN, but DescribeExecution needs an execution ARN
    # built from name+account (see AWS's arn:aws:states:...:execution:name
    # shape) rather than a state-machine ARN.
    return settings.PROVISION_STATE_MACHINE_ARN.rsplit(":", 1)[-1]


async def _account_id() -> str:
    # Parsed out of the state machine ARN (arn:aws:states:<region>:<account>:
    # stateMachine:<name>) rather than an STS call — cheap and avoids a new
    # IAM grant for a helper that (as of this PR) SWEEP has not yet wired up.
    parts = settings.PROVISION_STATE_MACHINE_ARN.split(":")
    return parts[4] if len(parts) > 4 else ""
