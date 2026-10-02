"""Keep Paperclip's instance-wide experimental flags at the state this repo records.

Paperclip keeps its experimental flags in ONE row for the whole instance (every
customer company shares it): table ``instance_settings``, ``singleton_key =
'default'``, JSONB column ``experimental``. ``DESIRED_FLAGS`` below is the
intended state. The backend merges those keys into the stored object
(``experimental || :flags``) at startup, so a deploy converges the instance:
dev on merge, prod on promotion. Every other stored key is left alone.
``scripts/set_paperclip_instance_flags.py`` is the manual door onto the same code.

One flag is ordered behind another job: ``enableExternalObjects`` makes Paperclip
resolve the GitHub PRs and issues a task mentions, and it stores an
unauthenticated lookup of a private repository as a terminal "Not found". So the
startup task first syncs every workspace's GitHub token into Paperclip
(``paperclip_github_token.sweep``) and only then writes the flags; if that sweep
did not finish clean, the flag is held back for this start (see
``converge_at_startup``).

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

The task role already carries what this needs: the account-wipe grants in
service-stack.ts (``AccountWipePaperclipAurora*``, ``AccountWipeListSecrets``).
"""

from __future__ import annotations

import asyncio
import json
import logging

from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.services.paperclip_sql import cluster_arn, find_secret_arn, rds_data_client, secretsmanager_client

logger = logging.getLogger(__name__)

# The intended state. Add a flag here in the PR that ships the feature needing it.
# enableNativeRunner defaults to ON upstream and we never use it.
DESIRED_FLAGS: dict[str, bool] = {
    "enableNativeRunner": False,
    # The Teams Cases panels and the agents' cases API (403 "Cases are disabled" while off).
    "enableCases": True,
    # Live GitHub PR / issue status on tasks. Applied only after the token sweep: see above.
    "enableExternalObjects": True,
}
#: Held back at startup until every workspace's GitHub token is in Paperclip.
TOKEN_GATED_FLAG = "enableExternalObjects"
#: Ceiling on the whole startup sweep (it is sequential, ~10 s worst case per workspace).
_SWEEP_TIMEOUT_S = 600

SELECT_SQL = "SELECT experimental::text FROM instance_settings WHERE singleton_key = 'default'"
# Safe when several backend tasks boot at once (2-4, doubled during a roll): it
# is ONE statement, Postgres serialises the conflicting rows, and every writer
# merges the same keys to the same values, so the order they land in is moot.
UPSERT_SQL = (
    "INSERT INTO instance_settings (singleton_key, experimental) "
    "VALUES ('default', CAST(:flags AS jsonb)) "
    "ON CONFLICT (singleton_key) DO UPDATE SET "
    "experimental = instance_settings.experimental || EXCLUDED.experimental, updated_at = now()"
)

# Two things can make an early call fail. Aurora scales to zero, so the first
# Data API call after a quiet spell fails while the cluster resumes. And on a
# fresh environment the backend and Paperclip deploy in parallel, so this can
# run before Paperclip has migrated the database and created instance_settings.
# One try a minute for 15 minutes outlasts both; after that the next backend
# task start tries again.
_ATTEMPTS = 15
_RETRY_PAUSE_S = 60


def plan_changes(current: dict | None, desired: dict) -> dict[str, tuple[object, object]]:
    """Pure: ``{key: (stored value or None when unset, desired value)}`` for
    every key that needs a write. ``current`` is None when the row is missing."""
    stored = current or {}
    return {key: (stored.get(key), want) for key, want in desired.items() if stored.get(key) != want}


def database(env: str) -> dict:
    """The Data API addressing kwargs for this environment's Paperclip cluster."""
    return {
        "resourceArn": cluster_arn(env),
        "secretArn": find_secret_arn(secretsmanager_client(), env),
        "database": "paperclip",
    }


def read(client, db: dict) -> dict | None:
    """The stored ``experimental`` object, or None when the row is missing."""
    records = client.execute_statement(sql=SELECT_SQL, **db).get("records", [])
    if not records:
        return None
    current = json.loads(records[0][0]["stringValue"])
    if not isinstance(current, dict):
        raise TypeError(f"stored experimental is not a JSON object: {current!r}")
    return current


def write(client, db: dict, flags: dict) -> None:
    client.execute_statement(
        sql=UPSERT_SQL,
        parameters=[{"name": "flags", "value": {"stringValue": json.dumps(flags)}}],
        **db,
    )


def ensure_instance_flags(env: str, hold_back: frozenset[str] = frozenset()) -> dict[str, tuple[object, object]]:
    """Blocking. Write ``DESIRED_FLAGS`` only if the stored row differs, and
    return the changes applied (empty when nothing differed).

    A flag in ``hold_back`` is left exactly as stored: not switched on, and not
    switched off if an earlier start already switched it on."""
    client = rds_data_client()
    db = database(env)
    current = read(client, db)
    desired = {key: want for key, want in DESIRED_FLAGS.items() if key not in hold_back}
    changes = plan_changes(current, desired)
    if changes:
        write(client, db, desired)
    held = sorted(set(plan_changes(current, DESIRED_FLAGS)) & hold_back)
    if held:
        logger.error(
            "Paperclip instance flags HELD BACK (left as stored): %s. The next backend start tries again; "
            "scripts/set_paperclip_instance_flags.py --apply sets them by hand.",
            ", ".join(held),
        )
        put_metric("paperclip.instance_flags.held_back")
    return changes


async def _flags_to_hold_back() -> frozenset[str]:
    """Sync every workspace's GitHub token into Paperclip, and name the flag
    that must wait when that did not finish clean. Idempotent (an unchanged
    token writes nothing) and safe across the tasks booting together: see
    ``paperclip_github_token.sweep``."""
    from core.services import paperclip_github_token as tokens

    try:
        async with asyncio.timeout(_SWEEP_TIMEOUT_S):
            unsynced = [owner_id async for owner_id, outcome in tokens.sweep() if outcome in tokens.UNSYNCED]
    except Exception:  # noqa: BLE001 -- the scan, or the time limit; never fails startup
        logger.exception("Paperclip GitHub token sweep did not finish")
        return frozenset({TOKEN_GATED_FLAG})
    if unsynced:
        logger.error("Paperclip GitHub token sweep left %d workspace(s) unsynced: %s", len(unsynced), unsynced)
        return frozenset({TOKEN_GATED_FLAG})
    return frozenset()


async def converge_at_startup(env: str) -> None:
    """The lifespan's one-shot task: the GitHub token sweep, THEN the flags.
    Never raises: a failure is logged and counted, and the next task start
    tries again."""
    hold_back = await _flags_to_hold_back()
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            changes = await run_in_thread(ensure_instance_flags, env, hold_back)
        except Exception:
            if attempt == _ATTEMPTS:
                logger.exception("Paperclip instance flags not applied after %d attempts", _ATTEMPTS)
                put_metric("paperclip.instance_flags.failed")
                return
            await asyncio.sleep(_RETRY_PAUSE_S)
            continue
        if changes:
            logger.info("Paperclip instance flags applied: %s", ", ".join(sorted(changes)))
        return
