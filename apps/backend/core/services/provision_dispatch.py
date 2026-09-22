"""Container-provision dispatch: SFN watchdog owns custody, backend submits.

THE single provisioning funnel. Every path through this module does the same
thing: make sure a state-machine execution is watching the owner's claim,
then return. Attempt work runs ONLY on the machine (the ECS provisioner
task); the backend never runs the runner itself.

The inline child-process fast path is gone (Phase 1 of the AgentCore-era
simplification, see
docs/superpowers/specs/2026-09-22-provisioning-simplification-design.md): it
skipped ~30s of ECS cold-start per signup at the cost of a 4-slot semaphore,
a dual attempt-token fence, stand-down paths, a shutdown handoff, and a timing
ladder coupled to the watchdog's backstop. With typical provisions at ~1
minute, that trade no longer pays. What remains:

- **Branch A**: a ``provisioning``/``error`` row with a self-consistent
  stamped execution name gets its ``StartExecution`` re-issued (idempotent on
  name+input while RUNNING; ``ExecutionAlreadyExists`` when CLOSED falls
  through to a fresh claim). Never mints a second claim for a row that
  already names one.
- **Branch B**: fresh dispatch — mint claim + name in one atomic write,
  START the execution. If the start fails, release the claim to ``error``
  (retryable) so the next poll retries with a fresh claim. No unwatched
  attempts: with nothing running the attempt, an idle leased row is the
  worse outcome, and an ``error`` row is exactly what the poll's normal
  retry re-dispatches.

Dispatch stays crash-safe by execution NAME: the claim write stamps the name
atomically (container_repo), and StartExecution is idempotent for STANDARD
workflows on identical name+input, so any later poll can safely re-issue the
same call. See
docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §7.

Return contract: ``True`` when an execution owns the work (started now or
already RUNNING), ``False`` when nothing does — claim lost, start failed, or
non-org refusal. Callers with a recovery marker (notably the trial-conversion
reprovision) must retain it on ``False``. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import boto3
from botocore.exceptions import ClientError

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
            # --no-sync: the venv was baked with `uv sync --frozen --no-dev`
            # at image build; a bare `uv run` re-syncs the environment WITH
            # the dev-dependency group — downloading pytest/moto/etc. at every
            # provisioner task start.
            "command": [
                "uv",
                "run",
                "--no-sync",
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


async def dispatch_provision(owner_id: str) -> bool:
    """Start or re-start provisioning. Return whether an execution owns the
    work. Never raises; a caller with a recovery marker must retain it when
    dispatch stands down (returns False)."""
    # Re-bind correlation ids: dispatched via create_task, this can run in a
    # detached task where the HTTP-middleware binds did not propagate.
    bind_log_context(user_id=owner_id)
    if not is_org_owner_id(owner_id):
        # Personal workspaces no longer exist. Refuse here — the one funnel every
        # door goes through — before a containers row is claimed or an SFN
        # execution started (an admin reprovision whose Clerk lookup failed open
        # hands this a user_ id). provision_runner step 0 refuses too, as a backstop.
        logger.error("dispatch_provision: %s is not a workspace (org) — refusing to provision", owner_id)
        emit_provision_metric("refused_non_org")
        return False
    try:
        return await _dispatch_inner(owner_id)
    except Exception:  # noqa: BLE001 — dispatch must never take down the caller
        logger.exception("dispatch_provision: dispatch failed for owner %s", owner_id)
        return False


async def _dispatch_inner(owner_id: str) -> bool:
    row = await container_repo.get_by_owner_id(owner_id)

    # Branch A (design §3): a row with a self-consistent stamped execution
    # name, in a status the machine legitimately owns. That is "provisioning"
    # OR "error" — during in-machine retries the runner/SFN honestly writes
    # error + error_retrying, and treating that as fresh-dispatch would mint a
    # SECOND concurrent execution whose stale twin re-runs the credential
    # mints (a rotation — the duplicate-mint prod incident class).
    # Re-issue StartExecution with the SAME name+input:
    #   • RUNNING → idempotent success, same ARN (cross-instance single-flight)
    #   • never started (crash between claim and start) → starts now
    #   • CLOSED → ExecutionAlreadyExists → fall through to Branch B (a fresh
    #     claim; without it a "provisioning" row whose execution closed
    #     without a terminal row write is wedged forever). Branch B's claim
    #     conditions bound this: a fresh provisioning lease refuses,
    #     error/stale rows admit.
    if row and row.get("status") in ("provisioning", "error"):
        stamped = row.get("provision_execution_name")
        claim = row.get("provision_claim_id")
        if stamped and claim and stamped == execution_name(owner_id, claim):
            outcome = await _start(owner_id, claim)
            if outcome != "closed":
                # "ok" → an execution owns it. "failed" → return WITHOUT
                # touching the row: the claim is not ours to release, and a
                # later poll retries.
                return outcome == "ok"

    # Branch B: fresh dispatch — mint claim + name in one atomic write, START
    # the watchdog. The order carries the module's central invariant: the
    # execution exists before this function reports ownership, so there is no
    # window with a leased row and nothing watching it.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        emit_provision_metric("claim_lost")
        return False
    outcome = await _start(owner_id, claim)
    if outcome != "ok":
        # No watchdog for this claim: Step Functions is unreachable ("failed"),
        # or a finished execution already holds this exact name ("closed" —
        # the name embeds claim_id[:8], so this is an 8-hex collision).
        # Release to error (retryable): the next poll retries with a fresh
        # claim, and an error row is exactly what the status poll's normal
        # retry re-dispatches. Fenced on the claim-time token — no runner ever
        # ran, so nothing could have re-stamped it.
        emit_provision_metric("dispatch_start_failed")
        logger.error(
            "dispatch_provision: no watchdog for owner %s (start outcome=%s) — releasing claim to error",
            owner_id,
            outcome,
        )
        try:
            await container_repo.release_provision_to_error(
                owner_id,
                claim,
                last_error=f"provision dispatch could not start watchdog (outcome={outcome})",
                retryable=True,
                attempt_token=claim,
            )
        except Exception:  # noqa: BLE001 — release is best-effort; the stale-lease path still covers the row
            logger.exception("dispatch_provision: start-failure release failed for owner %s", owner_id)
        return False
    return True


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

    Read primitive for an operator (or a future reconciler) that needs to ask
    what actually happened to a given claim's execution:
      - status RUNNING           → still legitimately in flight, leave it
      - status SUCCEEDED/FAILED/TIMED_OUT/ABORTED (a "CLOSED without a
        terminal row write" wedge) → call dispatch_provision(owner_id) again.
    Deliberately just the read primitive — no polling loop, no retry policy,
    no owner iteration here.
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
    # IAM grant for a helper that SWEEP has not yet wired up.
    parts = settings.PROVISION_STATE_MACHINE_ARN.split(":")
    return parts[4] if len(parts) > 4 else ""
