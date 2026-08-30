"""One-shot: strip safety-scan snippets out of ALREADY-PUBLISHED manifests.

``safety_flags[].snippet`` is 80 raw characters of the seller's own source
line — by construction the field most likely to hold a credential, plus the
file path and line number it came from. It was persisted into every artifact
manifest and served verbatim by the unauthenticated
``GET /marketplace/listings/{id}`` route under a public cache header.

The write path no longer persists it and the read path scrubs it on the way
out, but the bytes are still at rest in two places per listing version:

  * the ``manifest_json`` attribute on the DynamoDB listing row, and
  * ``s3://$MARKETPLACE_ARTIFACTS_BUCKET/{s3_prefix}manifest.json``.

Published artifacts are immutable and versioned, so nothing rewrites them on
its own. This does — in place, dropping ONLY the ``snippet`` key. Every other
manifest field (including each flag's pattern/severity/file/line) is left
alone, so moderation history and the storefront render unchanged.
``manifest_sha256`` is recomputed to stay consistent with the new S3 bytes.

Dry-run by default; ``--apply`` to write. Targets ``MARKETPLACE_LISTINGS_TABLE``
+ ``MARKETPLACE_ARTIFACTS_BUCKET`` from settings, so run it with the backend
env loaded (the backend ECS task, or the same env exported locally — importing
``core.services`` binds the full ``Settings`` object, ``CLERK_ISSUER``
included, same as the other marketplace scripts here).

Usage:
    cd apps/backend
    MARKETPLACE_LISTINGS_TABLE=lightbulb-prod-marketplace-listings \
    MARKETPLACE_ARTIFACTS_BUCKET=lightbulb-prod-marketplace-artifacts \
      uv run python -m scripts.scrub_manifest_snippets --env prod
    ... --apply

Delete this script (and its test) once it has run against dev + prod — git
history is the operational record.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from core.services import marketplace_safety


def scrub_row_manifest(row: dict) -> tuple[object, bool]:
    """``(new manifest_json value, changed?)`` for one listing row.

    ``manifest_json`` is a JSON **string** on draft rows (``put_draft_v1``
    writes ``{"S": ...}``) and a DynamoDB **Map** on every row that went
    through ``update_artifact_fields``. Preserve whichever shape the row
    already has — flipping it would break the readers that expect one.

    An unparseable string is left untouched: this script's job is dropping a
    key, not repairing malformed rows.
    """
    raw = row.get("manifest_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw, False
        scrubbed = marketplace_safety.manifest_without_snippets(parsed)
        return (raw, False) if scrubbed is parsed else (json.dumps(scrubbed), True)
    scrubbed = marketplace_safety.manifest_without_snippets(raw)
    return scrubbed, scrubbed is not raw


def scrub_manifest_bytes(body: bytes) -> bytes | None:
    """Rewritten ``manifest.json`` bytes, or ``None`` when nothing to strip.

    ``sort_keys=True`` matches ``marketplace_service._upload_artifact_to_s3``
    so a re-run is a byte-level no-op.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    scrubbed = marketplace_safety.manifest_without_snippets(parsed)
    if scrubbed is parsed:
        return None
    return json.dumps(scrubbed, sort_keys=True).encode("utf-8")


def _scan_all(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod"], required=True, help="label (for the log header)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args()

    import boto3

    from core.config import settings

    table_name = settings.MARKETPLACE_LISTINGS_TABLE
    bucket = settings.MARKETPLACE_ARTIFACTS_BUCKET
    if not table_name or not bucket:
        print("ERROR: MARKETPLACE_LISTINGS_TABLE / MARKETPLACE_ARTIFACTS_BUCKET unset")
        return 2

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    s3 = boto3.client("s3")

    rows = _scan_all(table)
    print(f"=== scrub manifest snippets ({args.env}) — {len(rows)} rows scanned ===")

    ddb_writes = s3_writes = errors = 0
    for row in rows:
        listing_id, version = row.get("listing_id"), row.get("version")
        short = f"{str(listing_id)[:8]} v{version} status={row.get('status')}"

        new_manifest, row_dirty = scrub_row_manifest(row)

        new_body = None
        prefix = row.get("s3_prefix")
        if prefix:
            key = f"{prefix}manifest.json"
            try:
                new_body = scrub_manifest_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            except Exception as exc:  # noqa: BLE001 — missing/unreadable object: report + continue
                print(f"  WARN s3 read {key}: {exc}")

        if not row_dirty and new_body is None:
            continue
        print(f"  SCRUB {short} row={'yes' if row_dirty else 'no'} s3={'yes' if new_body is not None else 'no'}")
        if not args.apply:
            ddb_writes += int(row_dirty)
            s3_writes += int(new_body is not None)
            continue

        # S3 first: the row carries the sha of the S3 bytes, so a crash between
        # the two leaves a stale sha rather than a sha pointing at bytes that
        # were never written.
        if new_body is not None:
            try:
                s3.put_object(Bucket=bucket, Key=f"{prefix}manifest.json", Body=new_body)
                s3_writes += 1
            except Exception as exc:  # noqa: BLE001 — report + continue
                errors += 1
                print(f"  ERR s3 {short}: {exc}")
                new_body = None

        # Only touch the attributes that actually changed — a blanket
        # ``SET manifest_json = :mj`` would write NULL onto a row that has no
        # manifest at all but whose S3 object needed the scrub.
        sets, values = [], {}
        if row_dirty:
            sets.append("manifest_json = :mj")
            values[":mj"] = new_manifest
        if new_body is not None:
            sets.append("manifest_sha256 = :sha")
            values[":sha"] = hashlib.sha256(new_body).hexdigest()
        if sets:
            try:
                table.update_item(
                    Key={"listing_id": listing_id, "version": int(version)},
                    UpdateExpression="SET " + ", ".join(sets),
                    ExpressionAttributeValues=values,
                )
                ddb_writes += 1
            except Exception as exc:  # noqa: BLE001 — report + continue
                errors += 1
                print(f"  ERR row {short}: {exc}")

    if not args.apply:
        print(f"\nDRY-RUN — would rewrite {ddb_writes} rows + {s3_writes} S3 manifests. Pass --apply.")
        return 0
    print(f"\nAPPLIED — rewrote {ddb_writes} rows, {s3_writes} S3 manifests, {errors} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
