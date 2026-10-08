"""ONE-OFF repair: turn back on the private user stream of every CURRENT member whose stream is off.

Before #3922, ``ensure_user_stream`` revived an inactive stream on any call (live-config above
all), so a member who was removed and then re-added got their stream back the next time they
opened the app. Now only the ``organizationMembership.created`` webhook (``rejoin=True``) does.
A member re-added before that deploy, who has not opened the app since, still has an inactive
row and would get no live events. This script finds them: for every workspace, Clerk's CURRENT
member list; for each member whose ``USERSTREAM#`` row exists and is inactive,
``ensure_user_stream(owner, user, rejoin=True)``. A person not in Clerk's list is never touched.

DRY RUN by default; ``--apply`` writes. Run once as a one-off Fargate run-task after the deploy
(the same way ``roll_hermes_runtimes`` is run), then DELETE this script and its test:

    uv run python -m scripts.repair_inactive_member_streams [--owner-id <org>] [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.repositories import container_repo, messaging_repo
from core.services import clerk_admin

logger = logging.getLogger(__name__)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner-id")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)

    if args.owner_id:
        owners = [args.owner_id]
    else:
        rows = await container_repo.scan_all(consistent=True)
        owners = sorted({r["owner_id"] for r in rows if str(r.get("owner_id") or "").startswith("org_")})

    repaired = failed = 0
    for owner_id in owners:
        try:
            # strict: a Clerk blip must fail this owner, not read as "no members"
            for member in await clerk_admin.list_organization_members(owner_id, strict=True):
                user_id = member["user_id"]
                stream = await messaging_repo.get_user_stream(owner_id, user_id)
                if stream is None or stream.get("active"):
                    continue
                if args.apply:
                    await messaging_repo.ensure_user_stream(owner_id, user_id, rejoin=True)
                repaired += 1
                print(f"{owner_id} {user_id} {'repaired' if args.apply else 'would-repair'}")
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the sweep
            failed += 1
            logger.error("repair: owner=%s FAILED: %s: %s", owner_id, type(exc).__name__, exc)
    logger.info(
        "repair: owners=%d %s=%d failed=%d", len(owners), "repaired" if args.apply else "would-repair", repaired, failed
    )
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
