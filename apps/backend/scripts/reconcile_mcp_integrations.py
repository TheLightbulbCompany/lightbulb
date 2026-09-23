"""Re-project connected integrations' MCP wiring into existing Hermes profiles.

``credentials.mcp_server.reconcile_integrations`` rewrites every roster profile's
``mcp_servers`` + ``.env`` from the durable sources and bumps ``.revision``, so the bridge re-pulls
before the agent's next turn. A change to how that block is BUILT (e.g. ``hermes_profiles.
with_profile_home`` pinning a stdio server's ``HOME``) reaches only profiles written after it
ships; this pushes it onto profiles that already exist. No image roll, no restart.

Runs INSIDE the backend task (ECS exec), like ``reapply_hermes_skills_dirs``:

    uv run python -m scripts.reconcile_mcp_integrations --owner-id <org> [--dry-run]
    uv run python -m scripts.reconcile_mcp_integrations --all [--dry-run]

``--dry-run`` lists each agent whose stored config has a stdio MCP server without its profile
``HOME``. The real run reconciles only owners with at least one such agent; the reconcile itself is
idempotent. Sequential; continues past a failing owner and exits non-zero if any failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.credentials.mcp_server import reconcile_integrations
from core.services.hermes_profiles import read_profile_config, with_profile_home
from core.services.messaging import load_agent_roster

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    return sorted(r["owner_id"] for r in rows if r.get("agentcore_runtime_arn") and r.get("owner_id"))


def _stale_servers(owner_id: str, agent_id: str) -> list[str]:
    """Stdio servers in this agent's stored config whose HOME is not its profile home."""
    servers = read_profile_config(owner_id, agent_id).get("mcp_servers") or {}
    wanted = with_profile_home(servers, agent_id)
    return sorted(name for name in servers if servers[name] != wanted[name])


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
            stale = {}
            for agent in await load_agent_roster(owner_id):
                names = await run_in_thread(_stale_servers, owner_id, agent["id"])
                if names:
                    stale[agent["id"]] = names
            for agent_id, names in stale.items():
                print(f"{owner_id} {agent_id} {'would-reconcile' if args.dry_run else 'reconcile'} {','.join(names)}")
            if stale and not args.dry_run:
                await reconcile_integrations(owner_id)
                print(f"{owner_id} reconciled")
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet run
            failed += 1
            logger.error("reconcile: owner=%s FAILED: %s: %s", owner_id, type(exc).__name__, exc)
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
