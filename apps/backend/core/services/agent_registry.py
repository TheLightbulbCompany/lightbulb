"""The specialist registry (spec 03 §5.2): `AGENT#` rows on the `tasks` table, keyed by today's Paperclip agent ids.

`roster(ws)` returns what `messaging.load_agent_roster` returns today for the keys its consumers read
(spec 03 §2.1): `id, name, role, title, status, reportsTo, capabilities, metadata, companyId`, plus `icon` and
`appearance`. `metadata` carries the listing under Paperclip's own keys (`marketplace_hire.LISTING_ID_KEY` /
`LISTING_SLUG_KEY`), because consumers read it with those helpers.

Errors: `RegistryUnavailable` (any store failure; `load_agent_roster` maps it to `RUNTIME_UNAVAILABLE`) and
`AgentConflict` (an id already registered under another workspace).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from core.repositories import agent_registry_repo as repo
from core.repositories.agent_registry_repo import AgentConflict

__all__ = ["AgentConflict", "RegistryUnavailable", "get", "owner_of", "roster", "row_from_paperclip"]

# Paperclip metadata keys for marketplace provenance (core/services/marketplace_hire.py:52-54).
_LISTING_ID_KEY = "isol8_listing_id"
_LISTING_SLUG_KEY = "isol8_listing_slug"


class RegistryUnavailable(Exception):
    """The registry store could not be read or written."""


def row_from_paperclip(agent: dict) -> dict:
    """A Paperclip agent dict → camelCase `Agent` fields. Never called for a `terminated` agent."""
    meta = agent.get("metadata") or {}
    listing = {
        k: v for k, v in (("listingId", meta.get(_LISTING_ID_KEY)), ("listingSlug", meta.get(_LISTING_SLUG_KEY))) if v
    }
    return {
        "agentId": agent["id"],
        "name": str(agent.get("name") or agent["id"])[:256],
        "role": agent.get("role"),
        "title": agent.get("title"),
        "persona": agent.get("capabilities"),
        "icon": agent.get("icon"),
        "appearance": agent.get("appearance"),
        "reportsTo": agent.get("reportsTo"),
        "status": "paused" if agent.get("status") == "paused" else "active",
        "listing": listing or None,
        "budgetMonthlyCents": int(agent.get("budgetMonthlyCents") or 0),
        "companyId": agent.get("companyId"),
    }


def _roster_row(row: dict) -> dict[str, Any]:
    listing = row.get("listing") or {}
    metadata = {
        k: v
        for k, v in ((_LISTING_ID_KEY, listing.get("listingId")), (_LISTING_SLUG_KEY, listing.get("listingSlug")))
        if v
    }
    return {
        "id": row["agentId"],
        "name": str(row.get("name") or row["agentId"])[:256],
        "role": row.get("role"),
        "title": row.get("title"),
        "status": row.get("status", "active"),
        "reportsTo": row.get("reportsTo"),
        "capabilities": row.get("persona"),
        "icon": row.get("icon"),
        "appearance": row.get("appearance"),
        "metadata": metadata,
        "companyId": row.get("companyId"),
    }


async def roster(ws: str) -> list[dict[str, Any]]:
    """Active and paused specialists, sorted by id."""
    try:
        rows = await repo.list_agents(ws)
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc
    return sorted((_roster_row(r) for r in rows if r.get("status") != "deleted"), key=lambda r: r["id"])


async def get(ws: str, agent_id: str) -> dict | None:
    try:
        return await repo.get(ws, agent_id)
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc


async def owner_of(agent_id: str) -> str | None:
    try:
        return await repo.owner_of(agent_id)
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc
