"""Super-agent step 2 (spec 03 §6.2, wave1-R Task 6): copy workspace rows and pending-brief
sentinels from ``paperclip-companies`` to ``tasks`` (``WS#{org}/META``, ``WS#{org}/PENDING_BRIEF``).

``--apply`` writes a META row when it is missing or when the old row's ``updated_at`` is newer
than META's ``updatedAt`` (so a second pass after the flip carries late writes and never
overwrites newer META). ``--verify`` compares every copied attribute and reports ``missing``,
``field:<name>`` or ``meta_newer`` (not a failure); exit 1 on ``missing``/``field``.
``--reverse --apply`` copies META back onto the old rows (rollback), skipping any old row
newer than its META. Paperclip-only attributes
(``company_id``, ``agent_keys``, ``agent_cost_remainder``, ``automation_pause*``) never move:
they stay on the old row. Identity rows are never copied.

Refuses prod and an env with the super-agent master off. DRY RUN BY DEFAULT. Runs inside the
backend task:

    uv run python -m scripts.copy_workspace_meta --all
    uv run python -m scripts.copy_workspace_meta --all --apply
    uv run python -m scripts.copy_workspace_meta --all --verify
    uv run python -m scripts.copy_workspace_meta --all --reverse --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.config import settings
from core.services.super_agent_flags import env_enabled
from core.dynamodb import get_table, run_in_thread
from core.repositories.paperclip_repo import (
    _PENDING_BRIEF_PREFIX,
    from_meta_item,
    old_row_only,
    to_meta_item,
)

OLD_TABLE = "paperclip-companies"


def _old():
    return get_table(OLD_TABLE)


def _tasks():
    return get_table("tasks")


def _target(old_key: str) -> tuple[str, str]:
    """``(owner_id, sk)`` for an old-table key."""
    if old_key.startswith(_PENDING_BRIEF_PREFIX):
        return old_key.removeprefix(_PENDING_BRIEF_PREFIX), ts.PENDING_BRIEF_SK
    return old_key, ts.META_SK


def _stamp(item: dict) -> datetime | None:
    """When a (snake_case) row last changed: ``updated_at``, or a sentinel's latest event."""
    if item.get("updated_at"):
        return datetime.fromisoformat(item["updated_at"])
    events = [item.get(k) for k in ("submitted_at", "last_attempt_at", "failed_at")]
    return max((datetime.fromisoformat(e) for e in events if e), default=None)


def _copied(item: dict) -> dict:
    return {k: v for k, v in item.items() if not old_row_only(k)}


async def _scan(table, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        resp = await run_in_thread(table.scan, **kwargs)
        items += resp.get("Items", [])
        if not resp.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def _get(table, key: dict) -> dict | None:
    return (await run_in_thread(table.get_item, Key=key, ConsistentRead=True)).get("Item")


async def old_rows(owner_id: str | None) -> list[dict]:
    """Workspace rows (``org_…`` keys) and pending-brief sentinels; never identity rows."""
    if owner_id:
        keys = [owner_id, f"{_PENDING_BRIEF_PREFIX}{owner_id}"]
        return [i for k in keys if (i := await _get(_old(), {"user_id": k}))]
    items = await _scan(
        _old(),
        FilterExpression=Attr("user_id").begins_with("org_") | Attr("user_id").begins_with(_PENDING_BRIEF_PREFIX),
    )
    return sorted(items, key=lambda i: i["user_id"])


async def meta_rows(owner_id: str | None) -> list[dict]:
    if owner_id:
        keys = [{"pk": ts.ws_pk(owner_id), "sk": sk} for sk in (ts.META_SK, ts.PENDING_BRIEF_SK)]
        return [i for k in keys if (i := await _get(_tasks(), k))]
    items = await _scan(_tasks(), FilterExpression=Attr("sk").is_in([ts.META_SK, ts.PENDING_BRIEF_SK]))
    return sorted(items, key=lambda i: (i["pk"], i["sk"]))


async def copy_forward(owner_id: str | None, *, apply: bool) -> list[str]:
    """Write META where it is missing or older than the old row. Returns one line per write."""
    lines = []
    for old in await old_rows(owner_id):
        owner, sk = _target(old["user_id"])
        key = {"pk": ts.ws_pk(owner), "sk": sk}
        current = await _get(_tasks(), key)
        if current is not None:
            old_at, meta_at = _stamp(old), _stamp(from_meta_item(current))
            if old_at is None or (meta_at is not None and old_at <= meta_at):
                continue
        lines.append(f"{owner} {sk}: {'copied' if apply else 'would copy'}")
        if not apply:
            continue
        item = to_meta_item(old, owner, sk)
        cond: dict = {"ConditionExpression": "attribute_not_exists(pk)"}
        if current is not None and current.get("updatedAt"):
            # Only over the META we just compared against: a newer META write since then wins.
            cond = {
                "ConditionExpression": "attribute_not_exists(pk) OR updatedAt = :seen",
                "ExpressionAttributeValues": {":seen": current["updatedAt"]},
            }
        elif current is not None:
            cond = {}  # ponytail: a pending-brief sentinel has no version to fence on; dev-only window
        try:
            await run_in_thread(_tasks().put_item, Item=item, **cond)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            lines[-1] = f"{owner} {sk}: skipped (META changed during the copy)"
    return lines


async def verify(owner_id: str | None) -> list[str]:
    """``missing`` | ``field:<name>`` | ``meta_newer`` per old row, as ``"{owner} {sk}: {problem}"``."""
    problems = []
    for old in await old_rows(owner_id):
        owner, sk = _target(old["user_id"])
        current = await _get(_tasks(), {"pk": ts.ws_pk(owner), "sk": sk})
        if current is None:
            problems.append(f"{owner} {sk}: missing")
            continue
        meta = from_meta_item(current)
        old_at, meta_at = _stamp(old), _stamp(meta)
        if meta_at is not None and (old_at is None or meta_at > old_at):
            problems.append(f"{owner} {sk}: meta_newer")
            continue
        want = _copied(old)
        for name in sorted(set(want) | set(meta)):
            if want.get(name) != meta.get(name):
                problems.append(f"{owner} {sk}: field:{name}")
    return problems


async def copy_reverse(owner_id: str | None, *, apply: bool) -> list[str]:
    """Rollback: META attributes back onto the old rows. An UpdateItem, so the Paperclip-only
    attributes that never left the old row survive; copied attributes META no longer has are removed.
    An old row newer than its META (written after a revert) is never overwritten."""
    lines = []
    for current in await meta_rows(owner_id):
        item = from_meta_item(current)
        key = item.pop("user_id")
        existing = await _get(_old(), {"user_id": key})
        old_at, meta_at = (_stamp(existing), _stamp(item)) if existing else (None, None)
        if old_at is not None and (meta_at is None or old_at > meta_at):
            lines.append(f"{key}: skipped (old row newer)")
            continue
        lines.append(f"{key}: {'restored' if apply else 'would restore'}")
        if not apply:
            continue
        existing = existing or {}
        absent = [k for k in existing if k != "user_id" and not old_row_only(k) and k not in item]
        names = {f"#a{i}": k for i, k in enumerate(item)}
        values = {f":a{i}": v for i, v in enumerate(item.values())}
        expr = "SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(item)))
        if absent:
            names.update({f"#x{i}": k for i, k in enumerate(absent)})
            expr += " REMOVE " + ", ".join(f"#x{i}" for i in range(len(absent)))
        # Fence on the old row we just compared: a write to it since then wins.
        if not existing:
            cond = "attribute_not_exists(user_id)"
        elif existing.get("updated_at"):
            cond, values[":seen"] = "updated_at = :seen", existing["updated_at"]
        else:
            cond = "attribute_not_exists(updated_at)"  # ponytail: a sentinel has no version to fence on
        try:
            await run_in_thread(
                _old().update_item,
                Key={"user_id": key},
                UpdateExpression=expr,
                ConditionExpression=cond,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            lines[-1] = f"{key}: skipped (old row changed during the copy)"
    return lines


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner-id")
    who.add_argument("--all", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--reverse", action="store_true", help="copy META back to the old table")
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod":
        print("refusing: copy_workspace_meta is dev-only (prod runs through spec 09)")
        return 2
    if not env_enabled():
        print("refusing: the super-agent env master is off (no tasks table here)")
        return 2
    if args.reverse and args.verify:
        parser.error("--reverse takes --apply or nothing")

    if args.verify:
        problems = await verify(args.owner_id)
        for line in problems:
            print(line)
        failed = [p for p in problems if not p.endswith(": meta_newer")]
        print(f"verify: {len(failed)} failures, {len(problems) - len(failed)} meta_newer")
        return 1 if failed else 0
    run = copy_reverse if args.reverse else copy_forward
    lines = await run(args.owner_id, apply=args.apply)
    for line in lines:
        print(line)
    print(f"{len(lines)} rows {'written' if args.apply else 'to write (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
