"""One-shot migration: switch Paperclip agents from sessionKeyStrategy
'fixed' to 'issue'.

Run this once per environment (dev, then prod) after deploying the
agent-pairing PR. Iterates every PaperclipCompany row in DDB,
``list_agents`` per (deduped) ``company_id``, and patches those whose
``adapterConfig.sessionKeyStrategy`` is ``"fixed"``.

Idempotent: agents already on ``"issue"`` are skipped. Safe to re-run.

Usage::

    cd apps/backend
    uv run python -m scripts.migrate_paperclip_session_strategy --dry-run
    uv run python -m scripts.migrate_paperclip_session_strategy --apply

Adaptation notes
----------------

The original plan template referenced ``PaperclipAdminClient.update_agent``
and ``PaperclipRepo.scan_all``; neither exists in this codebase. The
adaptations made here:

* ``update_agent`` → use the existing
  :meth:`PaperclipAdminClient.patch_agent` (``PATCH /api/agents/{agentId}``)
  with ``body={"adapterConfig": new_cfg}``.
* ``scan_all`` → there is no full-table scan helper on ``PaperclipRepo``
  (the repo is keyed by ``user_id`` and exposes ``get`` /
  ``get_org_company_id`` / ``list_by_org_id`` / ``count_org_members`` /
  ``scan_purge_due``). For a one-shot CLI we paginate the underlying
  DDB table directly via :func:`core.dynamodb.run_in_thread` +
  ``Table.scan`` — this is acceptable for the small fleet sizes the
  Paperclip companies table sees and avoids adding a method to the
  repository purely for a one-shot script.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger("migrate_paperclip_session_strategy")


def needs_migration(agent: dict[str, Any]) -> bool:
    """True if this agent has ``sessionKeyStrategy == "fixed"`` and
    needs to be flipped to ``"issue"``. False for already-migrated
    agents and for non-openclaw_gateway adapter types (which don't have
    this field).
    """
    cfg = agent.get("adapterConfig") or {}
    return cfg.get("sessionKeyStrategy") == "fixed"


async def migrate_one_agent(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    agent: dict[str, Any],
) -> None:
    """Apply the strategy flip to a single agent. Preserves all other
    ``adapterConfig`` fields verbatim (``url``, ``authToken``,
    ``sessionKey``, …).
    """
    new_cfg = dict(agent["adapterConfig"])
    new_cfg["sessionKeyStrategy"] = "issue"
    await admin_client.patch_agent(
        session_cookie=admin_cookie,
        agent_id=agent["id"],
        body={"adapterConfig": new_cfg, "replaceAdapterConfig": True},
    )


async def _scan_company_ids() -> list[str]:
    """Paginate the ``paperclip-companies`` table and return the
    deduped set of ``company_id`` values, sorted for deterministic
    output.

    PaperclipCompany rows are keyed by ``user_id``, but multiple users
    in the same Isol8 org share a single ``company_id`` — without
    deduping we'd hit ``list_agents`` once per member and migrate the
    same fleet repeatedly. ``set`` dedupes; sorting makes log output
    reproducible across runs.
    """
    table = get_table("paperclip-companies")
    company_ids: set[str] = set()
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            cid = item.get("company_id")
            if cid:
                company_ids.add(cid)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return sorted(company_ids)


async def _migrate_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    apply: bool,
) -> tuple[int, int]:
    """Returns ``(matched, migrated)`` for the company."""
    raw = await admin_client.list_agents(
        session_cookie=admin_cookie,
        company_id=company_id,
    )
    # ``list_agents`` is typed as ``dict`` upstream but actually returns
    # a JSON array (see paperclip_openclaw_pairing for the same handling).
    if not isinstance(raw, list):
        logger.warning(
            "list_agents returned non-list for company=%s (got %s) — skipping",
            company_id,
            type(raw).__name__,
        )
        return 0, 0

    matched = 0
    migrated = 0
    for agent in raw:
        if not needs_migration(agent):
            continue
        matched += 1
        logger.info(
            "%s %s/%s (%s)",
            "MIGRATE" if apply else "DRY-RUN",
            company_id,
            agent.get("id"),
            agent.get("name", "?"),
        )
        if apply:
            try:
                await migrate_one_agent(
                    admin_client=admin_client,
                    admin_cookie=admin_cookie,
                    agent=agent,
                )
                migrated += 1
            except PaperclipApiError as e:
                logger.error(
                    "FAILED %s/%s status=%s: %s",
                    company_id,
                    agent.get("id"),
                    e.status_code,
                    e,
                )
    return matched, migrated


async def main(apply: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not settings.PAPERCLIP_INTERNAL_URL:
        raise RuntimeError(
            "PAPERCLIP_INTERNAL_URL is not configured. Run this script "
            "from a backend container (or with backend env loaded) so the "
            "internal Paperclip URL is reachable."
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)

        company_ids = await _scan_company_ids()
        total_matched = 0
        total_migrated = 0
        for company_id in company_ids:
            matched, migrated = await _migrate_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=company_id,
                apply=apply,
            )
            total_matched += matched
            total_migrated += migrated

        logger.info(
            "DONE: companies=%d matched=%d migrated=%d (apply=%s)",
            len(company_ids),
            total_matched,
            total_migrated,
            apply,
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Flip Paperclip agents from sessionKeyStrategy 'fixed' to 'issue'.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="List the agents that WOULD be migrated; make no changes.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Actually patch each matching agent.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
