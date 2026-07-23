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
    from core.repositories.container_repo import ProvisionSuperseded, classify_provision_error

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
