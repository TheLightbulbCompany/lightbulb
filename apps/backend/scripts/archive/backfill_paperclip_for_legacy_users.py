"""One-shot backfill: provision Paperclip for owners with a container
but no PaperclipCompany row.

Fixes the legacy prod user(s) whose container was provisioned before
Paperclip was integrated into the system. After the consolidation that
made ``/container/status`` the canonical Paperclip-provisioning trigger,
new users are correctly auto-provisioned, but pre-existing legacy rows
remain stuck with a 409 on ``/teams`` until backfilled.

Run via::

    cd apps/backend
    uv run python -m scripts.backfill_paperclip_for_legacy_users \\
        --env prod
    uv run python -m scripts.backfill_paperclip_for_legacy_users \\
        --env prod --apply

Defaults to dry-run; pass ``--apply`` to actually call ``provision_org``.

Idempotent: ``provision_org`` short-circuits on existing active rows,
so re-running is safe.

Adaptation notes
----------------

* ``container_repo`` exposes no full-table scan helper, so we paginate
  the underlying ``containers`` DDB table directly via
  :func:`core.dynamodb.run_in_thread` + ``Table.scan`` — same pattern
  used by ``migrate_paperclip_session_strategy.py``. Acceptable for the
  small fleet sizes the containers table sees.
* ``paperclip_repo`` is a class (``PaperclipRepo``) keyed by
  ``user_id``, but the ``by-org-id`` GSI maps ``org_id → row``. For
  legacy detection we use :meth:`PaperclipRepo.get_org_company_id`
  because ``org_id`` is stored as the container's ``owner_id`` for
  *both* personal and org owners (see ``provision_org`` step 8). This
  unifies the existence check across both owner kinds.
* For org owners, ``lookup_owner_email`` will fall through both
  ``user_repo`` (no row keyed by ``org_*``) and Clerk's
  ``get_user`` (404 on org ids) and return ``None``. The script then
  prints a SKIP line — operators handle those by hand (rare; legacy
  prod has personal owners only at time of writing).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("backfill_paperclip_for_legacy_users")


async def _scan_owner_ids() -> list[str]:
    """Paginate the ``containers`` table and return every ``owner_id``.

    Sorted for deterministic output across runs. Mirrors the scan
    pattern in ``migrate_paperclip_session_strategy._scan_company_ids``.
    """
    from core.dynamodb import get_table, run_in_thread

    table = get_table("containers")
    owner_ids: set[str] = set()
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("owner_id")
            if owner_id:
                owner_ids.add(owner_id)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return sorted(owner_ids)


async def main(*, apply: bool, env: str) -> int:
    # Imports inside main so script setup (path, env vars) is done first.
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_owner_email import lookup_owner_email
    from core.services.paperclip_provisioning import (
        build_paperclip_provisioning,
        close_paperclip_provisioning,
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo = PaperclipRepo(table_name="paperclip-companies")

    owner_ids = await _scan_owner_ids()
    legacy: list[str] = []
    for owner_id in owner_ids:
        # ``get_org_company_id`` queries the ``by-org-id`` GSI (PK:
        # ``org_id``). The PaperclipCompany row stores the container's
        # ``owner_id`` in the ``org_id`` column for both personal and
        # org owners (see provision_org step 8), so this lookup is the
        # right unified existence check.
        existing = await repo.get_org_company_id(owner_id)
        if existing is None:
            legacy.append(owner_id)

    print(f"[{env}] Found {len(legacy)} owner(s) with container but no Paperclip company")
    for owner_id in legacy:
        print(f"  {owner_id}")
    if not apply:
        print("DRY RUN — pass --apply to provision")
        return 0

    provisioning = await build_paperclip_provisioning()
    try:
        for owner_id in legacy:
            owner_email = await lookup_owner_email(
                org_id=owner_id if owner_id.startswith("org_") else None,
                fallback_user_id=owner_id,
            )
            if not owner_email:
                print(f"  SKIP {owner_id}: no email resolvable")
                continue
            try:
                await provisioning.provision_org(
                    owner_id=owner_id,
                    owner_user_id=owner_id,
                    owner_email=owner_email,
                )
                print(f"  OK   {owner_id}")
            except Exception as e:  # noqa: BLE001
                print(f"  ERR  {owner_id}: {e}")
    finally:
        await close_paperclip_provisioning(provisioning)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Paperclip for owners with a container but no PaperclipCompany row. Defaults to dry-run."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run provisioning (default: dry-run).",
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help="Environment label (informational; doesn't change DDB targeting).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env)))
