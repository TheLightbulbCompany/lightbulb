"""One-shot: give every trial_local workspace armed under the old 3-day window signup + 30 days,
and re-arm its nightly CEO loop (owner ruling 2026-09-22). DELETE after it has run in prod.

The per-poll heal (``ensure_workspace_trial_default``) only reaches owners who open the app; the
expired cohort is exactly who doesn't. Runs INSIDE the backend task (ECS exec), like
``roll_hermes_runtimes``:

    uv run python -m scripts.extend_short_trials --dry-run
    uv run python -m scripts.extend_short_trials

Prints one line per trial row: ``extended <old> -> <new>``, ``ok`` (already 30 days) or
``would-extend`` in a dry run. Idempotent: the write is conditioned on the old ``trial_end``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime

from core.repositories import billing_repo
from core.repositories.billing_repo import _TRIAL_LOCAL_DURATION_SECONDS
from core.services.user_provision import arm_nightly_ceo


async def main(dry_run: bool) -> int:
    failed = 0
    now = int(time.time())
    for row in await billing_repo.scan_all():
        owner = row["owner_id"]
        if row.get("subscription_status") != "trial_local" or row.get("trial_end") is None:
            continue
        old = int(row["trial_end"])
        try:
            target = int(datetime.fromisoformat(row["created_at"]).timestamp()) + _TRIAL_LOCAL_DURATION_SECONDS
        except (KeyError, TypeError, ValueError):
            print(f"{owner} skip-no-created_at {old}")
            continue
        if dry_run:
            print(f"{owner} {'would-extend' if target > old else 'ok'} {old} -> {max(old, target)}")
            continue
        try:
            new = await billing_repo.extend_short_trial(owner, row)
            print(f"{owner} {'extended' if new > old else 'ok'} {old} -> {new}")
            if new > now:
                # Idempotent and cached: arming every live trial (not only rows extended on this
                # run) is what makes a re-run retry an arm that failed after its extension landed.
                await arm_nightly_ceo(owner)
        except Exception as exc:  # noqa: BLE001 — one owner's failure must not stop the sweep
            failed += 1
            print(f"{owner} FAILED {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().dry_run)))
