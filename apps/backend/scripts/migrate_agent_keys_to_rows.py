"""One-off: move the per-agent Paperclip API keys from Secrets Manager onto the workspace rows.

Each ``lightbulb/<env>/paperclip-agent-keys/<owner>/<agent>`` secret is copied into the owner's
``paperclip-companies`` row (``agent_keys.<agent>``, Fernet) by ``PaperclipRepo.put_agent_key``,
then read back and compared. Runs INSIDE the backend task (ECS exec), like
``roll_hermes_runtimes``; never on a laptop (the values are customer credentials):

    uv run python -m scripts.migrate_agent_keys_to_rows --dry-run        # counts only, reads no value
    uv run python -m scripts.migrate_agent_keys_to_rows                  # copy + verify, deletes nothing
    uv run python -m scripts.migrate_agent_keys_to_rows --delete-migrated  # schedule deletion (7-day window)

``--owner-id <org>`` limits any mode to one owner. Copy is safe to re-run. ``--delete-migrated``
schedules deletion of a secret ONLY when the row holds the identical key; every other secret is
left alone (a secret whose owner has no workspace row is on the cleanup lists in
``docs/research/2026-10-27-secrets-inventory.md``). A scheduled deletion is restorable with
``aws secretsmanager restore-secret`` for 7 days. No value is ever printed.

Delete this script (and the Secrets Manager fallback in ``paperclip_api_key``) once the prefix is empty.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.services.paperclip_api_key import _env_prefix, _owner_prefix, _repo


def _list_names(prefix: str) -> list[str]:
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    names: list[str] = []
    for page in sm.get_paginator("list_secrets").paginate(Filters=[{"Key": "name", "Values": [prefix]}]):
        names += [s["Name"] for s in page.get("SecretList", []) if s.get("Name", "").startswith(prefix)]
    return sorted(names)


def _get_value(name: str) -> str:
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    return sm.get_secret_value(SecretId=name)["SecretString"]


def _schedule_delete(name: str) -> None:
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    sm.delete_secret(SecretId=name, RecoveryWindowInDays=7)


async def _one(name: str, mode: str, has_row: dict[str, bool]) -> str:
    owner_id, _, agent_id = name.removeprefix(_env_prefix()).partition("/")
    if not agent_id or "/" in agent_id:
        return "unparsed"
    if owner_id not in has_row:
        has_row[owner_id] = (await _repo().get_workspace(owner_id, consistent_read=True)) is not None
    if not has_row[owner_id]:
        return "no_row"
    if mode == "dry-run":
        # Decrypted in memory only to test presence; never printed.
        return "in_row" if await _repo().get_agent_key(owner_id, agent_id) is not None else "to_copy"

    value = await run_in_thread(_get_value, name)
    stored = await _repo().get_agent_key(owner_id, agent_id)
    if mode == "delete-migrated":
        if stored != value:
            return "kept_not_migrated"
        await run_in_thread(_schedule_delete, name)
        return "deletion_scheduled"
    if stored is not None:
        # The row is the reader's first choice, so a different key there is the live one.
        return "verified" if stored == value else "row_differs"
    await _repo().put_agent_key(owner_id, agent_id, value)
    return "copied" if await _repo().get_agent_key(owner_id, agent_id) == value else "verify_failed"


async def run(*, mode: str, owner_id: str | None = None) -> Counter:
    prefix = _owner_prefix(owner_id) if owner_id else _env_prefix()
    names = await run_in_thread(_list_names, prefix)
    counts: Counter = Counter(secrets=len(names))
    has_row: dict[str, bool] = {}
    for name in names:
        try:
            result = await _one(name, mode, has_row)
        except Exception as exc:  # one bad secret must not stop the sweep
            result = "error"
            print(f"{name}: error {type(exc).__name__}")
        counts[result] += 1
        if mode != "dry-run" and result not in ("copied", "verified", "deletion_scheduled"):
            print(f"{name}: {result}")
    counts["owners"] = len(has_row)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--delete-migrated", action="store_true")
    parser.add_argument("--owner-id")
    args = parser.parse_args(argv)
    mode = "dry-run" if args.dry_run else "delete-migrated" if args.delete_migrated else "copy"
    counts = asyncio.run(run(mode=mode, owner_id=args.owner_id))
    print(f"{mode}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts["error"] or counts["verify_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
