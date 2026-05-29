"""One-shot backfill: embed the gateway service token in existing
Paperclip agents' ``adapterConfig.url`` query string.

New agents are seeded with ``adapterConfig.url = "wss://...?token=<jwt>"``
so the WS gateway can authenticate the Paperclip→OpenClaw connection from
the URL alone. Agents seeded before that change carry a ``url`` with no
``?token=`` and keep getting 401s on the gateway — ``provision_org`` is
idempotent and never re-seeds them. This script rewrites every such
agent's ``adapterConfig.url`` to carry the token in the query string.

Run this once per environment (dev, then prod) after deploying the
URL-token PR. Iterates every PaperclipCompany row in DDB, ``list_agents``
per (deduped) ``company_id``, and patches each agent whose ``url`` does
not already carry a ``token=`` query parameter.

Idempotent: agents whose URL already carries the query token are skipped.
Safe to re-run.

Usage::

    cd apps/backend
    uv run python -m scripts.migrate_paperclip_adapter_url_token --dry-run
    uv run python -m scripts.migrate_paperclip_adapter_url_token --apply

The admin-session acquisition, company→owner resolution, and per-agent
``patch_agent(body={"adapterConfig": new_cfg, "replaceAdapterConfig":
True})`` call mirror ``migrate_paperclip_session_strategy.py`` — see that
script for the rationale behind the direct DDB scan (no full-table helper
on ``PaperclipRepo``) and the ``list_agents`` list-vs-dict handling.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services import service_token
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger("migrate_paperclip_adapter_url_token")


def needs_migration(agent: dict[str, Any]) -> bool:
    """True if this agent's ``adapterConfig.url`` lacks a ``token=`` query
    parameter.

    Idempotent: skips agents whose URL already carries the query token,
    and skips non-openclaw_gateway adapters (no ``url``).
    """
    cfg = agent.get("adapterConfig") or {}
    url = cfg.get("url") or ""
    return bool(url) and "token=" not in urlsplit(url).query


def migrate_one_agent(agent: dict[str, Any], owner_id: str | None) -> dict[str, Any]:
    """Return the new ``adapterConfig`` with the auth token embedded in
    the URL.

    Reuse the agent's existing ``authToken`` when present (already scoped
    to ``owner_id`` by prior provisioning/migration); mint a fresh
    owner-scoped token only if ``authToken`` is missing. Any pre-existing
    query string on the URL is stripped before the token is appended.
    """
    cfg = dict(agent["adapterConfig"])
    token = cfg.get("authToken")
    if not token and owner_id:
        token = service_token.mint(owner_id)
        cfg["authToken"] = token
    base = cfg["url"].split("?", 1)[0]
    cfg["url"] = f"{base}?token={token}"
    return cfg


def _redact_url(url: str) -> str:
    """Replace any ``token=<jwt>`` query value with a placeholder so the
    JWT never lands in logs."""
    base, _, query = url.partition("?")
    if not query:
        return url
    return f"{base}?token=<redacted>"


async def _patch_agent(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    agent: dict[str, Any],
    owner_id: str | None,
) -> None:
    """Apply the URL-token transform via a single ``patch_agent`` call."""
    new_cfg = migrate_one_agent(agent, owner_id)
    await admin_client.patch_agent(
        session_cookie=admin_cookie,
        agent_id=agent["id"],
        body={"adapterConfig": new_cfg, "replaceAdapterConfig": True},
    )


async def _scan_company_owner_map() -> dict[str, str]:
    """Paginate the ``paperclip-companies`` table and return a
    ``{company_id: owner_id}`` map.

    ``owner_id`` is sourced from the ``org_id`` column on the row
    (post-cutover that column carries the container's owner — Clerk
    org_id for orgs, Clerk user_id for personal owners; identical across
    every row in the same company per the ``PaperclipCompany.org_id``
    invariant). Mirrors ``migrate_paperclip_session_strategy``.
    """
    table = get_table("paperclip-companies")
    owner_by_company: dict[str, str] = {}
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            cid = item.get("company_id")
            owner = item.get("org_id")
            if not cid or not owner:
                continue
            existing = owner_by_company.get(cid)
            if existing is None:
                owner_by_company[cid] = owner
            elif existing != owner:
                logger.warning(
                    "company_id=%s has rows with disagreeing org_id (seen %s and %s); keeping first-seen — investigate",
                    cid,
                    existing,
                    owner,
                )
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return owner_by_company


async def _migrate_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    owner_id: str | None,
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
        old_url = (agent.get("adapterConfig") or {}).get("url", "")
        new_cfg = migrate_one_agent(agent, owner_id)
        logger.info(
            "%s %s/%s (%s) owner=%s  %s -> %s",
            "MIGRATE" if apply else "DRY-RUN",
            company_id,
            agent.get("id"),
            agent.get("name", "?"),
            owner_id or "<unknown>",
            _redact_url(old_url),
            _redact_url(new_cfg["url"]),
        )
        if apply:
            try:
                await _patch_agent(
                    admin_client=admin_client,
                    admin_cookie=admin_cookie,
                    agent=agent,
                    owner_id=owner_id,
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

        owner_by_company = await _scan_company_owner_map()
        company_ids = sorted(owner_by_company.keys())
        total_matched = 0
        total_migrated = 0
        for company_id in company_ids:
            matched, migrated = await _migrate_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=company_id,
                owner_id=owner_by_company.get(company_id),
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
        description="Embed the gateway service token in existing Paperclip agents' adapterConfig.url query string.",
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
