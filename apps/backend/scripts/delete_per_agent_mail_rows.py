"""One-off: delete every per-agent mailbox row from ``agent-email-addresses``.

Owner ruling 2026-10-03: agents have no mailboxes of their own; the workspace's
``agents@{slug}.{domain}`` is the only address. Rows for ``friday@…``,
``atlas@…`` and the like no longer route anything and are deleted here. The
team row is created on first use, so nothing replaces them.

Runs INSIDE the backend task (ECS exec), once per env after the deploy:

    uv run python -m scripts.delete_per_agent_mail_rows            # dry run: lists, deletes nothing
    uv run python -m scripts.delete_per_agent_mail_rows --apply

DELETE THIS SCRIPT once it has run with ``--apply`` in both dev and prod
(CLAUDE.md: spent one-shots go; git history keeps it).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.dynamodb import run_in_thread
from core.repositories import agent_email_repo


def is_per_agent(row: dict) -> bool:
    return str(row.get("address", "")).partition("@")[0] != "agents"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)

    doomed = [r for r in await agent_email_repo.scan_all() if is_per_agent(r)]
    for row in doomed:
        if args.apply:
            await run_in_thread(agent_email_repo._get_table().delete_item, Key={"address": row["address"]})
        print(f"{row['address']} (owner {row.get('owner_id')}): {'deleted' if args.apply else 'would-delete'}")
    print(f"{len(doomed)} per-agent row(s) {'deleted' if args.apply else 'found; re-run with --apply'}")
    return 0


if __name__ == "__main__":
    assert is_per_agent({"address": "friday@acme.thelightbulb.company"})
    assert is_per_agent({"address": "agentsx@acme.thelightbulb.company"})
    assert not is_per_agent({"address": "agents@acme.thelightbulb.company"})
    sys.exit(asyncio.run(main()))
