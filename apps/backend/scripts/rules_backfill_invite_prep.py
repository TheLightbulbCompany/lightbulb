"""Create the invite-prep built-in rule (observe) for one workspace's connected Google accounts (wave2-S Task 7).

    uv run python -m scripts.rules_backfill_invite_prep --owner org_… [--apply]

Dry run by default: prints what it would create. `--apply` writes. Dev only: refuses when ENVIRONMENT is prod or
the workspace's super-agent switch is off. Safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.config import settings
from core.repositories import google_accounts_repo
from core.services import rules, rules_builtins, super_agent_flags


async def run(owner: str, *, apply: bool = False) -> int:
    if (settings.ENVIRONMENT or "").lower() == "prod":
        print("refusing: ENVIRONMENT is prod", file=sys.stderr)
        return 2
    if not await super_agent_flags.flags_for(owner):
        print(f"refusing: super-agent switch is off for {owner}", file=sys.stderr)
        return 2
    if not apply:
        for account in await google_accounts_repo.list_accounts(owner):
            print(f"would ensure {rules.invite_prep_id(account['email'])} for {account['email']}")
        print("dry run; pass --apply to write", file=sys.stderr)
        return 0
    for sid in await rules_builtins.ensure_invite_prep(owner):
        print(sid)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--apply", action="store_true", help="write; without it the script only prints")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.owner, apply=args.apply)))


if __name__ == "__main__":
    main()
