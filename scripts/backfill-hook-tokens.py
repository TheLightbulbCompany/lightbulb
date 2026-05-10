#!/usr/bin/env python3
"""Backfill OPENCLAW_HOOK_TOKEN for all existing provisioned users.

Run once after the hook-token deploy. Idempotent: skips users that
already have a hook_token set on their containers row.

Steps per user:
  1. Generate token, persist via container_repo.set_hook_token.
  2. Call EcsManager.refresh_task_definition to re-register the task
     def with OPENCLAW_HOOK_TOKEN in the env list and roll the service.
  3. Wait until the service has at least one RUNNING task on the new
     revision.
  4. Patch openclaw.json via the existing config patcher so the gateway
     re-reads its config with the new hooks block.

Usage:
    AWS_PROFILE=isol8-admin DDB_PREFIX=isol8-dev \\
        python scripts/backfill-hook-tokens.py [--dry-run] [--owner u_1]
"""

import argparse
import asyncio
import logging
import secrets
import sys
import time

# Allow running from the repo root.
sys.path.insert(0, "apps/backend")

from core.config import settings  # noqa: E402
from core.containers.ecs_manager import EcsManager  # noqa: E402
from core.repositories import container_repo  # noqa: E402
from core.services.config_patcher import patch_openclaw_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-hook-tokens")


_HOOKS_PATCH = {
    "hooks": {
        "enabled": True,
        "token": "${OPENCLAW_HOOK_TOKEN}",
        "path": "/hooks",
        "maxBodyBytes": 1_048_576,
        # OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) requires "hook:" in
        # this list when defaultSessionKey is unset. Without it, the gateway
        # throws at boot. This is the same shape rendered by config.py for
        # newly-provisioned users; we patch existing users to match.
        "allowedSessionKeyPrefixes": ["isol8-", "hook:"],
        "allowRequestSessionKey": False,
    }
}


async def _wait_for_new_revision_running(
    manager: EcsManager,
    container: dict,
    new_task_def_arn: str,
    timeout_s: int = 300,
) -> bool:
    """Poll describe_services until the PRIMARY deployment has rolled to
    new_task_def_arn AND has at least one task running on it.

    Different from resolve_running_container — that helper returns success
    as soon as ANY task is up, which during a forceNewDeployment rollout is
    the OLD task. We need to confirm the NEW revision is actually live
    before we mark hook_token_provisioned_at; otherwise a subsequent
    rollout failure leaves the row marked complete but the running
    container rejecting the token the proxy stored.
    """
    deadline = time.monotonic() + timeout_s
    cluster = settings.ECS_CLUSTER_ARN
    service_name = container["service_name"]
    while time.monotonic() < deadline:
        try:
            response = manager._ecs.describe_services(
                cluster=cluster, services=[service_name]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] describe_services failed: %s", container.get("owner_id"), exc)
            await asyncio.sleep(5)
            continue
        services = response.get("services") or []
        if not services:
            await asyncio.sleep(5)
            continue
        # PRIMARY is the deployment ECS is currently rolling out.
        primary = next(
            (d for d in services[0].get("deployments", []) if d.get("status") == "PRIMARY"),
            None,
        )
        if (
            primary
            and primary.get("taskDefinition") == new_task_def_arn
            and primary.get("runningCount", 0) >= 1
        ):
            return True
        await asyncio.sleep(5)
    return False


async def backfill_one(manager: EcsManager, owner_id: str, dry_run: bool) -> str:
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        return "no_container_row"
    # Idempotency guard: hook_token_provisioned_at is the COMPLETION marker.
    # set_hook_token alone doesn't guarantee the env var is on the running
    # task or the openclaw.json carries the hooks block — those happen later
    # in the sequence. Re-running is safe and resumes from wherever the
    # previous run left off.
    if container.get("hook_token_provisioned_at"):
        return "already_provisioned"
    if dry_run:
        return "would_backfill"

    token = secrets.token_urlsafe(32)
    await container_repo.set_hook_token(owner_id, token)
    log.info("[%s] set hook_token", owner_id)

    new_arn = await manager.refresh_task_definition(owner_id)
    if not new_arn:
        return "refresh_failed"
    log.info("[%s] task-def refreshed to %s; waiting for new revision to RUNNING", owner_id, new_arn)

    if not await _wait_for_new_revision_running(manager, container, new_arn):
        return "timeout_waiting_for_new_revision"

    await patch_openclaw_config(owner_id, _HOOKS_PATCH)
    log.info("[%s] config patched", owner_id)

    # Final step: mark fully provisioned. Done LAST so a crash anywhere
    # above leaves the row's hook_token_provisioned_at unset, and the
    # next backfill run resumes cleanly.
    await container_repo.mark_hook_token_provisioned(owner_id)
    log.info("[%s] marked hook_token_provisioned_at", owner_id)
    return "ok"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--owner", help="Backfill a single owner_id (otherwise all)")
    args = parser.parse_args()

    manager = EcsManager()
    if args.owner:
        targets = [args.owner]
    else:
        rows = await container_repo.scan_all()
        targets = [r["owner_id"] for r in rows]

    log.info("backfilling %d owners (dry_run=%s)", len(targets), args.dry_run)
    summary: dict[str, int] = {}
    for oid in targets:
        try:
            outcome = await backfill_one(manager, oid, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            outcome = f"error: {exc}"
        summary[outcome] = summary.get(outcome, 0) + 1
        log.info("[%s] %s", oid, outcome)

    log.info("done: %s", summary)


if __name__ == "__main__":
    asyncio.run(main())
