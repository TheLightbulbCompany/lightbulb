"""Backfill requirements for existing marketplace listings.

Published listings whose artifacts predate the requirements scanner have no
requirements block, so their listing pages disclose nothing about what the
agent needs and the hire gate has no requires.credentials to read. This script
re-scans each published listing's ``workspace.tar.gz`` from S3 and merges the
computed requirements into every place a consumer reads:

  - the S3 ``manifest.json`` (the storefront listing detail reads this),
  - the DDB row's ``manifest_json`` (the hire gate + deploy read this), with
    ``manifest_sha256`` recomputed so the two stay consistent,
  - a denormalized ``requirements_summary`` row attribute for fast render.

Only ``manifest.json`` metadata is rewritten — the immutable ``workspace.tar.gz``
agent bytes are never touched. Dry-run by default; pass ``--apply`` to write.
Idempotent: a second run recomputes the same fragment and writes it again.

Usage:
    uv run python -m scripts.backfill_marketplace_requirements            # dry-run
    uv run python -m scripts.backfill_marketplace_requirements --apply
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path

from core.services.marketplace.requirements import (
    build_manifest_requirements,
    load_effective_config,
    requirements_scan,
)


def scan_tarball(tarball_bytes: bytes) -> dict:
    """Extract a listing's ``workspace.tar.gz`` and scan it into a manifest
    fragment ``{requirements, scan_incomplete, requires}`` — the same shape
    ``agent_export`` freezes at publish, ready to merge into the manifest.

    The agent tree is at the tar root (``./openclaw.json``, ``./pipelines``,
    ``./scripts``), so the extracted root IS the agent dir.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tf:
            tf.extractall(root, filter="data")
        effective_cfg = load_effective_config(root, {})
        reqs, scan_incomplete = requirements_scan(effective_cfg, root)
        return build_manifest_requirements(reqs, scan_incomplete)


def _normalize_manifest(raw) -> dict:
    """A listing row's ``manifest_json`` is a DDB Map (dict) for artifact-backed
    rows, but a JSON string on the legacy create_draft path. Accept both."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


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
        s3_prefix = row.get("s3_prefix") or f"listings/{listing_id}/v{version}/"
        tar_key = f"{s3_prefix}workspace.tar.gz"
        manifest_key = f"{s3_prefix}manifest.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=tar_key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 — report + continue
            print(f"  SKIP {listing_id} v{version}: cannot read {tar_key}: {exc}")
            continue

        fragment = scan_tarball(body)
        summary = marketplace_service.summarize_requirements(fragment)
        print(
            f"  {listing_id} v{version}: {summary['counts']} "
            f"providers={summary['requires_providers']} blocking={summary['has_blocking']}"
        )
        if not args.apply:
            continue

        # Merge the fragment into the existing manifest.json (preserving
        # safety_flags etc.), falling back to the row's manifest_json if the S3
        # object is missing/unreadable.
        try:
            existing = json.loads(s3.get_object(Bucket=bucket, Key=manifest_key)["Body"].read())
            if not isinstance(existing, dict):
                existing = _normalize_manifest(row.get("manifest_json"))
        except Exception:  # noqa: BLE001 — fall back to the row's copy
            existing = _normalize_manifest(row.get("manifest_json"))
        existing.update(fragment)

        # manifest_sha256 covers manifest.json (see _upload_artifact_to_s3), so
        # recompute it over the same canonical bytes we write to S3.
        manifest_bytes = json.dumps(existing, sort_keys=True).encode("utf-8")
        sha = hashlib.sha256(manifest_bytes).hexdigest()
        s3.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=manifest_bytes,
            ContentType="application/json",
        )
        marketplace_service._listings_table().update_item(
            Key={"listing_id": listing_id, "version": version},
            UpdateExpression=("SET manifest_json = :mj, manifest_sha256 = :sha, requirements_summary = :s"),
            ExpressionAttributeValues={":mj": existing, ":sha": sha, ":s": summary},
        )
    if not args.apply:
        print("\n(dry-run — pass --apply to write manifest.json + row updates)")


if __name__ == "__main__":
    main()
