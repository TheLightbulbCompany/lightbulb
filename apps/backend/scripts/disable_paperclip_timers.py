"""Super-agent step 0 (spec 03 §8): turn off every Paperclip timer heartbeat and pause the CEO
routines we created, for dev workspaces that were armed before the switch.

``paperclip_agent_lifecycle.disable_timer_automation`` per owner: the owner's own routines are
never touched. Live code keeps it off from here (``arm_nightly_ceo`` with
``NIGHTLY_CEO_HEARTBEATS_ENABLED`` false). Refuses prod. Re-runnable. DRY RUN BY DEFAULT.
Runs inside the backend task (Paperclip is VPC-internal):

    uv run python -m scripts.disable_paperclip_timers --all
    uv run python -m scripts.disable_paperclip_timers --all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.config import settings
from core.repositories import billing_repo
from core.services.paperclip_agent_lifecycle import disable_timer_automation


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod":
        print("refusing: disable_paperclip_timers is dev-only")
        return 2

    owners = (
        [args.owner_id]
        if args.owner_id
        else sorted({r["owner_id"] for r in await billing_repo.scan_all() if r.get("owner_id")})
    )
    verb = "disabled" if args.apply else "would disable"
    failed = timers = routines = 0
    for owner_id in owners:
        try:
            report = await disable_timer_automation(owner_id, dry_run=not args.apply)
        except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
            failed += 1
            print(f"{owner_id}: ERROR {type(e).__name__}: {e}")
            continue
        timers += len(report["agents"])
        routines += len(report["routines"])
        if report["agents"] or report["routines"]:
            print(f"{owner_id}: {verb} timers={report['agents']} ceo_routines={report['routines']}")
    print(f"{verb}: {timers} timers, {routines} CEO routines across {len(owners)} owners; {failed} errors")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
