"""Backend-authored agent origin metadata.

Paperclip owns the agent row, while Isol8 owns how that agent entered the
company.  Keep that provenance outside seller-controlled agent fields and
derive it only from persisted seed IDs or the marketplace deployment sidecar.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from datetime import datetime, timezone
from typing import Any, Collection, Literal, TypedDict

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)


AgentOrigin = Literal["Seed", "Starter", "Marketplace", "Custom"]


def marketplace_provenance_key(owner_id: str, agent_id: str) -> str:
    return f"marketplace-installs/{owner_id}/{agent_id}.json"


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


@functools.cache
def _s3():
    """One S3 client for the process. Building a client costs ~20x the GetObject
    itself in CPU (it loads the service model), and /teams/agents read one sidecar
    per agent with a fresh client each, holding the GIL during cold load."""
    import boto3

    return boto3.client("s3")


def _read_sidecar(owner_id: str, agent_id: str) -> dict[str, Any] | None:
    import botocore.exceptions

    if not settings.OWNER_STATE_BUCKET:
        return None
    try:
        body = (
            _s3()
            .get_object(
                Bucket=settings.OWNER_STATE_BUCKET,
                Key=marketplace_provenance_key(owner_id, agent_id),
            )["Body"]
            .read()
        )
    except botocore.exceptions.ClientError as exc:
        if (exc.response or {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("ignoring malformed marketplace provenance marker for agent=%s", agent_id)
        return None
    return parsed if isinstance(parsed, dict) else None


async def _load_agent_sidecars(owner_id: str, agent_ids: list[str]) -> dict[str, dict]:
    loaded = await asyncio.gather(*(run_in_thread(_read_sidecar, owner_id, agent_id) for agent_id in agent_ids))
    return {
        agent_id: sidecar
        for agent_id, sidecar in zip(agent_ids, loaded)
        if isinstance(sidecar, dict)
        and sidecar.get("agent_id") == agent_id
        and marketplace_provenance(sidecar) is not None
    }


async def write_marketplace_provenance(
    *, owner_id: str, agent_id: str, listing_id: str, slug: str, version: int, seller_id: str
) -> None:
    if not settings.OWNER_STATE_BUCKET:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured; refusing to commit marketplace provenance")
    marker = {
        "listing_id": listing_id,
        "template_slug": slug,
        "template_version": int(version),
        "seller_id": seller_id,
        "agent_id": agent_id,
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }

    def _write() -> None:
        import boto3

        boto3.client("s3").put_object(
            Bucket=settings.OWNER_STATE_BUCKET,
            Key=marketplace_provenance_key(owner_id, agent_id),
            Body=json.dumps(marker, separators=(",", ":")).encode(),
            ContentType="application/json",
        )

    await run_in_thread(_write)


def stamp_agent_provenance(
    agent: Any,
    *,
    seeded_ids: Collection[str],
    starter_ids: Collection[str] = (),
    sidecar: dict[str, Any] | None = None,
) -> Any:
    """Stamp one Paperclip agent with the authoritative Isol8 origin."""

    if not isinstance(agent, dict):
        return agent
    agent_id = agent.get("id")
    seeded = isinstance(agent_id, str) and agent_id in seeded_ids
    if seeded:
        return {**agent, "seeded": True, "origin": "Seed"}
    if isinstance(agent_id, str) and agent_id in starter_ids:
        return {**agent, "seeded": False, "origin": "Starter"}

    marketplace = marketplace_provenance(sidecar)
    if marketplace is not None:
        return {
            **agent,
            "seeded": False,
            "origin": "Marketplace",
            "marketplace": marketplace,
        }

    return {**agent, "seeded": False, "origin": "Custom"}
