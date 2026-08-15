"""Backend-authored agent origin metadata.

Paperclip owns the agent row, while Isol8 owns how that agent entered the
company.  Keep that provenance outside seller-controlled agent fields and
derive it only from persisted seed IDs or the marketplace deployment sidecar.
"""

from __future__ import annotations

from typing import Any, Collection, Literal, TypedDict


AgentOrigin = Literal["Seed", "Marketplace", "Custom"]


class MarketplaceProvenance(TypedDict):
    listingId: str
    slug: str
    version: int
    seller: str


def marketplace_provenance(sidecar: dict[str, Any] | None) -> MarketplaceProvenance | None:
    """Return canonical marketplace fields only for complete v2 sidecars.

    Legacy sidecars did not persist the listing ID.  A slug, name, or partial
    seller record is not enough evidence to call an agent Marketplace, so those
    agents intentionally fall back to Custom.
    """

    if not isinstance(sidecar, dict):
        return None
    listing_id = sidecar.get("listing_id")
    slug = sidecar.get("template_slug")
    seller = sidecar.get("seller_id")
    raw_version = sidecar.get("template_version")
    if not all(isinstance(value, str) and value.strip() for value in (listing_id, slug, seller)):
        return None
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None
    return {
        "listingId": listing_id,
        "slug": slug,
        "version": version,
        "seller": seller,
    }


def stamp_agent_provenance(
    agent: Any,
    *,
    seeded_ids: Collection[str],
    sidecar: dict[str, Any] | None = None,
) -> Any:
    """Stamp one Paperclip agent with the authoritative Isol8 origin."""

    if not isinstance(agent, dict):
        return agent
    agent_id = agent.get("id")
    seeded = isinstance(agent_id, str) and agent_id in seeded_ids
    if seeded:
        return {**agent, "seeded": True, "origin": "Seed"}

    marketplace = marketplace_provenance(sidecar)
    if marketplace is not None:
        return {
            **agent,
            "seeded": False,
            "origin": "Marketplace",
            "marketplace": marketplace,
        }

    return {**agent, "seeded": False, "origin": "Custom"}
