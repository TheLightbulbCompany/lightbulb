"""Guided-setup requests from the public `/request-setup` form.

One row per submission on the ``setup-requests`` table (partition key ``id``,
a random uuid). Rows are append-only: the founders read them, nothing else
writes or scans them.
"""

from __future__ import annotations

from core.dynamodb import get_table, run_in_thread


async def put(item: dict) -> None:
    await run_in_thread(get_table("setup-requests").put_item, Item=item)
