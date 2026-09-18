"""Roll AgentCore (Hermes) workspaces onto the currently configured bridge image.

Runs INSIDE the backend task (ECS exec), like ``migrate_workspace_to_hermes``:

    uv run python -m scripts.roll_hermes_runtimes --owner-id <org> [--dry-run]
    uv run python -m scripts.roll_hermes_runtimes --all [--dry-run]

A ``hermes-version.json`` tag flip changes ``AGENTCORE_HERMES_IMAGE`` for the backend, but an
existing runtime keeps the image it was created with until ``UpdateAgentRuntime`` moves it --
that is what ``hermes_provisioning.roll_runtime_image`` does per owner (update on drift, wait
READY, wait for the bridge to report idle, stop the warm session). Exit 0 when every targeted
owner is current or updated, 1 if any owner failed (each failure is logged; the rest still roll).

Read the per-owner line this prints, not just the exit code. ``updated-session-busy`` means the
runtime IS on the new image but a turn was still running at the ten-minute deadline, so the warm
session was deliberately left alive and keeps serving the OLD image until it ends -- up to its
4 h idle / 8 h lifetime. That is exit 0 by design (nothing failed), so a roll is only complete
once every line reads ``current`` or ``updated``; re-run for the stragglers.

This is a SEQUENTIAL loop, and the idle wait is per owner: budget ~55-90 s extra for each parked
owner (the probe cold-boots its microVM) and up to ~10.5 min for each busy one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import container_repo
from core.services import runtime_transport
from core.services.hermes_provisioning import roll_runtime_image

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    return sorted(r["owner_id"] for r in rows if runtime_transport.host_for(r) == "agentcore" and r.get("owner_id"))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _targets(args):
        try:
            result = await roll_runtime_image(owner_id, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet roll
            failed += 1
            logger.error("roll: owner=%s FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        print(f"{owner_id} {result}")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
