"""Backfill ``manifest.requirements`` for existing marketplace listings.

Published listings whose artifacts predate the requirements scanner have no
requirements block, so their listing pages show nothing about what the agent
needs. This script re-scans each published listing's ``workspace.tar.gz`` from
S3 and writes the computed requirements back without a seller re-export.

It writes a ``requirements.json`` sidecar next to the version's ``manifest.json``
(immutable artifacts are never rewritten in place) and updates the listing row's
denormalized summary. Dry-run by default; pass ``--apply`` to write. Idempotent.

Usage:
    uv run python -m scripts.backfill_marketplace_requirements            # dry-run
    uv run python -m scripts.backfill_marketplace_requirements --apply
"""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import tempfile
from pathlib import Path

from core.services.marketplace.requirements import requirements_scan


def scan_tarball(tarball_bytes: bytes) -> list[dict]:
    """Extract a listing's ``workspace.tar.gz`` and scan it for requirements.

    The agent tree is at the tar root (``./openclaw.json``, ``./pipelines``,
    ``./scripts``), so the extracted root IS the agent dir.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tf:
            tf.extractall(root, filter="data")
        effective_cfg: dict = {}
        cfg_path = root / "openclaw.json"
        if cfg_path.exists():
            try:
                effective_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                effective_cfg = {}
        reqs, _ = requirements_scan(effective_cfg, root)
        return [r.to_manifest() for r in reqs]


def _published_listings() -> list[dict]:
    from core.services import marketplace_service

    table = marketplace_service._listings_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [it for it in items if it.get("status") == "published"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()

    import boto3

    from core.config import settings
    from core.services import marketplace_service

    s3 = boto3.client("s3")
    bucket = settings.MARKETPLACE_ARTIFACTS_BUCKET

    for row in _published_listings():
        listing_id = row.get("listing_id")
        version = int(row.get("version", 1))
        key = f"listings/{listing_id}/v{version}/workspace.tar.gz"
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 — report + continue
            print(f"  SKIP {listing_id} v{version}: cannot read {key}: {exc}")
            continue
        reqs = scan_tarball(body)
        summary = marketplace_service.summarize_requirements({"requirements": reqs})
        print(
            f"  {listing_id} v{version}: {summary['counts']} "
            f"providers={summary['requires_providers']} blocking={summary['has_blocking']}"
        )
        if args.apply:
            s3.put_object(
                Bucket=bucket,
                Key=f"listings/{listing_id}/v{version}/requirements.json",
                Body=json.dumps(reqs, sort_keys=True).encode("utf-8"),
                ContentType="application/json",
            )
            marketplace_service._listings_table().update_item(
                Key={"listing_id": listing_id, "version": version},
                UpdateExpression="SET requirements_summary = :s",
                ExpressionAttributeValues={":s": summary},
            )
    if not args.apply:
        print("\n(dry-run — pass --apply to write requirements.json sidecars + row summaries)")


if __name__ == "__main__":
    main()
