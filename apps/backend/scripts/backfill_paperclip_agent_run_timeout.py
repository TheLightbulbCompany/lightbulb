"""One-shot backfill: raise every Paperclip agent's per-run timeout to 300s.

The openclaw-gateway adapter defaults ``timeoutSec=120`` / ``waitTimeoutMs=120000``
(``build-config.ts``), tuned for short single-step heartbeat runs. Our agents
do substantive single-run work that routinely exceeds 2 min, so the gateway
stamps a still-working run ``timed_out`` at 120s and fires a retry that queues
on the per-issue session lane and times out too — a cascade of ``timed_out``
runs even though the work commits. #1078 pinned ``timeoutSec=300`` /
``waitTimeoutMs=300000`` in ``synthesize_openclaw_adapter`` so every agent
CREATED after it gets the higher ceiling — but synth only runs at create/update
time. Agents created BEFORE #1078 are frozen at 120s (a reprovision rebuilds
``openclaw.json`` from the stored records, preserving their old adapterConfig).

This script one-time patches those legacy agents fleet-wide. It iterates every
PaperclipCompany row in DDB, ``list_agents`` per (deduped) ``company_id``, and
for each openclaw_gateway agent whose ``timeoutSec`` is below the canonical
value, sets ``timeoutSec`` + ``waitTimeoutMs`` to the current constants —
preserving every other ``adapterConfig`` field verbatim (``url``, ``authToken``,
``devicePrivateKeyPem``, ``paperclipApiUrl``, ``claimedApiKeyPath``,
``sessionKey``, ``sessionKeyStrategy``) by copying the existing dict and using
``replaceAdapterConfig``.

Idempotent: agents already at ``timeoutSec >= AGENT_RUN_TIMEOUT_SEC`` (and the
matching wait) are skipped. Safe to re-run. The dry-run doubles as an audit:
it lists exactly which agents are still on the legacy timeout.

Usage::

    cd apps/backend
    uv run python -m scripts.backfill_paperclip_agent_run_timeout --dry-run
    uv run python -m scripts.backfill_paperclip_agent_run_timeout --apply

Must run from a backend container (or with backend env loaded) so
``PAPERCLIP_INTERNAL_URL`` resolves the in-VPC Cloud Map host.

Adapted from ``scripts.migrate_paperclip_session_strategy`` — see its
"Adaptation notes" for why we paginate the ``paperclip-companies`` table
directly (no ``scan_all`` helper) and use ``patch_agent`` (no ``update_agent``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services.paperclip_adapter_config import (
    AGENT_RUN_TIMEOUT_SEC,
    AGENT_RUN_WAIT_TIMEOUT_MS,
    OPENCLAW_GATEWAY_TYPE,
)
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger("backfill_paperclip_agent_run_timeout")


def needs_backfill(agent: dict[str, Any]) -> bool:
    """True if this agent's per-run timeout is below the canonical value.

    Only openclaw_gateway agents carry the adapterConfig timeout fields; any
    other adapter type is skipped. An agent is a candidate when EITHER
    ``timeoutSec`` OR ``waitTimeoutMs`` is missing or below the canonical
    constant — both are lifted together so the two never drift apart.
    """
    if agent.get("adapterType") != OPENCLAW_GATEWAY_TYPE:
        return False
    cfg = agent.get("adapterConfig") or {}
    timeout_sec = cfg.get("timeoutSec")
    wait_ms = cfg.get("waitTimeoutMs")
    if not isinstance(timeout_sec, int) or timeout_sec < AGENT_RUN_TIMEOUT_SEC:
        return True
    if not isinstance(wait_ms, int) or wait_ms < AGENT_RUN_WAIT_TIMEOUT_MS:
        return True
    return False


async def backfill_one_agent(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    agent: dict[str, Any],
) -> None:
    """Lift ``timeoutSec`` + ``waitTimeoutMs`` to the canonical constants in a
    single ``patch_agent`` call, preserving every other ``adapterConfig`` field.
    """
    new_cfg = dict(agent.get("adapterConfig") or {})
    new_cfg["timeoutSec"] = AGENT_RUN_TIMEOUT_SEC
    new_cfg["waitTimeoutMs"] = AGENT_RUN_WAIT_TIMEOUT_MS
    await admin_client.patch_agent(
        session_cookie=admin_cookie,
        agent_id=agent["id"],
        body={"adapterConfig": new_cfg, "replaceAdapterConfig": True},
    )


async def _scan_company_ids() -> list[str]:
    """Paginate the ``paperclip-companies`` table and return the deduped
    set of ``company_id`` values.

    Multiple users in the same org share one ``company_id``; we dedupe so
    ``list_agents`` runs once per company. Timeout is owner-agnostic (unlike
    the session-strategy migration), so we don't need the owner map.
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


async def _backfill_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    apply: bool,
) -> tuple[int, int]:
    """Returns ``(matched, patched)`` for the company."""
    raw = await admin_client.list_agents(
        session_cookie=admin_cookie,
        company_id=company_id,
    )
    # ``list_agents`` is typed ``dict`` upstream but returns a JSON array
    # (same handling as migrate_paperclip_session_strategy).
    if not isinstance(raw, list):
        logger.warning(
            "list_agents returned non-list for company=%s (got %s) — skipping",
            company_id,
            type(raw).__name__,
        )
        return 0, 0

    matched = 0
    patched = 0
    for agent in raw:
        if not needs_backfill(agent):
            continue
        matched += 1
        cfg = agent.get("adapterConfig") or {}
        logger.info(
            "%s %s/%s (%s) timeoutSec=%s→%d waitTimeoutMs=%s→%d",
            "PATCH" if apply else "DRY-RUN",
            company_id,
            agent.get("id"),
            agent.get("name", "?"),
            cfg.get("timeoutSec"),
            AGENT_RUN_TIMEOUT_SEC,
            cfg.get("waitTimeoutMs"),
            AGENT_RUN_WAIT_TIMEOUT_MS,
        )
        if apply:
            try:
                await backfill_one_agent(
                    admin_client=admin_client,
                    admin_cookie=admin_cookie,
                    agent=agent,
                )
                patched += 1
            except PaperclipApiError as e:
                logger.error(
                    "FAILED %s/%s status=%s: %s",
                    company_id,
                    agent.get("id"),
                    e.status_code,
                    e,
                )
    return matched, patched


async def main(apply: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not settings.PAPERCLIP_INTERNAL_URL:
        raise RuntimeError(
            "PAPERCLIP_INTERNAL_URL is not configured. Run this script "
            "from a backend container (or with backend env loaded) so the "
            "internal Paperclip URL is reachable."
        )

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)

        company_ids = await _scan_company_ids()
        total_matched = 0
        total_patched = 0
        for company_id in company_ids:
            matched, patched = await _backfill_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=company_id,
                apply=apply,
            )
            total_matched += matched
            total_patched += patched

        logger.info(
            "DONE: companies=%d matched=%d patched=%d target_timeoutSec=%d (apply=%s)",
            len(company_ids),
            total_matched,
            total_patched,
            AGENT_RUN_TIMEOUT_SEC,
            apply,
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill Paperclip agents' per-run timeout to the canonical 300s.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="List the agents that WOULD be patched; make no changes.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Actually patch each matching agent.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
