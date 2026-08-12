"""Container-provision dispatch: inline fast path, SFN durability backstop.

THE single provisioning funnel. A fresh claim runs the provision runner as
attempt 0 in a CHILD PROCESS of the backend task (``_run_inline`` — same
image, same task role, same env, same exit-code ABI as the SFN provisioner
task), skipping the ~30s ECS cold-launch the state machine pays per
attempt. A subprocess — not an in-loop coroutine, not a thread — because
the runner was designed as a standalone process: its call tree contains
synchronous network/EFS I/O that would stall the backend's event loop, and
it touches module-global asyncio primitives (the Paperclip admin-session
lock, the org-seed locks) that must not be awaited from a second event
loop in the same process. Process isolation removes both classes outright.
The state machine remains the durable owner of everything after attempt 0:

- **Inline failure** (exit 10/11, or a spawn crash): the runner has already
  released the row to ``error`` with the stamped claim + execution name
  intact, so the next status poll lands on Branch A below and hands the
  SAME claim to the state machine — retries stay SFN-owned, paced by the
  machine's own Wait states, never by the poll.
- **Backend shutdown** (deploy roll): the lifespan hook calls
  ``handoff_inflight_provisions`` — terminate the child, await it, then
  StartExecution with the deterministic name+input. Crash-without-SIGTERM
  is the pre-existing Branch A "never started" leg, reached from the next
  ~3s status poll on a surviving instance.
- **Overlap** (another instance's Branch A re-issue racing a still-live
  attempt during a rolling deploy): the inline attempt runs under a
  DISTINCT attempt token (``inline_attempt_token``, via the same
  ``PROVISION_ATTEMPT_TOKEN`` env contract the state machine uses), so the
  SFN runner's first-act ``claim_attempt`` re-stamp fences the inline
  straggler out at its next fenced checkpoint — the same hazard-1 fence
  that already orders SFN retries (no dual-run window, spec §7).

Dispatch stays crash-safe by execution NAME: the claim write stamps the name
atomically (container_repo), and StartExecution is idempotent for STANDARD
workflows on identical name+input, so any later poll can safely re-issue the
same call. See
docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §7.

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
   disambiguates via ``DescribeExecution`` — NOT yet built (SWEEP is
   Wave 5). ``describe_dispatch_execution`` below is the clean query
   interface SWEEP will call; until it lands, the wall-clock stale-lease
   heuristic in ``container_repo.is_provision_claim_stale`` remains the
   interim bridge (see its docstring) — do not delete it until SWEEP lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
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


async def dispatch_provision(owner_id: str) -> None:
    """Start (or idempotently re-start) the provisioning state machine for
    this owner. Never raises — dispatch failure releases the claim to
    ``error`` so the status poll's normal retry re-dispatches."""
    # Re-bind correlation ids: dispatched via create_task, this can run in a
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
            if _inline_attempt_live(owner_id, claim):
                # This process's inline attempt holds the lease; the ~3s poll
                # cadence must not hand its claim to the state machine while
                # it runs.
                return
            outcome = await _start(owner_id, claim)
            if outcome != "closed":
                # "ok" → done. "failed" → return WITHOUT touching the row:
                # the claim is not ours to release, and a later poll retries.
                return

    # Branch B: fresh dispatch — mint claim + name in one atomic write, then
    # run attempt 0 in-process. No StartExecution here: the machine only ever
    # sees a claim via Branch A (inline failure, shutdown handoff, or another
    # instance's re-issue), always under the same deterministic name+input.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        emit_provision_metric("claim_lost")
        return
    _spawn_inline(owner_id, claim)


# Live inline attempts, owner_id -> (claim_id, task). The dict holds the strong
# reference that keeps each attempt task alive; entries are popped by the
# attempt's own finally.
_INLINE_ATTEMPTS: dict[str, tuple[str, asyncio.Task]] = {}

# ponytail: fixed cap of 4 concurrent runner subprocesses (~250MB RSS each
# against the backend task's memory) — the 5th signup queues behind the
# semaphore; raise alongside the task size if signup bursts ever exceed it.
_INLINE_CONCURRENCY = asyncio.Semaphore(4)

# The state machine bounds every attempt with taskTimeout (30 min,
# service-stack.ts RunProvisioner). The runner's wait loop is deliberately
# unbounded and relies on that outer bound, so attempt 0 needs the SAME
# per-attempt ceiling — without it, a wedged box (deployment never FAILED,
# gateway never healthy) would hold its semaphore slot and suppress the SFN
# handoff forever. On expiry the child is terminated and the claim handed to
# the machine.
_INLINE_ATTEMPT_TIMEOUT_S = 30 * 60


def inline_attempt_token(claim_id: str) -> str:
    # DISTINCT from the SFN's attempt-0 token (claim_id itself) so an SFN
    # takeover of the same claim fences an inline straggler at its next
    # checkpoint via claim_attempt's re-stamp — see the module docstring.
    return f"{claim_id}-inline"


def _inline_attempt_live(owner_id: str, claim_id: str) -> bool:
    entry = _INLINE_ATTEMPTS.get(owner_id)
    return bool(entry and entry[0] == claim_id and not entry[1].done())


def _spawn_inline(owner_id: str, claim_id: str) -> None:
    task = asyncio.create_task(_run_inline(owner_id, claim_id))
    _INLINE_ATTEMPTS[owner_id] = (claim_id, task)


async def _runner_subprocess(owner_id: str, claim_id: str) -> int:
    """Run the provision runner as a child process; return its ABI exit code.

    ``sys.executable`` is the backend's own venv interpreter (the image CMD is
    ``uv run uvicorn …``), so the child imports the app without the ``uv``
    wrapper layer — a direct child, so terminate() reaches the runner itself.
    On cancellation (shutdown handoff): terminate, bounded-wait, kill — no
    straggler outlives its StartExecution successor. A SIGTERM-killed runner
    is the same shape as a Spot-reclaimed provisioner task, which every row
    write already tolerates (condition-fenced; stale-lease recovery).
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.provision_runner",
        "--owner",
        owner_id,
        "--claim",
        claim_id,
        env={**os.environ, "PROVISION_ATTEMPT_TOKEN": inline_attempt_token(claim_id)},
    )
    try:
        return await proc.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        raise


async def _run_inline(owner_id: str, claim_id: str) -> None:
    bind_log_context(user_id=owner_id)
    try:
        async with _INLINE_CONCURRENCY:
            try:
                # The timeout wraps only the attempt itself, never the
                # semaphore queue wait — a queued signup does not burn budget.
                code = await asyncio.wait_for(_runner_subprocess(owner_id, claim_id), timeout=_INLINE_ATTEMPT_TIMEOUT_S)
            except TimeoutError:
                # wait_for has cancelled the attempt (terminating the child)
                # — hand the claim to the state machine, exactly like the
                # shutdown handoff.
                outcome = await _start(owner_id, claim_id)
                emit_provision_metric("inline_timeout")
                logger.error(
                    "inline provision attempt for owner %s exceeded %ss — handed to SFN (outcome=%s)",
                    owner_id,
                    _INLINE_ATTEMPT_TIMEOUT_S,
                    outcome,
                )
                return
        if code == 0:
            emit_provision_metric("inline_ok")
        else:
            # 10/11/20: the runner already wrote the honest row state (error
            # with retryability, or verified stand-down). The stamped
            # claim/name survive release_provision_to_error, so the next poll
            # lands on Branch A and hands this claim to the state machine.
            emit_provision_metric("inline_failed")
            logger.warning(
                "inline provision attempt for owner %s exited %s — SFN takes over on next poll",
                owner_id,
                code,
            )
    except asyncio.CancelledError:
        raise  # shutdown handoff owns the successor dispatch
    except Exception:  # noqa: BLE001 — the runner classifies internally; this is the spawn belt
        logger.exception("inline provision attempt crashed for owner %s", owner_id)
        emit_provision_metric("inline_failed")
        try:
            # Fenced on the inline attempt token: if the state machine has
            # already taken this claim over (its runner re-stamped the token),
            # this release silently no-ops instead of flipping the SFN-owned
            # row back to error mid-attempt.
            await container_repo.release_provision_to_error(
                owner_id,
                claim_id,
                last_error="inline provision attempt crashed",
                retryable=True,
                attempt_token=inline_attempt_token(claim_id),
            )
        except Exception:  # noqa: BLE001 — release is best-effort
            logger.exception("inline provision: release failed for owner %s", owner_id)
    finally:
        entry = _INLINE_ATTEMPTS.get(owner_id)
        if entry and entry[0] == claim_id:
            _INLINE_ATTEMPTS.pop(owner_id, None)


async def handoff_inflight_provisions() -> None:
    """Shutdown hook (main.py lifespan): hand every live inline attempt to the
    state machine before the process exits. Cancel first and AWAIT it — the
    cancellation path terminates the child process — so no straggler outlives
    its StartExecution successor. If StartExecution itself fails, the row
    still holds the stamped claim+name and the next poll's Branch A re-issue
    self-heals."""
    for owner_id, (claim_id, task) in list(_INLINE_ATTEMPTS.items()):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — the attempt is dead either way
                logger.exception("inline provision handoff: attempt for owner %s errored on cancel", owner_id)
        outcome = await _start(owner_id, claim_id)
        emit_provision_metric("inline_handoff")
        logger.info(
            "inline provision handoff for owner %s: StartExecution outcome=%s",
            owner_id,
            outcome,
        )


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
    # Wave 5, not yet built). SWEEP's job on a stuck "provisioning"
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
    # IAM grant for a helper that SWEEP has not yet wired up.
    parts = settings.PROVISION_STATE_MACHINE_ARN.split(":")
    return parts[4] if len(parts) > 4 else ""
