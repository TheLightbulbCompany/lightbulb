"""One-off: delete slug-reservation sentinel rows (listing_id='slug:...').

The sentinel mechanism is removed: slugs are no longer unique, and the
listing_id UUID is the sole identity key. The old per-listing sentinel rows
(``listing_id='slug:{slug}', version=0``) are now inert — nothing reads or
writes them — but they linger in the table and should be swept.

Dry-run by default; pass ``--apply`` to delete. Requires AWS SSO
(``aws sso login --profile isol8-admin``).

    cd apps/backend && uv run python -m scripts.sweep_orphan_slug_sentinels \
        --env prod                                        # dry run
    cd apps/backend && uv run python -m scripts.sweep_orphan_slug_sentinels \
        --env prod --apply
"""

from __future__ import annotations

import argparse

import boto3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    table_name = f"isol8-{args.env}-marketplace-listings"
    table = boto3.resource("dynamodb").Table(table_name)

    # Single page only: the marketplace-listings table is tiny (~tens of rows),
    # so one 1MB scan page covers it and there's no need to follow
    # LastEvaluatedKey. Revisit if the listing count ever grows large.
    scan = table.scan(ProjectionExpression="listing_id, version")
    sentinels = [
        {"listing_id": i["listing_id"], "version": i["version"]}
        for i in scan.get("Items", [])
        if str(i["listing_id"]).startswith("slug:")
    ]

    print(f"{table_name}: {len(sentinels)} sentinel row(s)")
    for key in sentinels:
        print("  ", key["listing_id"])
        if args.apply:
            table.delete_item(Key=key)

    print("APPLIED" if args.apply else "DRY-RUN (pass --apply to delete)")


if __name__ == "__main__":
    main()
