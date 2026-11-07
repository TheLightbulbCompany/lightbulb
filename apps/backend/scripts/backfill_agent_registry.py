"""Backfill the super-agent `AGENT#` registry from Paperclip, keeping Paperclip's agent ids (spec 03 §6.1).

DRY RUN BY DEFAULT: prints the planned created/updated counts. `--apply` writes (idempotent) and soft-deletes
registry rows whose agent is terminated or gone in Paperclip. `--verify`
compares registry and Paperclip and exits 1 on any difference, printing agent ids and field names only (no
names, no personas). Refuses prod (unless --allow-prod).
Runs inside the dev backend task (Paperclip is VPC-internal):

    uv run python -m scripts.backfill_agent_registry --all
    uv run python -m scripts.backfill_agent_registry --all --apply
    uv run python -m scripts.backfill_agent_registry --all --verify
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.agent_registry import seed_from_paperclip, verify


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner-id")
    target.add_argument("--all", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    mode.add_argument("--verify", action="store_true", help="compare only; exit 1 on any difference")
    parser.add_argument("--allow-prod", action="store_true", help="lift the prod refusal (phase-1 task 3 data move)")
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod" and not args.allow_prod:
        print("refusing: backfill_agent_registry is dev-only unless --allow-prod")
        return 2

    owners = (
        [args.owner_id]
        if args.owner_id
        else sorted(await PaperclipRepo(table_name="paperclip-companies").list_active_workspace_members())
    )
    failed = diffs = 0
    totals = [0, 0, 0, 0]
    for ws in owners:
        try:
            if args.verify:
                found = await verify(ws)
                diffs += len(found)
                for d in found:
                    print(f"{ws}: {d.kind} {d.agent_id}" + (f" field={d.field}" if d.field else ""))
                continue
            report = await seed_from_paperclip(ws, dry_run=not args.apply)
        except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
            failed += 1
            print(f"{ws}: ERROR {type(e).__name__}")
            continue
        totals = [t + n for t, n in zip(totals, report)]
        if report.created or report.updated or report.deleted:
            print(f"{ws}: created={report.created} updated={report.updated} deleted={report.deleted}")
    if args.verify:
        print(f"verify: {diffs} differences across {len(owners)} workspaces; {failed} errors")
        return 1 if diffs or failed else 0
    verb = "applied" if args.apply else "would apply (dry run)"
    print(
        f"{verb}: created={totals[0]} updated={totals[1]} skipped_terminated={totals[2]} deleted={totals[3]} "
        f"across {len(owners)} workspaces; {failed} errors"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
