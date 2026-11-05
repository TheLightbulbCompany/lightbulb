"""Re-index every task card of one workspace (dev/local only).

uv run python -m scripts.memory_index_tasks --owner org_x [--apply]   (dry run, counting tasks, unless --apply)
"""

import argparse
import asyncio

from core import tasks_schema as ts
from core.config import settings
from core.repositories import tasks_repo
from core.services import memory_tasks_index


async def _all_tasks(owner: str) -> list[dict]:
    out: list[dict] = []
    for status in ts.TaskStatus.__args__:
        cursor = None
        while True:
            page, cursor = await tasks_repo.list_tasks_by_status(owner, status, limit=100, cursor=cursor)
            out += page
            if not cursor:
                break
    return out


async def _run(owner: str, apply: bool) -> None:
    tasks = await _all_tasks(owner)
    print(f"{len(tasks)} tasks for {owner}")
    if not apply:
        return
    for task in tasks:
        await memory_tasks_index.index_task(owner, task)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--apply", action="store_true", help="write the cards (default: dry run)")
    args = ap.parse_args(argv)
    if settings.ENVIRONMENT == "prod":
        raise SystemExit("refusing to re-index task cards in prod")
    asyncio.run(_run(args.owner, args.apply))


if __name__ == "__main__":
    main()
