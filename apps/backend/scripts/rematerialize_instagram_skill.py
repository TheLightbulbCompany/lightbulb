"""One-shot migration: re-materialize the Instagram skill for owners connected
BEFORE in-container token renewal shipped.

Why this exists. Renewal moved out of the backend and into
``skills/instagram/token.sh``, which the agent calls to read the token. Owners
who connected earlier hold EFS artifacts from the previous materializer: a
``SKILL.md`` that reads ``credentials.json`` directly, and no ``token.sh`` at
all. Nothing re-materializes on deploy — ``apply_connection`` is reached only
through the connect flows in ``routers/connections.py`` — so once the backend
refresher is gone those owners silently stop renewing, and their grant dies at
its 60-day expiry with no error raised anywhere.

This re-runs the materializer with the credential still held in Secrets Manager,
rewriting SKILL.md + token.sh + credentials.json in place. It reads that
retained secret, so it must run BEFORE any secret pruning: flipping Instagram to
``refresh_owner: actor`` stops NEW connects from retaining one, but does not
delete what is already stored.

Run via::

    cd apps/backend
    DYNAMODB_TABLE_PREFIX=isol8-prod- uv run python -m scripts.rematerialize_instagram_skill --env prod
    DYNAMODB_TABLE_PREFIX=isol8-prod- uv run python -m scripts.rematerialize_instagram_skill --env prod --apply

Note the TRAILING HYPHEN on the prefix: ``core.dynamodb.table_name`` concatenates
it verbatim, so ``isol8-prod`` would address ``isol8-prodintegrations``.

Defaults to dry-run. Writes to the owner's EFS workspace, so run it where
``EFS_MOUNT_PATH`` actually resolves (a task in the cluster) — not a laptop.

Owners already on the new scheme are SKIPPED, keyed on ``token.sh`` existing in
their skill dir. This is load-bearing, not tidiness: once Instagram is
``refresh_owner: actor`` the OAuth callback no longer writes Secrets Manager
(``_retains_secret`` is False), so an owner who reconnects after the deploy has a
fresh credential on EFS and a STALE one in Secrets Manager. Re-materializing them
would push the stale token back over a working connection — the migration would
become the outage. ``token.sh`` is the marker because only the new materializer
writes it.

There is no cross-process lock, and none is claimed. ``connection_lock`` is an
in-process ``asyncio.Lock``; this runs as its own process, so taking it would
serialize against nothing. The realistic collision — an owner who reconnected
before the migration reaches them — is handled by the ``token.sh`` marker above.
What remains is the narrow case of a reconnect landing between that check and
``apply_connection`` completing, which would re-materialize the stale retained
credential over the fresh one. Rather than build a distributed lock for a
one-shot manual operation over a handful of owners, run this when nobody is
mid-reconnect; if it does collide, the owner reconnects once and a re-run then
skips them on the marker.

Idempotent: re-running rewrites the same three files atomically. Once every
environment is migrated, DELETE this script — git history is the record.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("rematerialize_instagram_skill")


async def main(*, apply: bool, env: str) -> int:
    os.environ["ENVIRONMENT"] = env

    # Imports after env setup, matching the other ops scripts: core.config reads
    # ENVIRONMENT at import time.
    from core.repositories import integrations_repo
    from core.services.credentials import apply_connection, integration_secret_store
    from core.services.credentials.materializers.base import owner_efs_root

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # The table is partitioned by owner_id with provider as the sort key and has
    # no provider-side index, so finding every Instagram row means a scan. That
    # is exactly what scan_all exists for, and this runs once, off the request path.
    rows = await integrations_repo.scan_all()
    owners = sorted(
        {r["owner_id"] for r in rows if r.get("provider") == "instagram" and r.get("status") == "connected"}
    )
    if not owners:
        print(f"[{env}] No owners with an Instagram connection — nothing to migrate")
        return 0

    print(f"[{env}] {len(owners)} owner(s) with Instagram connected")
    failures = 0
    for owner_id in owners:
        # Already on the new scheme — either migrated by an earlier run, or
        # reconnected since the deploy. A post-deploy reconnect writes EFS but
        # NOT Secrets Manager (refresh_owner: actor ⇒ _retains_secret False), so
        # re-materializing from the retained secret would overwrite a working
        # connection with a stale token. Skip on the marker only the new
        # materializer writes.
        if (owner_efs_root(owner_id) / "skills" / "instagram" / "token.sh").exists():
            print(f"  {owner_id}: already has token.sh — skipping")
            continue
        credential = await integration_secret_store.get(owner_id, "instagram")
        if not credential or not credential.get("access_token"):
            # Nothing retained to re-materialize from — that owner has to
            # reconnect. Say so loudly rather than leaving a silent gap.
            print(f"  {owner_id}: NO retained credential — owner must reconnect Instagram")
            failures += 1
            continue
        if not apply:
            print(f"  {owner_id}: would re-materialize SKILL.md + token.sh + credentials.json")
            continue
        try:
            # Deliberately NOT taking connection_lock: it is an in-process
            # asyncio.Lock, and this runs as its own process, so acquiring it
            # would serialize against nothing while reading as though it
            # serialized against the OAuth callback. See the module docstring
            # for how the residual race is actually handled.
            await apply_connection.apply_connection(owner_id, "instagram", credential)
            print(f"  {owner_id}: re-materialized")
        except Exception as exc:
            print(f"  {owner_id}: FAILED — {exc}")
            failures += 1

    if not apply:
        print(f"[{env}] dry run — pass --apply to write")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, choices=["dev", "staging", "prod"])
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env)))
