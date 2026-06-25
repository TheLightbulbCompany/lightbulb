"""Backfill stale slug ``agent_id`` values on marketplace listings to UUIDs.

Listings created before agent workspaces became UUID-keyed stored ``agent_id``
as a slugified display name (e.g. ``"vera"``) rather than the live workspace
directory name (e.g. ``"f9fc524c-acef-4467-8387-91ac7cbd00ef"``). Such rows are
"orphaned": their republish/export fails with ``agent dir not found`` because
``workspaces/vera/`` never existed. ``agent_export`` now resolves the slug
forward at export time, but this script heals the *stored* column so the orphan
class disappears even for listings that are never republished (and so the
publish wizard's edit-mode locks the correct agent).

For each listing row whose ``agent_id`` is a slug that resolves to exactly one
live workspace UUID, it rewrites the top-level ``agent_id`` column slug → UUID.
Rows already keyed by a live UUID are skipped; ambiguous or unresolvable slugs
are skipped with a reason (left for manual handling, never guessed). Only the
``agent_id`` column is touched — the immutable ``workspace.tar.gz`` bytes and the
S3 ``manifest.json`` are never rewritten (the manifest's embedded ``agent_id``
self-heals on the next republish via the export forward-fix).

Resolution reads EFS (``workspaces/`` dirs + the seller's ``openclaw.json``), so
this MUST run INSIDE the backend ECS task (which has the EFS mount), the same
way ``backfill_paperclip_for_legacy_users`` is run — not from a laptop.

Dry-run by default; pass ``--apply`` to write. Idempotent + race-safe: the
``agent_id in live_dirs`` short-circuit makes re-runs no-ops, and the
``ConditionExpression`` only rewrites a row still carrying the original slug.

Usage:
    uv run python -m scripts.backfill_marketplace_agent_ids            # dry-run
    uv run python -m scripts.backfill_marketplace_agent_ids --apply
"""

from __future__ import annotations

import argparse

from core.containers import get_workspace
from core.services import agent_export


def _all_listings() -> list[dict]:
    """Every listing row, across all statuses/versions. A stale slug fails
    republish regardless of status, so backfill is not scoped to ``published``."""
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
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()

    from core.services import marketplace_service

    workspace = get_workspace()
    updated = skipped_uuid = skipped_unresolved = errors = 0

    for row in _all_listings():
        listing_id = row.get("listing_id")
        version = int(row.get("version", 1))
        seller_id = row.get("seller_id")
        agent_id = row.get("agent_id")

        # v=0 is the slug-reservation sentinel (no agent); rows without an
        # agent_id (e.g. metadata-only drafts) have nothing to heal.
        if not agent_id or not seller_id or version == 0:
            continue

        try:
            live_dirs = set(workspace.list_workspace_agent_dirs(seller_id))
            if agent_id in live_dirs:
                skipped_uuid += 1
                continue  # already a live workspace id — nothing to do
            config = workspace.read_openclaw_config(seller_id)
            resolved = agent_export._resolve_workspace_id(seller_id, agent_id, config)
        except agent_export.AgentNotFoundError as exc:
            skipped_unresolved += 1
            print(f"  SKIP {listing_id} v{version}: {agent_id!r} ambiguous — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — bad seller_id / EFS read error: skip the row
            errors += 1
            print(f"  ERR  {listing_id} v{version}: resolve failed — {exc}")
            continue

        if resolved == agent_id:
            # Slug with no single matching live agent (deleted/renamed source).
            skipped_unresolved += 1
            print(f"  SKIP {listing_id} v{version}: {agent_id!r} has no resolvable workspace dir")
            continue

        print(f"  OK   {listing_id} v{version}: {agent_id!r} -> {resolved!r}")
        if not args.apply:
            continue

        try:
            marketplace_service._listings_table().update_item(
                Key={"listing_id": listing_id, "version": version},
                UpdateExpression="SET agent_id = :u",
                ConditionExpression="agent_id = :old",
                ExpressionAttributeValues={":u": resolved, ":old": agent_id},
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 — report + continue
            errors += 1
            print(f"  ERR  {listing_id} v{version}: update failed — {exc}")

    print(f"\nupdated={updated} already_uuid={skipped_uuid} unresolved={skipped_unresolved} errors={errors}")
    if not args.apply:
        print("(dry-run — pass --apply to write agent_id column updates)")


if __name__ == "__main__":
    main()
