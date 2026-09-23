"""Roll AgentCore (Hermes) workspaces onto the currently configured bridge image.

Runs INSIDE the backend task (ECS exec), like ``migrate_workspace_to_hermes``:

    uv run python -m scripts.roll_hermes_runtimes --owner-id <org> [--dry-run]
    uv run python -m scripts.roll_hermes_runtimes --all [--dry-run]
    uv run python -m scripts.roll_hermes_runtimes --all --respec   # model flip: env + profiles

A ``hermes-version.json`` tag flip changes ``AGENTCORE_HERMES_IMAGE`` for the backend, but an
existing runtime keeps the image it was created with until ``UpdateAgentRuntime`` moves it --
that is what ``hermes_provisioning.roll_runtime_image`` does per owner (update on drift, wait
READY, wait for the bridge to report idle, stop the warm session). Exit 0 when every targeted
owner is current or updated, 1 if any owner failed or came back busy (the rest still roll).

``updated-session-busy`` means the runtime IS on the new spec but a turn was still running at the
ten-minute deadline, so the warm session was deliberately left alive. When the env moved (a
managed-model flip) its profiles were NOT rewritten, and the next cold start refuses the mismatch
-- so a busy owner exits 1: re-run it with ``--respec`` (its env no longer reads as drift).

``--dry-run`` is NOT read-only: it still runs the idempotent ensure calls a roll needs to
compute the desired spec (the owner's Browser profile create-or-adopt, the memory IAM policy).
It skips only ``UpdateAgentRuntime`` and the session stop, answering ``would-update``.

This is a SEQUENTIAL loop, and the idle wait is per owner: budget ~55-90 s extra for each parked
owner (the probe cold-boots its microVM) and up to ~10.5 min for each busy one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import container_repo
from core.services.hermes_provisioning import roll_runtime_image

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    # A row without a runtime ARN has nothing to roll -- there is no other transport to skip.
    return sorted(r["owner_id"] for r in rows if r.get("agentcore_runtime_arn") and r.get("owner_id"))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--respec", action="store_true", help="re-spec on a current image and rewrite profiles")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _targets(args):
        try:
            result = await roll_runtime_image(owner_id, dry_run=args.dry_run, respec=args.respec)
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet roll
            failed += 1
            logger.error("roll: owner=%s FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        print(f"{owner_id} {result}")
        # A busy owner may have a moved env but its old profile (an env drift, or --respec):
        # re-run it with --respec (the env no longer reads as drift) or it breaks on cold start.
        failed += result == "updated-session-busy"
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
