"""Backfill the per-owner "owner-litestream-s3" inline policy onto every
existing owner task role.

``ensure_owner_bedrock_role`` puts the policy from the PROVISION path only,
so owners provisioned before an env flipped to the O6 local-sqlite layout
never receive it. Their containers would then be force-applied onto the O6
boot chain, where litestream — the boot supervisor — AccessDenies on S3 and
the task crash-loops. RUN THIS BEFORE ANY FLEET FORCE-APPLY/RESTART WAVE in
a freshly-flipped env.

Re-runnable ops tool (idempotent puts). Requires LITESTREAM_BUCKET in the
environment (matching the target env's replica bucket) plus the usual AWS
credentials/DDB env the backend scripts use.

Usage:
    uv run python scripts/ensure_litestream_s3_policies.py [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import sys

sys.path.insert(0, ".")

from core.billing.bedrock_aip import build_owner_litestream_s3_policy  # noqa: E402
from core.config import settings  # noqa: E402
from core.repositories import container_repo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ensure_litestream_s3_policies")


async def main(dry_run: bool) -> int:
    bucket = settings.LITESTREAM_BUCKET
    if not bucket:
        logger.error("LITESTREAM_BUCKET is unset — this env is not on the O6 layout; nothing to do.")
        return 2

    import boto3

    iam = boto3.client("iam")
    rows = await container_repo.scan_all()
    done = missing_role = failed = 0
    for row in rows:
        owner_id = row.get("owner_id")
        role_arn = row.get("bedrock_role_arn")
        if not owner_id:
            continue
        if not role_arn:
            # Provision mints the role (and, with the bucket set, this policy)
            # on the owner's next ensure — nothing to backfill yet.
            missing_role += 1
            logger.info("skip %s: no bedrock_role_arn on row", owner_id)
            continue
        role_name = role_arn.rsplit("/", 1)[-1]
        doc = build_owner_litestream_s3_policy(owner_id, bucket)
        if dry_run:
            logger.info("DRY RUN would put owner-litestream-s3 on %s", role_name)
            done += 1
            continue
        try:
            await asyncio.to_thread(
                iam.put_role_policy,
                RoleName=role_name,
                PolicyName="owner-litestream-s3",
                PolicyDocument=json.dumps(doc),
            )
            done += 1
            logger.info("put owner-litestream-s3 on %s", role_name)
        except Exception as exc:  # noqa: BLE001 — keep sweeping, report at end
            failed += 1
            logger.error("FAILED %s (%s): %s", owner_id, role_name, exc)

    logger.info("done=%d missing_role=%d failed=%d", done, missing_role, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
