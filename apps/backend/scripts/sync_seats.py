"""Set every Stripe-billed workspace's seat item to its current roster (T04).

Run once after ``STRIPE_SEAT_PRICE_ID`` is first configured in an environment (membership
events delivered before then were acknowledged with no seat billed), and any time seats are
suspected stale. Re-runnable: the sync sets an absolute quantity.

    uv run python -m scripts.sync_seats [--dry-run]

Runs inside the backend task (needs the Stripe key, Clerk key and DynamoDB). Exit 1 if any
workspace failed; the rest still sync.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.repositories import billing_repo
from core.services import billing_service


async def main(dry_run: bool) -> int:
    failed = 0
    for row in await billing_repo.scan_all():
        owner_id = row.get("owner_id", "")
        if not owner_id.startswith("org_") or not row.get("stripe_subscription_id"):
            continue
        try:
            if dry_run:
                print(owner_id, "seats", await billing_service.seats_for_workspace(owner_id))
            else:
                print(owner_id, await billing_service.sync_seat_quantity(owner_id))
        except Exception as e:  # noqa: BLE001 -- one workspace never stops the rest
            failed += 1
            print(owner_id, "error", type(e).__name__, e, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().dry_run)))
