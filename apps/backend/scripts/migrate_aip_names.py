"""Re-mint per-owner Bedrock application inference profiles under the new name.

WHY A SCRIPT AND NOT A DEPLOY
-----------------------------
Bedrock has NO rename operation for application inference profiles — only
create, delete, get, list. So "renaming" is really: mint a new profile (which
gets a new, opaque ARN), repoint everything that stored the old ARN, then delete
the old profile.

Changing ``_aip_name`` alone does nothing for an existing owner:
``ensure_owner_aips`` short-circuits on ``if tier in arns: continue``, i.e. it is
idempotent on the owner's STORED ARN MAP, not on the name. Only an owner whose
map is empty mints fresh.

WHAT THIS DOES, PER OWNER
-------------------------
1. Clears ``bedrock_aip_arns`` on the containers row.
2. Deletes that owner's rows from the reverse table (its PRIMARY KEY is the arn,
   so a stale row is not overwritten by the new mint — it lingers and keeps
   resolving a deleted profile to this owner).

It does NOT mint. The next provision does that, which is also what repoints the
gate-token row, the task-def model env, and openclaw.json on EFS — five
persistence sites that a hand-rolled mint here would leave inconsistent.

ORDER MATTERS, AND IT IS NOT SYMMETRIC
--------------------------------------
* STOPPED owners: clear now, and their next wake mints under the new name. The
  window costs nothing because nothing is running.
* RUNNING owners: clearing leaves the gate-token row naming an ARN that is about
  to be deleted, so their inference breaks until a provision completes. Do these
  ONE AT A TIME, reprovision immediately, and verify before moving on. This
  script refuses them unless --include-running is passed.

Old profiles are NOT deleted here. Delete them only after the owner has minted
new ones and been verified — see --report.

USAGE
-----
    cd apps/backend
    uv run python scripts/migrate_aip_names.py --env prod              # plan
    uv run python scripts/migrate_aip_names.py --env prod --apply      # stopped only
    uv run python scripts/migrate_aip_names.py --env prod --apply --include-running \
        --only-owner user_XXX                                          # one paying owner
    uv run python scripts/migrate_aip_names.py --env prod --report     # who is on which name
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _prefix(env: str) -> str:
    return os.environ.get("DYNAMODB_TABLE_PREFIX") or f"lightbulb-{env}-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--apply", action="store_true", help="write; default is a read-only plan")
    ap.add_argument(
        "--include-running",
        action="store_true",
        help="also clear owners whose container is running (breaks their inference until reprovisioned)",
    )
    ap.add_argument("--only-owner", default="", help="restrict to a single owner_id")
    ap.add_argument("--report", action="store_true", help="just show which profiles are on which name")
    args = ap.parse_args()

    ddb = boto3.resource("dynamodb", region_name=args.region)
    bedrock = boto3.client("bedrock", region_name=args.region)
    pfx = _prefix(args.env)
    containers = ddb.Table(f"{pfx}containers")
    reverse = ddb.Table(f"{pfx}bedrock-aip-owners")

    live = {}
    for p in bedrock.get_paginator("list_inference_profiles").paginate(typeEquals="APPLICATION"):
        for s in p.get("inferenceProfileSummaries", []):
            live[s["inferenceProfileArn"]] = s["inferenceProfileName"]

    old_pfx, new_pfx = f"isol8-{args.env}-", f"lightbulb-{args.env}-"
    if args.report:
        old = [n for n in live.values() if n.startswith(old_pfx)]
        new = [n for n in live.values() if n.startswith(new_pfx)]
        print(f"  on the OLD name ({old_pfx}*): {len(old)}")
        print(f"  on the NEW name ({new_pfx}*): {len(new)}")
        for n in sorted(old):
            print(f"    old: {n}")
        return 0

    rows = containers.scan().get("Items", [])
    if args.only_owner:
        rows = [r for r in rows if r.get("owner_id") == args.only_owner]

    stats = {"cleared": 0, "skipped_running": 0, "already": 0, "planned": 0}
    for r in rows:
        owner = r.get("owner_id")
        arns = r.get("bedrock_aip_arns") or {}
        status = r.get("status", "")
        if not arns:
            stats["already"] += 1
            continue
        # already on the new name? nothing to do
        if all(live.get(a, "").startswith(new_pfx) for a in arns.values() if a in live):
            stats["already"] += 1
            continue
        if status == "running" and not args.include_running:
            print(f"  SKIP    {owner}  running — needs --include-running and an immediate reprovision")
            stats["skipped_running"] += 1
            continue
        if not args.apply:
            print(f"  plan    {owner}  status={status}  tiers={sorted(arns)}")
            stats["planned"] += 1
            continue
        for arn in arns.values():
            try:
                reverse.delete_item(Key={"aip_arn": arn})
            except Exception as e:  # noqa: BLE001 — report and continue the sweep
                print(f"  WARN    {owner}  reverse delete failed for {arn[-12:]}: {e}")
        containers.update_item(
            Key={"owner_id": owner},
            UpdateExpression="REMOVE bedrock_aip_arns",
        )
        print(f"  cleared {owner}  status={status}  (next provision mints under {new_pfx})")
        stats["cleared"] += 1

    print("\n" + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
