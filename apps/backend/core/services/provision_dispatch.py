"""Container-provision dispatch: SFN watchdog owns custody, inline fast path.

THE single provisioning funnel. A fresh claim (Branch B) does three things in
order: claim the row, START the state-machine execution, then run the
provision runner as attempt 0 in a CHILD PROCESS of the backend task
(``_run_inline`` — same image, same task role, same env, same exit-code ABI
as the SFN provisioner task), skipping the ~30s ECS cold-launch the machine
pays per attempt. A subprocess — not an in-loop coroutine, not a thread —
because the runner was designed as a standalone process: its call tree
contains synchronous network/EFS I/O that would stall the backend's event
loop, and it touches module-global asyncio primitives (the Paperclip
admin-session lock, the org-seed locks) that must not be awaited from a
second event loop in the same process. Process isolation removes both
classes outright.

**Start-before-spawn is the invariant.** From the instant a claim exists,
exactly ONE durable thing is responsible for driving it to a terminal state:
the execution. Its watch loop (``service-stack.ts``) polls the row and takes
the ECS leg over on its own clock, so the inline attempt is an opportunistic
accelerator, never a custodian. Nothing in this module hands custody back —
there is nothing to hand back:

- **Inline failure** (exit 10/11, or a spawn crash): the runner has already
  released the row to ``error``; the watch loop observes it within a tick
  and runs the ECS leg. Retries stay SFN-owned, paced by the machine's own
  Wait states.
- **Backend shutdown** (deploy roll) and **attempt timeout**: the child is
  terminated, then ``_stand_down_attempt`` assures the watchdog and releases
  the row to ``error`` — so recovery costs one watch tick, not the 40-tick
  backstop. **Crash without SIGTERM** reaches no code at all; that one is
  the backstop's.
- **Queue saturation**: the inline slot wait is BOUNDED
  (``_INLINE_QUEUE_TIMEOUT_S``) and releases the row to ``error`` on expiry,
  so a pileup of wedged attempts can never park a claim behind them.
- **Overlap** (an SFN attempt racing a still-live inline child): the inline
  attempt runs under a DISTINCT attempt token (``inline_attempt_token``, via
  the same ``PROVISION_ATTEMPT_TOKEN`` env contract the state machine uses),
  so the SFN runner's first-act ``claim_attempt`` re-stamp fences the inline
  straggler out at its next fenced checkpoint — the same hazard-1 fence that
  already orders SFN retries (no dual-run window, spec §7).
- **No watchdog available** (StartExecution fails, or a closed execution
  already holds the name): the attempt still runs, UNWATCHED. When no durable
  owner can be had, an unwatched attempt beats an idle claim — it makes
  progress and leaves the row honest however it ends, whereas declining to
  spawn would strand a leased row with nothing running. A degrade to the
  pre-watchdog behaviour, visible as ``dispatch_start_failed``, and undone by
  the next poll's Branch A re-issue once SFN is reachable.

This replaced a choreography of per-exit-path StartExecution handoffs whose
common leg (nonzero exit) had no handoff at all: it relied on the FRONTEND's
~3s status poll to re-dispatch, and a saturated inline queue suppressed even
that. With no tab open — or with every slot wedged — a claim was simply
orphaned. Dev broke fleet-wide this way on 2026-08-13 during a Fargate Spot
drought.

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
2. Execution CLOSED without a terminal row write: the watch loop closes this
   for every claim it owns — it only exits via Succeed (row ``running`` /
   superseded / claim gone), Fail (deterministic error), or the ECS retry
   leg, so a claim cannot outlive its watcher silently. What remains
   uncovered is the narrow window BEFORE the watchdog exists: a backend that
   dies between ``try_claim_provision`` and ``_start``. That row holds a
   lease with no execution, and the wall-clock stale-lease heuristic in
   ``container_repo.is_provision_claim_stale`` is still its recovery — do not
   delete that heuristic. ``describe_dispatch_execution`` below stays as the
   read primitive for an operator (or a future reconciler) that needs to ask
   what actually happened to a given claim's execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass

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
                # This process spawned that claim, so its execution is already
                # RUNNING (Branch B starts before spawning). Re-issuing would
                # be an idempotent no-op — skip the redundant API call rather
                # than make one per owner per ~3s poll.
                return
            outcome = await _start(owner_id, claim)
            if outcome != "closed":
                # "ok" → done. "failed" → return WITHOUT touching the row:
                # the claim is not ours to release, and a later poll retries.
                return

    # Branch B: fresh dispatch — mint claim + name in one atomic write, START
    # the watchdog, then run attempt 0 in-process. The order carries the
    # module's central invariant: whenever a watchdog CAN be had, it is
    # watching before the attempt exists, so no inline outcome needs a
    # handoff. The one case where it cannot (below) degrades explicitly
    # rather than silently.
    claim = str(uuid.uuid4())
    name = execution_name(owner_id, claim)
    won = await container_repo.try_claim_provision(owner_id, claim_id=claim, execution_name=name)
    if won is None:
        logger.info("dispatch_provision: claim lost for owner %s — standing down", owner_id)
        emit_provision_metric("claim_lost")
        return
    outcome = await _start(owner_id, claim)
    if outcome != "ok":
        # No watchdog for this claim: either Step Functions is unreachable, or
        # ("closed") a finished execution already holds this name — the name
        # embeds claim_id[:8], so an 8-hex collision means this claim can
        # never be watched.
        #
        # Run the attempt anyway. When no durable owner can be had, an
        # UNWATCHED attempt is strictly better than an idle claim: it makes
        # real progress, and every way it ends leaves the row honest — success
        # writes running (no watcher needed), a runner failure releases to
        # error, a crash hits the release below. Refusing to spawn would leave
        # a leased row with nothing running at all, recoverable only by a
        # later request path. This is a deliberate degrade to the pre-watchdog
        # behaviour for as long as SFN is down; the next poll's Branch A
        # re-issue attaches a watchdog as soon as it comes back (and on
        # "closed", Branch A falls through to a fresh claim + name).
        emit_provision_metric("dispatch_start_failed")
        logger.error(
            "dispatch_provision: no watchdog for owner %s (start outcome=%s) — "
            "running attempt 0 unwatched; recovery falls back to the status poll",
            owner_id,
            outcome,
        )
    _spawn_inline(owner_id, claim)


# Live inline attempts, owner_id -> (claim_id, task). The dict holds the strong
# reference that keeps each attempt task alive; entries are popped by the
# attempt's own finally.
@dataclass
class _InlineAttempt:
    """One live inline attempt.

    ``fence_token`` is the ``provision_attempt_token`` value currently ON THE
    ROW for this attempt, and it changes once: from the claim-time token
    (``claim_id``, stamped by ``try_claim_provision``) to the inline token,
    when the attempt acquires a slot and stamps it. Tracking it is load-bearing
    for ``_stand_down_attempt``: a release fenced on the WRONG token silently
    no-ops, leaving the row at ``provisioning`` to wait out the 40-tick
    backstop. The claim-time token cannot simply be used for both, because an
    SFN attempt-0 runner also carries ``claim_id`` (``execution_input``) while
    it writes ``status="provisioning"`` — so releasing on ``claim_id`` after
    the inline attempt is under way could clobber a live machine-owned run.
    """

    claim_id: str
    task: asyncio.Task
    fence_token: str


_INLINE_ATTEMPTS: dict[str, _InlineAttempt] = {}

# ponytail: fixed cap of 4 concurrent runner subprocesses (~250MB RSS each
# against the backend task's memory) — the 5th signup queues behind the
# semaphore; raise alongside the task size if signup bursts ever exceed it.
_INLINE_CONCURRENCY = asyncio.Semaphore(4)

# TIMING IS LOAD-BEARING — these two and the machine's watch backstop (40
# ticks x 60s = 2400s, service-stack.ts WatchRow) move together. The backstop
# may only fire once the inline child is DEAD: 300 + 1800 = 2100s < 2400s. If
# an SFN attempt started while an inline child were still alive, its
# first-act claim_attempt re-stamp could be stomped by the inline child's own
# re-stamp and BOTH would keep running against the same row.
#
# _INLINE_QUEUE_TIMEOUT_S bounds the wait for a concurrency slot. Unbounded,
# a pileup of wedged attempts parks later claims indefinitely while Branch A
# stands down for them on every poll — the machine never takes over (the
# 2026-08-13 Spot-drought failure).
#
# _INLINE_ATTEMPT_TIMEOUT_S mirrors the machine's per-attempt taskTimeout (30
# min, service-stack.ts RunProvisioner). The runner's wait loop is
# deliberately unbounded and relies on an outer bound, so attempt 0 needs the
# same ceiling.
_INLINE_QUEUE_TIMEOUT_S = 300
_INLINE_ATTEMPT_TIMEOUT_S = 30 * 60


def inline_attempt_token(claim_id: str) -> str:
    # DISTINCT from the SFN's attempt-0 token (claim_id itself) so an SFN
    # takeover of the same claim fences an inline straggler at its next
    # checkpoint via claim_attempt's re-stamp — see the module docstring.
    return f"{claim_id}-inline"


def _inline_attempt_live(owner_id: str, claim_id: str) -> bool:
    entry = _INLINE_ATTEMPTS.get(owner_id)
    return bool(entry and entry.claim_id == claim_id and not entry.task.done())


def _spawn_inline(owner_id: str, claim_id: str) -> None:
    # fence_token starts at the CLAIM-TIME token: try_claim_provision stamps
    # provision_attempt_token = claim_id, and nothing has changed it yet.
    entry = _InlineAttempt(claim_id=claim_id, task=None, fence_token=claim_id)  # type: ignore[arg-type]
    entry.task = asyncio.create_task(_run_inline(owner_id, claim_id))
    _INLINE_ATTEMPTS[owner_id] = entry


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
        try:
            await asyncio.wait_for(_INLINE_CONCURRENCY.acquire(), timeout=_INLINE_QUEUE_TIMEOUT_S)
        except TimeoutError:
            # No slot in time (a Spot-drought pileup of wedged attempts).
            # Make the row honest so the watchdog's next tick runs the
            # attempt as an ECS task instead of this owner queueing behind
            # wedged siblings. Fenced on the claim-time attempt token —
            # try_claim_provision stamps provision_attempt_token = claim_id,
            # and the child that would have re-stamped it never spawned — so
            # a machine retry that already re-stamped owns the row and this
            # release silently no-ops.
            emit_provision_metric("inline_queue_saturated")
            logger.warning(
                "inline provision for owner %s waited %ss for a slot — releasing to the machine",
                owner_id,
                _INLINE_QUEUE_TIMEOUT_S,
            )
            try:
                await container_repo.release_provision_to_error(
                    owner_id,
                    claim_id,
                    last_error="inline queue saturated — machine takes over",
                    retryable=True,
                    attempt_token=claim_id,
                )
            except Exception:  # noqa: BLE001 — release is best-effort
                logger.exception("inline provision: queue-saturation release failed for owner %s", owner_id)
            return
        try:
            # Stamp the inline fencing token BEFORE the child exists. The
            # child re-stamps the same value as its first action anyway (a
            # documented no-op re-assertion), but doing it here removes the
            # window where the attempt is under way while the row still
            # carries the claim-time token — a window in which a stand-down
            # could not tell which token to fence on. Also refreshes
            # provision_claimed_at, so a long queue wait never looks stale.
            try:
                await container_repo.claim_attempt(owner_id, claim_id, inline_attempt_token(claim_id))
            except container_repo.ProvisionSuperseded:
                # The claim was taken over while we queued. Not ours any more:
                # do not run, do not write.
                logger.info(
                    "inline provision for owner %s: claim taken over before the attempt started — standing down",
                    owner_id,
                )
                return
            entry = _INLINE_ATTEMPTS.get(owner_id)
            if entry is not None and entry.claim_id == claim_id:
                entry.fence_token = inline_attempt_token(claim_id)
            # The timeout wraps only the attempt itself, never the queue wait
            # above — a queued signup does not burn its attempt budget.
            code = await asyncio.wait_for(_runner_subprocess(owner_id, claim_id), timeout=_INLINE_ATTEMPT_TIMEOUT_S)
        except TimeoutError:
            # wait_for has cancelled the attempt, and that cancellation path
            # terminated the child and awaited it — so by the time we are
            # here, the runner is confirmed dead and the row has no worker.
            emit_provision_metric("inline_timeout")
            logger.error(
                "inline provision attempt for owner %s exceeded %ss — terminated",
                owner_id,
                _INLINE_ATTEMPT_TIMEOUT_S,
            )
            # By here the slot was acquired and the inline token stamped, so
            # that is the token on the row.
            await _stand_down_attempt(
                owner_id,
                claim_id,
                "inline provision attempt timed out",
                inline_attempt_token(claim_id),
            )
            return
        finally:
            _INLINE_CONCURRENCY.release()
        if code == 0:
            emit_provision_metric("inline_ok")
        else:
            # 10/11/20: the runner already wrote the honest row state (error
            # with retryability, or verified stand-down). The watchdog reads
            # that row on its next tick and retries, fails, or stands down —
            # no dispatcher action, and no dependency on a client poll.
            emit_provision_metric("inline_failed")
            logger.warning(
                "inline provision attempt for owner %s exited %s — machine observes within a tick",
                owner_id,
                code,
            )
    except asyncio.CancelledError:
        raise  # shutdown: the child is terminated; the machine is already watching
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
        if entry and entry.claim_id == claim_id:
            _INLINE_ATTEMPTS.pop(owner_id, None)


async def _stand_down_attempt(owner_id: str, claim_id: str, cause: str, fence_token: str) -> None:
    """Leave a killed inline attempt's row in a state something will act on.

    Called from the two paths that terminate a running child (attempt timeout,
    shutdown). Both used to just return, which was wrong in two ways:

    * If Branch B degraded to unwatched (StartExecution failed), the row was
      left leased with no execution AND no runner — an orphan.
    * Even when watched, a leased ``provisioning`` row shows the watchdog no
      progress, so recovery waited for the 40-tick backstop. After a deploy
      roll that is ~40 minutes of a stuck provision.

    So: make sure a watchdog exists, then make the row honest.

    ``_start`` is idempotent on the deterministic name+input, so the normal
    case (execution already RUNNING) is a no-op, and the degraded case
    genuinely acquires the watchdog Branch B could not get.

    ``fence_token`` MUST be the value actually on the row for this attempt
    (``_InlineAttempt.fence_token``), which is the claim-time token before the
    attempt gets a slot and the inline token after. Passing the inline token
    unconditionally silently no-ops the release for an attempt cancelled while
    still queued — the row stays ``provisioning`` and waits out the 40-tick
    backstop. Passing the claim-time token unconditionally is worse: an SFN
    attempt-0 runner carries ``claim_id`` too, so it could clobber a live
    machine-owned run. With the right token, a machine takeover (whose runner
    re-stamped) makes this a silent no-op, which is exactly what we want.

    ``retryable`` is True — a killed attempt is transient by definition —
    which the status endpoint treats as silently-retried rather than surfacing
    to the owner, and which the watch loop turns into the ECS leg on its very
    next tick.
    """
    outcome = await _start(owner_id, claim_id)
    if outcome != "ok":
        logger.error(
            "inline stand-down for owner %s: no watchdog (start outcome=%s) — recovery falls back to the status poll",
            owner_id,
            outcome,
        )
    try:
        await container_repo.release_provision_to_error(
            owner_id,
            claim_id,
            last_error=cause,
            retryable=True,
            attempt_token=fence_token,
        )
    except Exception:  # noqa: BLE001 — release is best-effort; the watchdog's backstop still covers it
        logger.exception("inline stand-down: release failed for owner %s", owner_id)


async def handoff_inflight_provisions() -> None:
    """Shutdown hook (main.py lifespan): terminate every live inline child
    before the process exits. Cancel and AWAIT — the cancellation path
    terminates the child process — so no straggler outlives this instance.

    Then stand each killed attempt down (see ``_stand_down_attempt``): assure
    the watchdog and make the row honest, so a deploy roll costs the owner one
    watch tick rather than the 40-tick backstop. Crash-without-SIGTERM cannot
    reach this hook at all; that case is the backstop's."""
    for owner_id, entry in list(_INLINE_ATTEMPTS.items()):
        if entry.task.done():
            continue
        entry.task.cancel()
        try:
            await entry.task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — the attempt is dead either way
            logger.exception("inline provision handoff: attempt for owner %s errored on cancel", owner_id)
        emit_provision_metric("inline_handoff")
        logger.info("inline provision for owner %s terminated for shutdown", owner_id)
        # Read fence_token AFTER the cancel: an attempt that was still queued
        # never stamped the inline token, and one that had a slot did.
        await _stand_down_attempt(
            owner_id,
            entry.claim_id,
            "backend shutdown during inline provision attempt",
            entry.fence_token,
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
