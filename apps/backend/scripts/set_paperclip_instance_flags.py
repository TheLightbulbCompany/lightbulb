"""Set Paperclip's instance-wide experimental flags to the state this repo records.

Paperclip keeps its experimental flags in ONE row for the whole instance (every
customer company shares it): table ``instance_settings``, ``singleton_key =
'default'``, JSONB column ``experimental``. ``DESIRED_FLAGS`` below is the
intended state; this script merges those keys into the stored object
(``experimental || :flags``) and leaves every other key alone. Re-runnable and
idempotent: run it again after adding a flag here, or after standing up a fresh
database.

Why direct SQL and not Paperclip's own doors:

- ``PATCH /api/instance/settings/experimental`` works, but writes an activity
  row into EVERY company's feed (``server/src/routes/instance-settings.ts``).
- ``PAPERCLIP_MANAGED_CONFIG`` flips the instance into cloud-managed mode, which
  blocks company creation.

Storing only the keys we set is safe: the service normalises the stored object
on every read and defaults each absent key (``normalizeExperimentalSettings`` in
the fork's ``server/src/services/instance-settings.ts``), and it reads the row
on each use with no cache, so a write takes effect without a restart. The row is
created lazily by the server (``getOrCreateRow``), so it may not exist yet; the
statement is an upsert, and a row it creates gets the column defaults for
everything else. Names verified against fork tag ``v2026.916.1-isol8.3``;
re-check them when ``paperclip-version.json`` moves.

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

Prints one line per flag and exits non-zero on any failure. Aurora scales to
zero, so a first call against a paused cluster can fail while it resumes: run it
again.
"""

from __future__ import annotations

import argparse
import json
import sys

from core.services.paperclip_sql import cluster_arn, find_secret_arn, rds_data_client, secretsmanager_client

# The intended state. Add a flag here in the PR that ships the feature needing it.
# enableNativeRunner defaults to ON upstream and we never use it.
DESIRED_FLAGS: dict[str, bool] = {"enableNativeRunner": False}

SELECT_SQL = "SELECT experimental::text FROM instance_settings WHERE singleton_key = 'default'"
UPSERT_SQL = (
    "INSERT INTO instance_settings (singleton_key, experimental) "
    "VALUES ('default', CAST(:flags AS jsonb)) "
    "ON CONFLICT (singleton_key) DO UPDATE SET "
    "experimental = instance_settings.experimental || EXCLUDED.experimental, updated_at = now()"
)


def plan_changes(current: dict | None, desired: dict) -> dict[str, tuple[object, object]]:
    """Pure: ``{key: (stored value or None when unset, desired value)}`` for
    every key that needs a write. ``current`` is None when the row is missing."""
    stored = current or {}
    return {key: (stored.get(key), want) for key, want in desired.items() if stored.get(key) != want}


def _read(client, db: dict) -> dict | None:
    records = client.execute_statement(sql=SELECT_SQL, **db).get("records", [])
    if not records:
        return None
    current = json.loads(records[0][0]["stringValue"])
    if not isinstance(current, dict):
        sys.exit(f"REFUSING: stored experimental is not a JSON object: {current!r}")
    return current


def _write(client, db: dict, flags: dict) -> None:
    client.execute_statement(
        sql=UPSERT_SQL,
        parameters=[{"name": "flags", "value": {"stringValue": json.dumps(flags)}}],
        **db,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client = rds_data_client()
    db = {
        "resourceArn": cluster_arn(args.env),
        "secretArn": find_secret_arn(secretsmanager_client(), args.env),
        "database": "paperclip",
    }

    current = _read(client, db)
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

    _write(client, db, DESIRED_FLAGS)
    after = _read(client, db)
    print(f"\nStored experimental now: {json.dumps(after, sort_keys=True)}")
    if plan_changes(after, DESIRED_FLAGS):
        sys.exit("FAILED: the row does not carry the desired flags after the write.")


if __name__ == "__main__":
    main()
