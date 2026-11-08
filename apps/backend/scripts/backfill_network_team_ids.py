"""Store ``teamId`` in ``sessions.json`` for workspaces already on a network an admin added them to.

Runs INSIDE the backend task (ECS exec), like ``roll_hermes_runtimes``, and only AFTER the image
whose bridge answers ``network.team`` is rolled to those workspaces (``roll_hermes_runtimes --all``):

    uv run python -m scripts.backfill_network_team_ids --owner-id <org> [--dry-run]
    uv run python -m scripts.backfill_network_team_ids --all [--dry-run]

Targets each owner with a ``sessions.json`` holding ``joined: true`` and no ``teamId``. Each one is
asked ``network.team`` (this wakes a parked runtime, ~55 s) and the answer is stored. Sequential; one
owner's failure is logged and the rest go on (exit 1 if any failed). ``--dry-run`` lists the targets
and asks nothing. Nothing here logs a ticket (there is none).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import container_repo
from core.services.runtime_transport import runtime_arn
from core.services import container_rpc, network_member

logger = logging.getLogger(__name__)


async def _owners(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    return sorted(r["owner_id"] for r in rows if runtime_arn(r) and r.get("owner_id"))


async def _needs_team_id(owner_id: str) -> bool:
    doc = await network_member._read_sessions(owner_id)
    return bool(doc and doc.get("joined") and not doc.get("teamId"))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _owners(args):
        try:
            if not await _needs_team_id(owner_id):
                continue
            if args.dry_run:
                print(f"{owner_id} would-store")
                continue
            team = await container_rpc.call(owner_id, "network.team", {}, timeout_s=85)
            team_id = (team or {}).get("team_id")
            if not team_id:
                raise RuntimeError("the bridge returned no team_id")
            doc = await network_member._read_sessions(owner_id)
            await network_member._patch_sessions(owner_id, doc, teamId=team_id)
            print(f"{owner_id} stored")
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet
            failed += 1
            logger.error("backfill: owner=%s FAILED: %s", owner_id, type(exc).__name__)
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
