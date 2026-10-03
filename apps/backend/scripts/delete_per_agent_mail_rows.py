"""One-off: delete every per-agent mailbox row from ``agent-email-addresses``.

Owner ruling 2026-10-03: agents have no mailboxes of their own; the workspace's
``agents@{slug}.{domain}`` is the only address. Rows for ``friday@…``,
``atlas@…`` and the like no longer route anything and are deleted here. Before
an owner's rows go, their team row is ensured and given the STRICTEST of the
owner's settings (any paused row pauses it; ``send_to_anyone`` only if every row
had it; verified senders unioned), so the cut-over never loosens a fence.

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
from core.services.agent_email import ensure_team_address
from core.services.agent_mail_send import _owner_email


def is_per_agent(row: dict) -> bool:
    return str(row.get("address", "")).partition("@")[0] != "agents"


def strictest(rows: list[dict]) -> dict:
    """``update_settings`` kwargs: the strictest of every row's owner-editable settings."""
    settings = {
        "send_to_anyone": all(bool(r.get("send_to_anyone")) for r in rows),
        "verified_senders": sorted({s for r in rows for s in r.get("verified_senders") or []}),
    }
    paused = next((r["outbound_disabled"] for r in rows if r.get("outbound_disabled")), None)
    if paused:
        settings["outbound_disabled"] = paused
    return settings


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)

    by_owner: dict[str, list[dict]] = {}
    for row in await agent_email_repo.scan_all():
        by_owner.setdefault(str(row.get("owner_id")), []).append(row)
    deleted = 0
    for owner_id, rows in by_owner.items():
        doomed = [r for r in rows if is_per_agent(r)]
        if not doomed:
            continue
        # The owner's existing team row (if any) takes part, so its own fences stay too.
        settings = strictest(rows)
        print(f"owner {owner_id}: {[r['address'] for r in doomed]} -> team row settings {settings}")
        if not args.apply:
            continue
        address, reason = await ensure_team_address(owner_id, await _owner_email(owner_id))
        if not address or await agent_email_repo.update_settings(address, **settings) is None:
            print(f"owner {owner_id}: team row not ready ({reason}); per-agent rows KEPT")
            continue
        for row in doomed:
            await run_in_thread(agent_email_repo._get_table().delete_item, Key={"address": row["address"]})
            deleted += 1
        print(f"owner {owner_id}: {address} updated, {len(doomed)} per-agent row(s) deleted")
    print(f"{deleted} per-agent row(s) deleted" if args.apply else "dry run; re-run with --apply")
    return 0


if __name__ == "__main__":
    assert is_per_agent({"address": "friday@acme.thelightbulb.company"})
    assert is_per_agent({"address": "agentsx@acme.thelightbulb.company"})
    assert not is_per_agent({"address": "agents@acme.thelightbulb.company"})
    assert strictest(
        [
            {"send_to_anyone": True, "verified_senders": ["a@x.com"]},
            {"send_to_anyone": False, "verified_senders": ["b@x.com", "a@x.com"], "outbound_disabled": "complaint"},
        ]
    ) == {"send_to_anyone": False, "verified_senders": ["a@x.com", "b@x.com"], "outbound_disabled": "complaint"}
    assert strictest([{"send_to_anyone": True}]) == {"send_to_anyone": True, "verified_senders": []}
    sys.exit(asyncio.run(main()))
