"""The specialist registry (spec 03 §5.2): `AGENT#` rows on the `tasks` table, keyed by agent id.

`roster(ws)` returns what `messaging.load_agent_roster` returns today for the keys its consumers read
(spec 03 §2.1): `id, name, role, title, status, reportsTo, capabilities, metadata, companyId`, plus `icon` and
`appearance`. `metadata` carries the listing under its legacy keys (`marketplace_hire.LISTING_ID_KEY` /
`LISTING_SLUG_KEY`), because consumers read it with those helpers.

Errors: `RegistryUnavailable` (any store failure; `load_agent_roster` maps it to `RUNTIME_UNAVAILABLE`) and
`AgentConflict` (an id already registered under another workspace).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from core.repositories import agent_registry_repo as repo
from core.repositories.agent_registry_repo import AgentConflict

__all__ = [
    "AgentConflict",
    "RegistryUnavailable",
    "create",
    "delete",
    "get",
    "owner_of",
    "roster",
    "update",
]

logger = logging.getLogger(__name__)

# Legacy metadata keys for marketplace provenance (core/services/marketplace_hire.py:52-54).
_LISTING_ID_KEY = "isol8_listing_id"
_LISTING_SLUG_KEY = "isol8_listing_slug"


class RegistryUnavailable(Exception):
    """The registry store could not be read or written."""


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


# --- Native writes (Phase 1 task 8b.1) ---------------------------------------------------------------

#: What `update` may set (registry attr names). `status` is display state only: nothing at runtime reads it.
_UPDATABLE = frozenset(
    {"name", "title", "role", "persona", "icon", "appearance", "reportsTo", "budgetMonthlyCents", "status"}
)


async def create(
    ws: str,
    *,
    name: str,
    role: str | None = None,
    title: str | None = None,
    persona: str | None = None,
    icon: str | None = None,
    reports_to: str | None = None,
    budget_monthly_cents: int = 0,
    listing: dict | None = None,
    agent_id: str | None = None,
) -> dict:
    """A new specialist (uuid4 id unless given); the same id replayed in the same ws returns the existing row."""
    row = {
        "agentId": agent_id or str(uuid.uuid4()),
        "name": name[:256],
        "role": role,
        "title": title,
        "persona": persona,
        "icon": icon,
        "reportsTo": reports_to,
        "budgetMonthlyCents": budget_monthly_cents,
        "listing": listing,
    }
    try:
        item, _ = await repo.put_new(ws, row)
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc
    return _roster_row(item)


async def update(ws: str, agent_id: str, fields: dict) -> dict:
    """SET `fields` on a live agent and return its roster row. Missing or deleted → `KeyError`."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"agent update may not set {sorted(bad)}")
    existing = await get(ws, agent_id)
    if existing is None or existing.get("status") == "deleted":
        raise KeyError(agent_id)
    if not fields:
        return _roster_row(existing)
    try:
        return _roster_row(await repo.upsert_fields(ws, agent_id, fields))
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc


async def delete(ws: str, agent_id: str) -> None:
    """Soft delete (the id stays reserved). Already gone → no-op."""
    existing = await get(ws, agent_id)
    if existing is None or existing.get("status") == "deleted":
        return
    try:
        await repo.mark_deleted(ws, agent_id)
    except (ClientError, BotoCoreError) as exc:
        raise RegistryUnavailable(str(exc)) from exc
