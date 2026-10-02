"""Inspect, or set by hand, Paperclip's instance-wide experimental flags.

The backend applies ``core.services.paperclip_instance_flags.DESIRED_FLAGS`` at
startup, so a deploy converges the instance and this script is NOT part of a
rollout. It is the manual door onto the same code: see what is stored, or apply
without waiting for a deploy. That module's docstring has the table, the reasons
for direct SQL, and why a partial object is safe.

Runs from a laptop with the admin profile, like ``recover_orphan_paperclip_member``
(the cluster and secret are addressed by name from ``--env``, so nothing needs
the backend task's environment). Dry-run by default; ``--apply`` writes.

    aws sso login --profile isol8-admin
    cd apps/backend
    export CLERK_ISSUER=https://clerk.thelightbulb.company  # core.config refuses to load without one
    AWS_PROFILE=isol8-admin uv run python -m scripts.set_paperclip_instance_flags --env dev
    AWS_PROFILE=isol8-admin uv run python -m scripts.set_paperclip_instance_flags --env dev --apply
    AWS_PROFILE=isol8-admin uv run python -m scripts.set_paperclip_instance_flags --env prod
    AWS_PROFILE=isol8-admin uv run python -m scripts.set_paperclip_instance_flags --env prod --apply

Prints the stored flags and one line per desired flag, and exits non-zero on any
failure. Aurora scales to zero, so a first call against a paused cluster can
fail while it resumes: run it again.
"""

from __future__ import annotations

import argparse
import json
import sys

from core.services.paperclip_instance_flags import DESIRED_FLAGS, database, plan_changes, read, write
from core.services.paperclip_sql import rds_data_client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client = rds_data_client()
    db = database(args.env)

    current = read(client, db)
    if current is None:
        print("instance_settings row: missing (the write creates it)")
    else:
        print(f"Stored experimental: {json.dumps(current, sort_keys=True)}")
    changes = plan_changes(current, DESIRED_FLAGS)
    for key, want in DESIRED_FLAGS.items():
        if key in changes:
            stored = changes[key][0]
            print(f"{key}: {'(unset)' if stored is None else json.dumps(stored)} -> {json.dumps(want)}")
        else:
            print(f"{key}: {json.dumps(want)} (already set)")

    if not changes:
        print("\nNothing to change.")
        return
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to write.")
        return

    write(client, db, DESIRED_FLAGS)
    after = read(client, db)
    print(f"\nStored experimental now: {json.dumps(after, sort_keys=True)}")
    if plan_changes(after, DESIRED_FLAGS):
        sys.exit("FAILED: the row does not carry the desired flags after the write.")


if __name__ == "__main__":
    main()
