"""Backfill the Paperclip identity (URL, agent id, company id) into existing Hermes profiles' ``.env``.

``hermes_profiles.write_agent_profile`` now writes ``paperclip_identity_env`` next to
``PAPERCLIP_API_KEY``; profiles written before that carry the key alone, so every
``paperclip-api.sh`` call from a room/DM turn fails "Missing PAPERCLIP_API_URL". This merges the
three names into each roster profile's stored ``.env`` through ``patch_profile_env_and_config``
(the connect path's own writer: only the named lines change, ``config.yaml``/``SOUL.md`` are left
alone, ``.revision`` bumps so the bridge re-pulls). One-shot: delete once it has run in prod.

Runs INSIDE the backend task (ECS exec), like ``reapply_hermes_skills_dirs``:

    uv run python -m scripts.backfill_paperclip_identity_env --owner-id <org> [--dry-run]
    uv run python -m scripts.backfill_paperclip_identity_env --all [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.hermes_profiles import paperclip_identity_env, patch_profile_env_and_config
from core.services.messaging import load_agent_roster

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    return sorted(r["owner_id"] for r in rows if r.get("agentcore_runtime_arn") and r.get("owner_id"))


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
            roster = await load_agent_roster(owner_id)
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet run
            failed += 1
            logger.error("backfill: owner=%s roster lookup FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        for agent in roster:
            env = paperclip_identity_env(agent)
            if "PAPERCLIP_COMPANY_ID" not in env or "PAPERCLIP_API_URL" not in env:
                failed += 1
                print(f"{owner_id} {agent['id']} SKIPPED (identity incomplete: {sorted(env)})")
                continue
            if args.dry_run:
                print(f"{owner_id} {agent['id']} would-update {sorted(env)}")
                continue
            try:
                await run_in_thread(patch_profile_env_and_config, owner_id, agent["id"], env, {})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error(
                    "backfill: owner=%s agent=%s FAILED: %s: %s", owner_id, agent["id"], type(exc).__name__, exc
                )
                continue
            print(f"{owner_id} {agent['id']} updated")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
