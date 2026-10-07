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
bounded. No invalidation hook on publish — the next public request after
TTL expiry picks up the new listing.

Seller-scoped reads (``seller_id`` set + ``include_unpublished=True``)
bypass the cache and do a fresh DDB scan. This gives sellers
read-your-writes semantics: a listing newly created/submitted via the
Publish wizard is visible immediately on the next "My Listings" call,
instead of being shadowed by a stale 60s snapshot. The bypass is keyed on
``include_unpublished`` so the higher-traffic public path keeps cache
amplification protection.
"""

import time
from typing import Any

from core.repositories import marketplace_repo


# Query caps — clamp, don't 400: agent-generated queries can be arbitrarily
# long, and a failed tool call costs a model turn where truncation only
# degrades scoring. Bounds the per-request scoring work to tokens × listings.
_MAX_QUERY_CHARS = 200
_MAX_QUERY_TOKENS = 20

# Module-level cache state. Reset between tests via _reset_cache().
_CACHE: list[dict[str, Any]] | None = None
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL_SECONDS: float = 60.0


def _reset_cache() -> None:
    """Test hook. Forces the next search() call to re-scan."""
    global _CACHE, _CACHE_TIMESTAMP
    _CACHE = None
    _CACHE_TIMESTAMP = 0.0


async def _refresh_cache() -> list[dict[str, Any]]:
    """Scan all listings (every status) into the module-level cache.

    The repo scan is unfiltered — the in-Python `status == "published"` gate
    inside search() is what enforces the public-vs-seller split. Filtering
    at the DDB layer would silently drop draft/review/retired/taken_down
    rows before include_unpublished=True could ever surface them.
    """
    global _CACHE, _CACHE_TIMESTAMP
    items = await marketplace_repo.scan_all_listings()
    _CACHE = items
    # monotonic: cache age is an in-process elapsed check (stamped here,
    # compared in _get_snapshot), immune to wall-clock steps.
    _CACHE_TIMESTAMP = time.monotonic()
    return items


async def _get_snapshot() -> list[dict[str, Any]]:
    """Return the cached snapshot, refreshing when expired or empty.

    Cache refresh is single-flight-NOT-implemented: if multiple concurrent
    requests arrive at TTL expiry, each may issue its own DDB scan (cache
    stampede). Benign at v0 scale (one Fargate task, low QPS, small listing
    count); upgrade to an asyncio.Lock around _refresh_cache() if scan
    amplification ever matters at scale.
    """
    if _CACHE is None or (time.monotonic() - _CACHE_TIMESTAMP) > _CACHE_TTL_SECONDS:
        return await _refresh_cache()
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
    query: str | None = None,
    format: str | None = None,
    limit: int = 24,
    seller_id: str | None = None,
    category: str | None = None,
    free_only: bool = False,
    viewer_id: str | None = None,
    include_unpublished: bool = False,
    status_in: list[str] | None = None,
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
        category: Optional category filter (e.g., "operations", "sales").
            Compared case-insensitively against the listing's "category"
            field. Drives the Browse view's category-chip filtering.
        free_only: Keep only listings that cost the CALLER nothing — free
            rows (price_cents absent or <= 0), plus ``viewer_id``'s own
            paid rows, which deploy free through the seller-self bypass.
            Applied BEFORE the limit slice: a client that dropped paid rows
            from the returned page instead would lose the free listings that
            ranked below the cut, with no way to page for them. The mobile
            marketplace passes this — iOS hires free agents only (no in-app
            storefront), so a paid row is not a result there.
        viewer_id: Who is asking. Only read when ``free_only`` is set, to
            spare that caller their own paid listings. Makes the result
            caller-dependent, so the route must not let a shared cache hold
            that response.
        include_unpublished: If True, return draft/review/retired/taken_down
            rows in addition to published. Caller must enforce auth (only
            seller-scoped reads should set this — the marketplace drawer's
            "My Listings" view passes seller_id=<self> + include_unpublished
            so a seller can see their own drafts). Default False = the
            public search semantics.
        status_in: Optional explicit status whitelist (e.g., ["review"]).
            Applied BEFORE the limit slice so seller-scoped reads with a
            narrow status filter aren't lost when the snapshot has many
            published rows ahead of the matching ones. Honored regardless
            of include_unpublished — but in practice only seller-scoped
            callers pass it, since the public path filters to "published"
            anyway.

    Returns:
        Plain listing dicts (caller threads through Pydantic).

    Cache behavior:
        Public reads use a module-level 60s cache. Seller-scoped reads
        with ``include_unpublished=True`` bypass the cache and do a fresh
        DDB scan, so a seller sees their own newly-submitted listings
        immediately after the wizard's submit chain completes. Without
        the bypass, the publish→"View my listings" CTA could surface a
        stale snapshot for up to 60s.
    """
    if include_unpublished and seller_id:
        # Read-your-writes for seller-scoped reads: skip the public
        # snapshot cache and re-scan. Same paginated read path as
        # _refresh_cache() but the result is NOT written into _CACHE,
        # so a subsequent public request still sees the cached snapshot
        # (drift bounded by TTL as documented at module level).
        snapshot = await marketplace_repo.scan_all_listings()
    else:
        snapshot = await _get_snapshot()

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
    if category:
        cat_lower = category.lower()
        candidates = [li for li in candidates if str(li.get("category", "")).lower() == cat_lower]
    if free_only:
        candidates = [
            li
            for li in candidates
            if int(li.get("price_cents") or 0) <= 0 or (viewer_id is not None and li.get("seller_id") == viewer_id)
        ]
    if status_in:
        # Run BEFORE the limit slice — otherwise a seller with >limit rows
        # in unrelated statuses ahead of the matching ones loses them to
        # the limit cut.
        wanted = {s for s in status_in if s}
        if wanted:
            candidates = [li for li in candidates if li.get("status") in wanted]

    tokens = [t.lower() for t in (query or "")[:_MAX_QUERY_CHARS].split() if t][:_MAX_QUERY_TOKENS]
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
