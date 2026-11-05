"""The specialist registry (spec 03 §5.2): `AGENT#` rows on the `tasks` table, keyed by today's Paperclip agent ids.

`roster(ws)` returns what `messaging.load_agent_roster` returns today for the keys its consumers read
(spec 03 §2.1): `id, name, role, title, status, reportsTo, capabilities, metadata, companyId`, plus `icon` and
`appearance`. `metadata` carries the listing under Paperclip's own keys (`marketplace_hire.LISTING_ID_KEY` /
`LISTING_SLUG_KEY`), because consumers read it with those helpers.

Errors: `RegistryUnavailable` (any store failure; `load_agent_roster` maps it to `RUNTIME_UNAVAILABLE`) and
`AgentConflict` (an id already registered under another workspace).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, NamedTuple

from botocore.exceptions import BotoCoreError, ClientError

from core.observability.metrics import put_metric
from core.repositories import agent_registry_repo as repo
from core.repositories.agent_registry_repo import AgentConflict
from core.services.super_agent_flags import env_enabled

__all__ = [
    "AgentConflict",
    "Diff",
    "RegistryUnavailable",
    "SeedReport",
    "get",
    "mirror_delete",
    "mirror_from_paperclip",
    "owner_of",
    "roster",
    "row_from_paperclip",
    "seed_from_paperclip",
    "verify",
]

logger = logging.getLogger(__name__)

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


# --- Mirror of Paperclip agent writes (wave1-R Task 3) ----------------------------------------------
#
# Called by `PaperclipAdminClient` after every successful agent create/patch/update/delete: the one place all
# agent write doors pass through. No-op unless the env master is on (the table does not exist in prod). Never
# raises: the user's Paperclip write already succeeded; a gap shows in the metric and in the backfill's
# `--verify` (`missing_in_registry`), and a backfill `--apply` heals it.


def _mirror_metric(result: str) -> None:
    put_metric("agent_registry.mirror", dimensions={"result": result})


async def _workspace_of(agent: dict) -> str | None:
    ws = await repo.owner_of(agent["id"])
    if ws is None and agent.get("companyId"):
        from core.repositories.paperclip_repo import PaperclipRepo

        row = await PaperclipRepo(table_name="paperclip-companies").find_workspace_by_company(agent["companyId"])
        ws = row.org_id if row is not None else None
    return ws


async def mirror_from_paperclip(agent: dict) -> None:
    """Upsert the registry row for a Paperclip agent dict (`terminated` → soft delete)."""
    if not env_enabled():
        return
    try:
        ws = await _workspace_of(agent)
        if ws is None:
            logger.warning("agent_registry.mirror_skipped agent=%s company=%s", agent.get("id"), agent.get("companyId"))
            _mirror_metric("skipped")
            return
        if agent.get("status") == "terminated":
            if await repo.get(ws, agent["id"]) is not None:
                await repo.mark_deleted(ws, agent["id"])
            _mirror_metric("deleted")
            return
        fields = row_from_paperclip(agent)
        _, created = await repo.put_new(ws, fields)
        if not created:
            fields.pop("agentId")
            await repo.upsert_fields(ws, agent["id"], fields)
        _mirror_metric("created" if created else "updated")
    except Exception:  # noqa: BLE001 -- never fail the Paperclip write
        logger.exception("agent_registry.mirror failed agent=%s", agent.get("id"))
        _mirror_metric("error")


async def mirror_delete(agent_id: str) -> None:
    """Soft-delete the registry row of a deleted Paperclip agent."""
    if not env_enabled():
        return
    try:
        ws = await repo.owner_of(agent_id)
        if ws is None or await repo.get(ws, agent_id) is None:
            logger.warning("agent_registry.mirror_skipped agent=%s (delete, not registered)", agent_id)
            _mirror_metric("skipped")
            return
        await repo.mark_deleted(ws, agent_id)
        _mirror_metric("deleted")
    except Exception:  # noqa: BLE001 -- never fail the Paperclip write
        logger.exception("agent_registry.mirror failed agent=%s (delete)", agent_id)
        _mirror_metric("error")


# --- Backfill and verify (wave1-R Task 4) ------------------------------------------------------------


class SeedReport(NamedTuple):
    created: int
    updated: int
    skipped_terminated: int


class Diff(NamedTuple):
    kind: Literal["missing_in_registry", "extra_in_registry", "field"]
    agent_id: str
    field: str | None = None


# The fields `verify` compares (registry attr names); status is normalised to active|paused on both sides.
_VERIFY_FIELDS = ("name", "role", "title", "reportsTo", "persona", "status", "listingId")


async def _paperclip_agents(ws: str) -> list[dict]:
    """The owner's live Paperclip agents: the call `load_agent_roster` makes today. Raises when unavailable."""
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner

    rows = await fetch_paperclip_agents_for_owner(
        owner_id=ws,
        repo=PaperclipRepo(table_name="paperclip-companies"),
        admin_client=shared_admin_client(),
        raise_on_unavailable=True,
    )
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


async def seed_from_paperclip(ws: str, *, dry_run: bool = False) -> SeedReport:
    """Id-preserving backfill: every non-terminated Paperclip agent → its `AGENT#` row. Idempotent: an
    unchanged row is not written. A registry row whose agent is terminated or gone is left for `verify`."""
    created = updated = skipped = 0
    for agent in await _paperclip_agents(ws):
        if agent.get("status") == "terminated":
            skipped += 1
            continue
        fields = row_from_paperclip(agent)
        existing = await repo.get(ws, agent["id"])
        if existing is None:
            created += 1
            if not dry_run:
                _, made = await repo.put_new(ws, fields)
                if not made:  # raced a mirror: fall through to the field compare next run
                    created -= 1
            continue
        fields.pop("agentId")
        changed = {k: v for k, v in fields.items() if existing.get(k) != v}
        if changed:
            updated += 1
            if not dry_run:
                await repo.upsert_fields(ws, agent["id"], changed)
    return SeedReport(created, updated, skipped)


def _comparable(row: dict) -> dict[str, Any]:
    out = {k: row.get(k) for k in _VERIFY_FIELDS if k != "listingId"}
    out["status"] = "paused" if row.get("status") == "paused" else "active"
    out["listingId"] = (row.get("listing") or {}).get("listingId")
    return out


async def verify(ws: str) -> list[Diff]:
    """Set and field equality of the registry against Paperclip (terminated / deleted dropped on each side)."""
    paperclip = {
        a["id"]: _comparable(row_from_paperclip(a))
        for a in await _paperclip_agents(ws)
        if a.get("status") != "terminated"
    }
    registry = {r["agentId"]: _comparable(r) for r in await repo.list_agents(ws) if r.get("status") != "deleted"}
    diffs = [Diff("missing_in_registry", i) for i in sorted(paperclip.keys() - registry.keys())]
    diffs += [Diff("extra_in_registry", i) for i in sorted(registry.keys() - paperclip.keys())]
    for agent_id in sorted(paperclip.keys() & registry.keys()):
        diffs += [Diff("field", agent_id, f) for f in _VERIFY_FIELDS if paperclip[agent_id][f] != registry[agent_id][f]]
    return diffs
