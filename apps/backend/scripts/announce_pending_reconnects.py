"""Announce every integration already ``reconnect_required`` (re-runnable ops tool).

``access_token._mark_reconnect`` files the Needs-you item and reconciles the profiles' MCP entries
only on the TRANSITION into ``reconnect_required``. A row that was already there before that
shipped never transitions again, so it has neither. This scans the integrations table and runs the
same announce (``access_token._announce_reconnect``) for each such row.

Runs INSIDE the backend task (ECS exec), like ``refresh_owner_facts``:

    uv run python -m scripts.announce_pending_reconnects            # dry run (default): lists rows
    uv run python -m scripts.announce_pending_reconnects --apply

Safe to re-run: the Activity item is keyed per provider, so a repeat shows one item. Best-effort
per row (the announce never raises); read the log for skips.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.repositories import integrations_repo
from core.services.credentials import access_token


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="announce; without it, only list")
    args = parser.parse_args(argv)

    rows = [r for r in await integrations_repo.scan_all() if r.get("status") == "reconnect_required"]
    for row in sorted(rows, key=lambda r: (r["owner_id"], r["provider"])):
        owner_id, provider = row["owner_id"], row["provider"]
        if not args.apply:
            print(f"{owner_id} {provider} would-announce")
            continue
        await access_token._announce_reconnect(owner_id, provider)
        print(f"{owner_id} {provider} announced")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
