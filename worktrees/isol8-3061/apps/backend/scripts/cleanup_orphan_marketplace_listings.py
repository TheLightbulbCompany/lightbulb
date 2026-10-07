"""Delete DEAD marketplace listing rows (and their S3 artifacts).

Repeated publish-then-takedown cycles, plus publishing an agent that already had
a listing via the generic wizard (which mints a NEW listing_id instead of a new
version), accumulate dead rows that clutter the seller's "My listings" view and
never go away on their own.

This only ever removes rows that are already DEAD:
  - any ``taken_down`` row — an admin pulled the whole listing; every version of
    a taken-down listing carries this status, so deleting them clears the listing,
  - orphan drafts (``artifact_uploaded`` false, older than ``--max-age-days``) —
    e.g. the empty draft left behind when the publish-dedupe guard 409s.

It NEVER deletes a ``published`` or ``retired`` row: a published row backs the live
storefront + every buyer's redeploy, and a retired row resolves past buyers'
entitlements — removing either breaks deploys, so that's the takedown flow's job
(it preserves entitlements), not this cleaner's. ``review`` rows and recent drafts
are also kept. v0 slug sentinels are skipped entirely — see the ``_ver(r) == 0``
guard below.

Removing a *duplicate* published listing (two published listing_ids for one
agent) is deliberately NOT done here — it's unsafe to hard-delete a published
row that may have live entitlements. The publish-dedupe guard prevents new
duplicates; any pre-existing one is a takedown decision, not a cleanup.

For each deleted row it also removes the S3 artifacts at
``listings/{listing_id}/v{version}/`` (workspace.tar.gz / manifest.json /
openclaw-slice.json). Artifacts are keyed per listing_id+version, so removing a
dead row's prefix can't affect any kept listing.

Dry-run by default; ``--apply`` to delete. Reads ``MARKETPLACE_LISTINGS_TABLE`` +
``MARKETPLACE_ARTIFACTS_BUCKET`` from settings, so run it where those are set
(the backend ECS task, or with the right env).

Usage:
    uv run python -m scripts.cleanup_orphan_marketplace_listings --env prod
    uv run python -m scripts.cleanup_orphan_marketplace_listings --env prod --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

# Statuses this cleaner must NEVER hard-delete: a ``published`` row backs the live
# storefront + every buyer redeploy, and a ``retired`` row resolves past buyers'
# entitlements. Either deletion breaks deploys, so it's the takedown flow's job.
_PROTECTED = {"published", "retired"}


def _ts(val) -> float:
    """A created_at / published_at value → epoch seconds (0.0 if absent/unparseable).

    Rows store these as ISO-8601 strings; tolerate a bare epoch too."""
    if val in (None, ""):
        return 0.0
    s = str(val)
    if s.isdigit():
        return float(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _ver(row: dict) -> int:
    """A row's version as int (0 if absent/malformed). DynamoDB's NUMBER type
    enforces numerics at write time, but a hand-edited row shouldn't crash the
    scan — fall back to 0 (treated as a sentinel and skipped)."""
    try:
        return int(row.get("version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _plan_deletions(rows: list[dict], *, max_age_days: int, now: float) -> dict:
    """Partition raw listing rows into keep/delete (pure — no AWS).

    ``rows``: listing rows (dicts with listing_id, version, status, agent_id,
    seller_id, s3_prefix, artifact_uploaded, created_at). Deletes only DEAD rows:
    ``taken_down`` (the whole listing was pulled — every version carries this) and
    orphan drafts (``artifact_uploaded`` false, older than ``max_age_days``). Keeps
    everything else, including every ``published`` / ``retired`` row (those back
    live deploys + buyer entitlements). v0 slug sentinels are ignored.

    Returns ``{"delete": [row+_reason], "keep_summary": [str]}``.
    """
    cutoff = now - max_age_days * 86400
    delete: list[dict] = []
    kept_published: list[str] = []
    for r in rows:
        if _ver(r) == 0:
            continue  # v0 slug sentinel — never a real listing row
        st = r.get("status")
        if st == "published":
            kept_published.append(f"listing {str(r.get('listing_id'))[:8]} agent={str(r.get('agent_id') or '-')[:12]}")
        if st == "taken_down":
            reason = "taken_down"
        elif st == "draft" and not bool(r.get("artifact_uploaded")) and 0 < _ts(r.get("created_at")) < cutoff:
            reason = "orphan-draft"
        else:
            continue  # keep: published / retired / review / recent draft
        delete.append({**r, "_reason": reason})

    # Safety invariant: this cleaner only removes dead rows. A published or retired
    # row must never reach the delete set — fail loudly rather than delete one.
    for d in delete:
        if d.get("status") in _PROTECTED:
            raise AssertionError(
                f"refusing: would delete a {d.get('status')} row {d.get('listing_id')} v{d.get('version')}"
            )

    return {"delete": delete, "keep_summary": sorted(set(kept_published))}


def _s3_keys(row: dict) -> list[str]:
    prefix = row.get("s3_prefix") or f"listings/{row.get('listing_id')}/v{int(row.get('version', 1) or 1)}/"
    return [f"{prefix}workspace.tar.gz", f"{prefix}manifest.json", f"{prefix}openclaw-slice.json"]


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
    parser.add_argument("--apply", action="store_true", help="delete (default: dry-run)")
    parser.add_argument("--max-age-days", type=int, default=30, help="orphan-draft age cutoff")
    parser.add_argument("--agent-id", help="scope the plan to one agent_id")
    parser.add_argument("--seller-id", help="scope the plan to one seller_id")
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
    if args.seller_id:
        rows = [r for r in rows if r.get("seller_id") == args.seller_id or _ver(r) == 0]
    if args.agent_id:
        rows = [r for r in rows if r.get("agent_id") == args.agent_id or _ver(r) == 0]

    plan = _plan_deletions(rows, max_age_days=args.max_age_days, now=datetime.now(timezone.utc).timestamp())

    print(f"=== cleanup ({args.env}) — {len(rows)} rows scanned ===")
    for line in plan["keep_summary"]:
        print(f"  KEEP  {line}")
    print(f"--- {len(plan['delete'])} rows to delete ---")
    s3_total = 0
    for r in plan["delete"]:
        keys = _s3_keys(r)
        s3_total += len(keys)
        print(
            f"  DELETE {str(r.get('listing_id'))[:8]} v{int(r.get('version', 1) or 1)} "
            f"status={r.get('status')} agent={str(r.get('agent_id') or '-')[:12]} ({r['_reason']})"
        )

    if not args.apply:
        print(f"\nDRY-RUN — would delete {len(plan['delete'])} rows + {s3_total} S3 objects. Pass --apply.")
        return 0

    deleted_rows = deleted_objs = errors = 0
    for r in plan["delete"]:
        try:
            table.delete_item(Key={"listing_id": r["listing_id"], "version": int(r.get("version", 1) or 1)})
            deleted_rows += 1
        except Exception as exc:  # noqa: BLE001 — report + continue
            errors += 1
            print(f"  ERR row {str(r.get('listing_id'))[:8]} v{r.get('version')}: {exc}")
            continue
        for key in _s3_keys(r):
            try:
                s3.delete_object(Bucket=bucket, Key=key)
                deleted_objs += 1
            except Exception as exc:  # noqa: BLE001 — S3 may already be gone; report + continue
                print(f"  WARN s3 {key}: {exc}")

    print(f"\nAPPLIED — deleted {deleted_rows} rows, {deleted_objs} S3 objects, {errors} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
