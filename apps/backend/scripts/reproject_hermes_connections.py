"""Re-project an already-migrated AgentCore owner's connected integrations into their profiles.

``migrate_workspace_to_hermes`` now does this as one step of its tail, so this exists for the
workspaces migrated BEFORE it did — their profiles carry no ``home/`` and no ``mcp-tokens/``
while every integrations row still reads "connected". See
``core.services.credentials.reproject`` for what it can and cannot fix.

Runs INSIDE the backend task (ECS exec), like ``migrate_workspace_to_hermes`` and
``roll_hermes_runtimes`` — it needs the backend's own settings and IAM:

    uv run python -m scripts.reproject_hermes_connections --owner-id <org> [--dry-run]

Prints one ``<provider>=<outcome>`` line per connected provider. Exit 0 when every provider
projected or merely needs the owner to reconnect, 1 when any provider errored.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.services.credentials.reproject import reproject_connections

logger = logging.getLogger(__name__)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    outcomes = await reproject_connections(args.owner_id, dry_run=args.dry_run)
    tag = "DRY RUN: " if args.dry_run else ""
    if not outcomes:
        print(f"{tag}{args.owner_id}: nothing to project (not an AgentCore owner, or no connected integrations)")
        return 0
    for provider, outcome in sorted(outcomes.items()):
        print(f"{tag}{args.owner_id} {provider}={outcome}")
    return 1 if any(o.startswith("error:") for o in outcomes.values()) else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    sys.exit(asyncio.run(main()))
