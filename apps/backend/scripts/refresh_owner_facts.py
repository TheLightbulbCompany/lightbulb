"""Rewrite ``lightbulb-facts.json`` into every provisioned owner's Hermes profiles.

``owner_facts.write_owner_facts`` runs on provision, provider switch and subscription webhooks, so a
NEW fact (e.g. ``app_url``, #2978) reaches an existing workspace only when one of those next fires.
This is the re-runnable ops entry point that pushes the current facts to every owner now, through
the same writer (it bumps each profile's ``.revision``, so the bridge re-pulls before the agent's
next turn). No image roll, no container restart.

Runs INSIDE the backend task (ECS exec), like ``reapply_hermes_skills_dirs``:

    uv run python -m scripts.refresh_owner_facts --owner-id <org> [--dry-run]
    uv run python -m scripts.refresh_owner_facts --all [--dry-run]

Safe to re-run, but not free: every run rewrites each profile's facts and bumps its ``.revision``
(so every bridge re-pulls on its next turn), whether or not anything changed. Sequential.
``write_owner_facts`` is best-effort and never raises (it logs a warning per skipped owner), so a
"refreshed" line and exit code 0 do not prove the write landed; read the log for skips.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.repositories import container_repo
from core.services.runtime_transport import runtime_arn
from core.services.owner_facts import write_owner_facts


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    # No runtime ARN = no provisioned profiles to write into.
    return sorted(r["owner_id"] for r in rows if runtime_arn(r) and r.get("owner_id"))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    for owner_id in await _targets(args):
        if args.dry_run:
            print(f"{owner_id}: would-refresh")
            continue
        await write_owner_facts(owner_id)
        print(f"{owner_id}: refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
