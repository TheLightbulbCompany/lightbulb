"""One-shot container-provision runner, executed as an ECS task by the
isol8-{env}-provision Step Functions state machine (never by the backend).

Exit codes are the machine's ABI (spec §7 / design doc §3): 0 provisioned+
healthy, 10 deterministic failure (no retry), 11 transient failure (machine
retries), 20 superseded by a deliberate reprovision (machine stops quietly).

The runner owns the provisioning -> running transition in the FOREGROUND
(wait_for_running) — the detached create_task pollers ecs_manager fires
internally for non-provisioning paths (wake, resize) never run inside this
process; the foreground wait is the one that matters here, and every row
write is condition-guarded (claim + generation + per-attempt token).

PROVISION_ATTEMPT_TOKEN (env, set by the state machine's containerOverrides
from ``$.attempt_token``) is this ATTEMPT's fencing token — fresh every SFN
retry (``BumpAttempt`` mints one via ``States.UUID()``), while ``--claim``
and the execution name stay stable across the whole execution. The runner's
first action is always to (re-)stamp it via ``container_repo.claim_attempt``,
which is what fences a still-running straggler from a superseded attempt out
at its next checkpoint — see that function's docstring for the full hazard
this closes (spec §7 hazard 1). Falls back to ``--claim`` itself when unset
(a bare/local invocation outside the state machine — attempt-0-equivalent).

``provision_generation`` (the container-incarnation fence, INV-2) is read
directly off the containers row at startup rather than threaded through the
state machine's input/command line: StartExecution's Branch-A idempotency
(core.services.provision_dispatch) requires the dispatch INPUT to stay
byte-identical across re-issues for the SAME claim, but generation can be
MINTED mid-execution (by create_user_service, inside THIS run) — embedding
it in the deterministic input would make a later Branch-A re-issue (a
concurrent status-poll re-affirming the same in-flight execution) recompute
a different input, misclassify the still-RUNNING execution as "closed", and
dispatch a duplicate. Reading it fresh off the row side-steps that.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
    from core.repositories import container_repo
    from core.repositories.container_repo import ProvisionSuperseded, classify_provision_error

    bind_log_context(user_id=owner_id)
    manager = _get_manager()
    # Attempt-token fencing (hazard 1, spec §7): every attempt -- including
    # attempt 0, where this is a same-value no-op re-assertion -- re-stamps
    # the row's provision_attempt_token BEFORE any other checkpoint. See
    # container_repo.claim_attempt.
    attempt_token = os.environ.get("PROVISION_ATTEMPT_TOKEN") or claim_id
    try:
        await container_repo.claim_attempt(owner_id, claim_id, attempt_token)
        # Read the container-incarnation fence off the row (see module
        # docstring for why this is NOT threaded through the SFN input).
        # Absent for a brand-new owner -- create_user_service mints it later,
        # inside provision_user_container below.
        pre_row = await container_repo.get_by_owner_id(owner_id)
        generation = (pre_row or {}).get("provision_generation")

        provider_choice, byo_provider = await _resolve_provider(owner_id)
        owner_type = "org" if is_org_owner_id(owner_id) else "personal"
        await manager.provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            owner_type=owner_type,
            claim_id=claim_id,
            generation=generation,
            attempt_token=attempt_token,
        )
        # The full-provision path (create_user_service) MINTS a fresh
        # provision_generation — for a brand-new owner AND for an existing
        # row whose ECS service was missing/INACTIVE. Re-read so
        # wait_for_running fences on the CURRENT incarnation, not the
        # pre-create value read above (which would make its own running
        # write raise ProvisionSuperseded against this very run and wedge
        # the row in "provisioning"). claim_id/attempt_token stay the
        # primary fences.
        post_row = await container_repo.get_by_owner_id(owner_id)
        generation = (post_row or {}).get("provision_generation")
        await manager.wait_for_running(
            owner_id,
            claim_id=claim_id,
            generation=generation,
            attempt_token=attempt_token,
        )
    except ProvisionSuperseded:
        logger.info("provision superseded for owner %s — standing down", owner_id)
        return EXIT_SUPERSEDED
    except Exception as exc:  # noqa: BLE001 — classified into the exit-code ABI
        # provision_user_container already released the lease to error with
        # cause fields (via the same shared classifier); the exit code only
        # tells the machine whether to retry.
        logger.exception("provision failed for owner %s", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_DETERMINISTIC

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
