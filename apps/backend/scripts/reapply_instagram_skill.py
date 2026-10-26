"""Refresh ``skills/instagram/{SKILL.md,token.sh}`` in already-connected Instagram profiles.

The ``instagram_cli`` materializer copies the skill files into a profile when Instagram connects,
so a change to the templates (e.g. #4321's Hermes paths) reaches only profiles seeded afterwards.
This rewrites just those two files from the current templates into every roster profile that
already holds ``skills/instagram/credentials.json``. It never touches ``credentials.json``: the
profile's copy is the live one (``token.sh`` renews the 60-day token in place), so a re-projection
from a stored credential would roll the token back.

Runs INSIDE the backend task (ECS exec), like ``reapply_hermes_skills_dirs``:

    uv run python -m scripts.reapply_instagram_skill --owner-id <org> [--dry-run]
    uv run python -m scripts.reapply_instagram_skill --all [--dry-run]

Idempotent (rewrites identical bytes, bumps ``.revision`` so the bridge re-pulls). Prints one line
per agent; exits non-zero if any owner/agent failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import integrations_repo
from core.services import hermes_profiles
from core.services.credentials.materializers import instagram_cli
from core.services.messaging import load_agent_roster

logger = logging.getLogger(__name__)

_DIR = "skills/instagram/"


def _apply_one(owner_id: str, agent_id: str, *, dry_run: bool) -> str:
    """Sync S3 work -- call via ``run_in_thread``."""
    prefix = hermes_profiles.profile_prefix(owner_id, agent_id)
    if hermes_profiles._s3_get(settings.OWNER_STATE_BUCKET, f"{prefix}{_DIR}credentials.json") is None:
        return "skipped (no instagram credentials in this profile)"
    if dry_run:
        return "would-update"
    files = {
        f"{_DIR}SKILL.md": instagram_cli._SKILL_TEMPLATE.read_bytes(),
        f"{_DIR}token.sh": instagram_cli._TOKEN_SCRIPT.read_bytes(),
    }
    hermes_profiles.write_profile_subtree(owner_id, agent_id, files, ())
    return "updated"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.owner_id:
        owners = [args.owner_id]
    else:
        owners = sorted({r["owner_id"] for r in await integrations_repo.scan_all() if r.get("provider") == "instagram"})
    failed = 0
    for owner_id in owners:
        try:
            agent_ids = [agent["id"] for agent in await load_agent_roster(owner_id)]
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet run
            failed += 1
            logger.error("instagram-skill: owner=%s roster lookup FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        for agent_id in agent_ids:
            try:
                result = await run_in_thread(_apply_one, owner_id, agent_id, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error(
                    "instagram-skill: owner=%s agent=%s FAILED: %s: %s", owner_id, agent_id, type(exc).__name__, exc
                )
                continue
            print(f"{owner_id} {agent_id} {result}")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
