"""Roll AgentCore (Hermes) workspaces onto the currently configured bridge image.

Runs INSIDE the backend task (ECS exec), like ``migrate_workspace_to_hermes``:

    uv run python -m scripts.roll_hermes_runtimes --owner-id <org> [--dry-run]
    uv run python -m scripts.roll_hermes_runtimes --all [--dry-run]
    uv run python -m scripts.roll_hermes_runtimes --all --respec   # model flip: env + profiles
    uv run python -m scripts.roll_hermes_runtimes --owner-id <org> --max-concurrent-turns 6
    uv run python -m scripts.roll_hermes_runtimes --owner-id <org> --network-workers on

``--network-workers on|off`` stores whether the owner's agents take network assignments on its
container row and rolls, so every later roll keeps it.

``--max-concurrent-turns N`` stores the owner's turn cap on its container row (0 clears it back to
the image default, 4; 1 is refused; no ``--dry-run``) and rolls, so the env carries ``LIGHTBULB_MAX_CONCURRENT_TURNS`` from then on.

A ``hermes-version.json`` tag flip changes ``AGENTCORE_HERMES_IMAGE`` for the backend, but an
existing runtime keeps the image it was created with until ``UpdateAgentRuntime`` moves it --
that is what ``hermes_provisioning.roll_runtime_image`` does per owner (update on drift, wait
READY, wait for the bridge to report idle, stop the warm session). Exit 0 when every targeted
owner is current or updated, 1 if any owner failed or came back busy (the rest still roll).

``updated-session-busy`` means the runtime IS on the new spec but its session was not replaced by the
ten-minute deadline: a turn was still running, AgentCore never confirmed the stop, or a microVM older than
the update (a split session's survivor) still answered. The last line names every such owner. When the env moved (a
managed-model flip) its profiles were NOT rewritten, and the next cold start refuses the mismatch
-- so a busy owner exits 1: re-run it with ``--respec`` (its env no longer reads as drift).

Every non-dry-run roll first runs ``prepare_hermes_workspace`` (idempotent), so a boot-secret
field added since the owner was provisioned is written and the idle session is restarted to read
it (a retired field, like ``vaultToken``, is dropped without counting as a change). A dry run
does not report a pending boot-secret change.

On the tier's shared runtime (``HERMES_SHARED_RUNTIME_ARN``) the runtime itself is CDK's: a deploy
with a new ``hermes-version.json`` pin updates its image. This script then rolls each owner's warm
session that started before that update onto it (the same idle stop), and leaves parked ones alone.

``--dry-run`` is NOT read-only: it still runs the idempotent ensure calls a roll needs to
compute the desired spec (the owner's Browser profile create-or-adopt, the memory IAM policy).
It skips only ``UpdateAgentRuntime`` and the session stop, answering ``would-update``.

This is a SEQUENTIAL loop, and the idle wait is per owner: budget ~55-90 s extra for each parked
owner (the probe cold-boots its microVM) and up to ~10.5 min for each busy one. An owner whose
session was warm before the roll gets it woken again on the new spec (~40-55 s more), so its next
message does not wait out the cold start.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import container_repo
from core.services.runtime_transport import runtime_arn
from core.services.hermes_provisioning import (
    prepare_hermes_workspace,
    roll_runtime_image,
)

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    # A row without a runtime ARN has nothing to roll -- there is no other transport to skip.
    return sorted(r["owner_id"] for r in rows if runtime_arn(r) and r.get("owner_id"))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--respec", action="store_true", help="re-spec on a current image and rewrite profiles")
    parser.add_argument("--max-concurrent-turns", type=int, help="set the owner's turn cap (0 = default)")
    parser.add_argument("--network-workers", choices=("on", "off"), help="this owner's agents take network assignments")
    args = parser.parse_args(argv)
    if args.network_workers:
        if not args.owner_id or args.dry_run:
            parser.error("--network-workers needs --owner-id and no --dry-run")
        await container_repo.update_fields(args.owner_id, {"network_workers": args.network_workers == "on"})
    if args.max_concurrent_turns is not None:
        # 1 is refused: background compactions may take max(1, cap - 1) slots, so at 1 a compaction
        # could hold the only slot a typed turn needs.
        if not args.owner_id or args.dry_run or args.max_concurrent_turns == 1 or args.max_concurrent_turns < 0:
            parser.error("--max-concurrent-turns needs --owner-id, no --dry-run, and 0 (default) or >= 2")
        await container_repo.update_fields(args.owner_id, {"max_concurrent_turns": args.max_concurrent_turns or None})

    settings_changed = bool(args.network_workers) or args.max_concurrent_turns is not None
    failed, busy = 0, []
    for owner_id in await _targets(args):
        try:
            # Converge the boot secret first (a field a newer bridge reads): the bridge reads it
            # only at cold start, so a change needs the idle
            # stop the roll already does. Idempotent; the same step reconcile_runtime runs.
            boot_changed = False if args.dry_run else await prepare_hermes_workspace(owner_id)
            # On the shared runtime the turn cap and network workers are boot values the session takes
            # on its first invoke, not env drift: a change needs the idle restart.
            result = await roll_runtime_image(
                owner_id, dry_run=args.dry_run, respec=args.respec, restart_session=boot_changed or settings_changed
            )
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet roll
            failed += 1
            logger.error("roll: owner=%s FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        print(f"{owner_id} {result}")
        # A busy owner may have a moved env but its old profile (an env drift, or --respec):
        # re-run it with --respec (the env no longer reads as drift) or it breaks on cold start.
        if result == "updated-session-busy":
            failed += 1
            busy.append(owner_id)
        # A concurrent roll may have built its spec from the row before this cap landed: re-run.
        failed += (
            args.max_concurrent_turns is not None or bool(args.network_workers)
        ) and result == "update-in-progress"
    if busy:
        print("still busy (re-run them): " + " ".join(busy))
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
