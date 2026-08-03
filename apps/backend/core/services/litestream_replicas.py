"""S3 replica-prefix lifecycle for the O6 local-SQLite container layout.

Litestream replicates each owner's hot SQLite DBs to
``s3://{LITESTREAM_BUCKET}/{owner_id}/...`` and the container's boot script
RESTORES whatever exists under the owner's prefix. Any flow that deletes the
corresponding durable state must therefore delete the replica prefix too, or
the next boot resurrects it:

- account wipe / debug reprovision without the prefix delete restores the
  pre-wipe sessions, device-trust rows, and ``auth_profile_store`` rows
  (live OAuth refresh tokens) — a credential-retention bug, and it breaks
  the documented clean-slate reprovision contract.
- agent delete without the ``agents/{agent_id}/`` delete re-materializes the
  deleted agent's DB on every boot, forever (unbounded S3 + local-disk
  growth on hire/delete churn).

Inert when ``LITESTREAM_BUCKET`` is unset (EFS layout). Failures are logged
loudly + metered but never raised: every caller is a cleanup path whose
primary operation already succeeded, and a crashed wipe would be worse than
a logged resurrection risk.
"""

import asyncio
import logging

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


def _delete_prefix_blocking(bucket: str, prefix: str) -> int:
    import boto3

    s3 = boto3.client("s3")
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not keys:
            continue
        # DeleteObjects is partial-success by design: per-key failures (object
        # lock, retention, a key-scoped authz denial) come back in ``Errors``
        # WITHOUT raising. Counting the request as a success there would report
        # a clean wipe while the surviving objects — including agent DBs whose
        # auth_profile_store rows hold live OAuth tokens — get restored on the
        # owner's next boot. Quiet mode still returns Errors.
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        errors = resp.get("Errors") or []
        if errors:
            sample = "; ".join(f"{e.get('Key')}: {e.get('Code')}" for e in errors[:3])
            raise RuntimeError(f"{len(errors)} of {len(keys)} objects not deleted ({sample})")
        deleted += len(keys)
    return deleted


async def delete_replica_prefix(owner_id: str, *, agent_id: str | None = None) -> bool:
    """Delete ``{owner_id}/`` (or ``{owner_id}/agents/{agent_id}/``) replicas.

    Returns True when the prefix is gone (including the nothing-to-delete
    case), False on failure — callers may log but must not abort their own
    cleanup on a False.
    """
    bucket = settings.LITESTREAM_BUCKET
    if not bucket:
        return True  # EFS layout — no replicas exist.
    if agent_id and ("/" in agent_id or ".." in agent_id or not agent_id.strip()):
        logger.warning("delete_replica_prefix: refusing unsafe agent_id %r", agent_id)
        return False
    prefix = f"{owner_id}/agents/{agent_id}/" if agent_id else f"{owner_id}/"
    try:
        deleted = await asyncio.to_thread(_delete_prefix_blocking, bucket, prefix)
        logger.info(
            "deleted_litestream_replicas",
            extra={"owner_id": owner_id, "prefix": prefix, "objects": deleted},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — cleanup path, never raise
        # NOT benign: the next container boot will RESTORE whatever survived
        # under this prefix. Loud + metered so it is observable and retried.
        logger.error(
            "delete_replica_prefix FAILED for %s (prefix %s): %s — stale replicas "
            "will be restored on the owner's next container boot",
            owner_id,
            prefix,
            exc,
        )
        put_metric("litestream.replica_delete_failed")
        return False
