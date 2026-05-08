"""In-process marketplace search.

At v0 scale (Isol8-internal only — paying users publishing to other paying
users) the listing count is small enough that a TTL-cached table scan +
Python tokenized scoring beats running an external SaaS search index.

Behavior:
  * Snapshot of all listings (every status) is held in a module-level cache
    and refreshed every 60s (or when empty). Refresh is a single unfiltered
    DDB scan — fine for <5000 listings. The in-Python `status == 'published'`
    filter inside search() is the load-bearing public-vs-seller gate, which
    lets include_unpublished=True actually surface drafts to seller-scoped
    callers.
  * search() filters/scores the cached snapshot in Python and returns the
    top `limit` hits. Scoring (per-token, additive): exact slug = 50, exact
    tag = 30, substring in name = 20, substring in description_md = 5.
    Zero-score listings are dropped. Sort is by score desc, then
    published_at desc.

The cache is intentionally process-local. Each backend Fargate task warms
its own copy on first request; with the 60s TTL, drift between tasks is
bounded. No invalidation hook on publish — the next request after TTL
expiry picks up the new listing.
"""

import time
from typing import Any

import boto3

from core.config import settings


# Module-level cache state. Reset between tests via _reset_cache().
_CACHE: list[dict[str, Any]] | None = None
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL_SECONDS: float = 60.0


def _listings_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)


def _reset_cache() -> None:
    """Test hook. Forces the next search() call to re-scan."""
    global _CACHE, _CACHE_TIMESTAMP
    _CACHE = None
    _CACHE_TIMESTAMP = 0.0


def _refresh_cache() -> list[dict[str, Any]]:
    """Scan all listings (every status) into the module-level cache.

    No FilterExpression is applied at scan time — the in-Python
    `status == "published"` gate inside search() is what enforces the
    public-vs-seller split. Filtering at the DDB layer would silently drop
    draft/review/retired/taken_down rows before include_unpublished=True
    could ever surface them.
    """
    global _CACHE, _CACHE_TIMESTAMP
    table = _listings_table()
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last
    _CACHE = items
    _CACHE_TIMESTAMP = time.time()
    return items


def _get_snapshot() -> list[dict[str, Any]]:
    """Return the cached snapshot, refreshing when expired or empty.

    Cache refresh is single-flight-NOT-implemented: if multiple concurrent
    requests arrive at TTL expiry, each may issue its own DDB scan (cache
    stampede). Benign at v0 scale (one Fargate task, low QPS, small listing
    count); upgrade to an asyncio.Lock around _refresh_cache() if scan
    amplification ever matters at scale.
    """
    if _CACHE is None or (time.time() - _CACHE_TIMESTAMP) > _CACHE_TTL_SECONDS:
        return _refresh_cache()
    return _CACHE


def _score(listing: dict[str, Any], tokens: list[str]) -> int:
    """Score a listing against tokenized query terms. See module docstring."""
    slug = (listing.get("slug") or "").lower()
    name = (listing.get("name") or "").lower()
    description = (listing.get("description_md") or "").lower()
    tags = {t.lower() for t in (listing.get("tags") or [])}

    score = 0
    for tok in tokens:
        if tok == slug:
            score += 50
        if tok in tags:
            score += 30
        if tok in name:
            score += 20
        if tok in description:
            score += 5
    return score


def _published_at_key(listing: dict[str, Any]) -> str:
    """Sort key for published_at-desc tiebreaker. None sorts last."""
    return listing.get("published_at") or ""


async def search(
    *,
    query: str | None,
    format: str | None = None,
    limit: int = 24,
    seller_id: str | None = None,
    include_unpublished: bool = False,
) -> list[dict[str, Any]]:
    """Search the cached published-listing snapshot.

    Args:
        query: Whitespace-tokenized search string. None or empty returns
            most-recent-published-first.
        format: "openclaw" | "skillmd" filter. None disables the filter.
        limit: Max results to return.
        seller_id: Optional Clerk user_id filter. The chat sidebar's
            featured-listings endpoint passes the curator's user_id here
            so the gallery only shows admin@isol8.co's published agents.
        include_unpublished: If True, return draft/review/retired/taken_down
            rows in addition to published. Caller must enforce auth (only
            seller-scoped reads should set this — the marketplace drawer's
            "My Listings" view passes seller_id=<self> + include_unpublished
            so a seller can see their own drafts). Default False = the
            public search semantics.

    Returns:
        Plain listing dicts (caller threads through Pydantic).
    """
    snapshot = _get_snapshot()

    if include_unpublished:
        # Caller has opted into seeing all statuses (seller-scoped reads).
        # Auth enforcement is the caller's responsibility.
        candidates = list(snapshot)
    else:
        # The cache holds every status (the DDB scan is unfiltered) — this
        # in-Python check is the load-bearing public-vs-seller gate that
        # keeps drafts/review/retired/taken_down out of public reads.
        candidates = [li for li in snapshot if li.get("status") == "published"]
    if format:
        candidates = [li for li in candidates if li.get("format") == format]
    if seller_id:
        candidates = [li for li in candidates if li.get("seller_id") == seller_id]

    tokens = [t.lower() for t in (query or "").split() if t]
    if not tokens:
        candidates.sort(key=_published_at_key, reverse=True)
        return candidates[:limit]

    scored: list[tuple[int, dict[str, Any]]] = []
    for li in candidates:
        s = _score(li, tokens)
        if s > 0:
            scored.append((s, li))

    scored.sort(key=lambda pair: (pair[0], _published_at_key(pair[1])), reverse=True)
    return [li for _, li in scored[:limit]]
