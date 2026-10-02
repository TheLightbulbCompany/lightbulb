"""Sync each workspace's GitHub token into Paperclip's ``GITHUB_TOKEN`` company secret.

``paperclip_github_token.sync_github_token`` already runs on every GitHub connect, disconnect
and runtime reconcile. This is the re-runnable ops entry point for the workspaces that connected
GitHub BEFORE that shipped (and for checking the fleet afterwards): it calls the same function
for every owner with a connected ``github`` integration.

Run it before switching on Paperclip's ``enableExternalObjects``: an unauthenticated lookup of a
private repository is stored as a terminal "Not found".

Runs INSIDE the backend task (ECS exec), like ``roll_hermes_runtimes`` -- it needs
``PAPERCLIP_INTERNAL_URL`` and the backend's Secrets Manager grants:

    uv run python -m scripts.sync_paperclip_github_tokens --all              # dry run (default)
    uv run python -m scripts.sync_paperclip_github_tokens --all --apply
    uv run python -m scripts.sync_paperclip_github_tokens --owner-id <org> [--apply]

Dry-run is the default: it reads the Lightbulb token and makes Paperclip's secret LIST call
only, and prints what it would do (``would_create`` / ``would_rotate`` / ``would_delete``).
Prints one line per owner with the outcome -- never the token: ``created``, ``rotated``,
``unchanged`` (Paperclip already holds this token; nothing written), ``deleted``, ``absent``,
``foreign_secret``, ``inactive_secret``, ``no_company``. Idempotent and sequential;
continues past a failing owner and exits non-zero if any failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import integrations_repo
from core.services.paperclip_github_token import sync_github_token

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await integrations_repo.scan_all()
    return sorted({r["owner_id"] for r in rows if r.get("provider") == "github" and r.get("status") == "connected"})


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write to Paperclip (default: dry run)")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _targets(args):
        try:
            outcome = await sync_github_token(owner_id, dry_run=not args.apply)
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet run
            failed += 1
            # Type and status only: str() of a PaperclipApiError carries the response body.
            status = getattr(exc, "status_code", "-")
            logger.error("sync: owner=%s FAILED: %s status=%s", owner_id, type(exc).__name__, status)
            continue
        print(f"{owner_id} {outcome}")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
