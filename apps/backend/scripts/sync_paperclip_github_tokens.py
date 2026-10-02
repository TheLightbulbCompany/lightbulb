"""Sync each workspace's GitHub token into Paperclip's ``GITHUB_TOKEN`` company secret.

``paperclip_github_token.sync_github_token`` already runs on every GitHub connect, disconnect
and runtime reconcile, and the backend sweeps every workspace at startup before it applies
Paperclip's instance flags (``paperclip_instance_flags.converge_at_startup``). This is the
manual door onto the same sweep: check the fleet, or repair it without waiting for a deploy.

``--all`` covers every owner with an ACTIVE Paperclip company, not only the GitHub-connected
ones: an owner whose disconnect cleanup failed holds no Lightbulb token but still has our
secret in Paperclip, and this is what deletes it.

Runs INSIDE the backend task (ECS exec), like ``roll_hermes_runtimes`` -- it needs
``PAPERCLIP_INTERNAL_URL`` and the backend's Secrets Manager grants:

    uv run python -m scripts.sync_paperclip_github_tokens --all              # dry run (default)
    uv run python -m scripts.sync_paperclip_github_tokens --all --apply
    uv run python -m scripts.sync_paperclip_github_tokens --owner-id <org> [--apply]

Dry-run is the default: it reads the Lightbulb token and makes Paperclip's secret LIST call
only, and prints what it would do (``would_create`` / ``would_rotate`` / ``would_delete``).
Prints one line per owner with the outcome -- never the token: ``created``, ``rotated``,
``unchanged`` (Paperclip already holds this token; nothing written), ``deleted``, ``absent``,
``foreign_secret``, ``inactive_secret``, ``no_company``, ``unsettled``, ``conflict``, ``failed``
(the reason is logged). Idempotent and sequential; continues past a failing owner and exits
non-zero if any owner ended ``failed``, ``unsettled`` or ``conflict``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.services.paperclip_github_token import UNSYNCED, sweep


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write to Paperclip (default: dry run)")
    args = parser.parse_args(argv)

    unsynced = 0
    async for owner_id, outcome in sweep([args.owner_id] if args.owner_id else None, dry_run=not args.apply):
        print(f"{owner_id} {outcome}")
        unsynced += outcome in UNSYNCED
    return 1 if unsynced else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
