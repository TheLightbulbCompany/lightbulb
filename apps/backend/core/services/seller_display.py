"""Resolve a marketplace seller's display name from their Clerk user_id.

The marketplace stores ``seller_id`` as a raw Clerk user_id (e.g.
``user_3DEyqezB5b3wmW97mgsZFnIlSwJ``). Listing responses carry that field
through to the storefront, which previously rendered "by user_3DEy…"
literally. This module turns the id into something a human reads:

  - The curator account (``admin@isol8.co``) → "isol8".
  - Any other seller → ``first_name + last_name`` from Clerk, falling
    back to the email's local-part, then the generic "Community member".

Resolution is cached per-process for ``_TTL_SECONDS`` so a single listing
page doesn't trigger one Clerk call per row, and a refresh-heavy session
doesn't hammer Clerk. The cache is intentionally not multi-process —
each backend task gets its own and warms naturally; names change rarely
enough that brief skew across tasks doesn't matter.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.services import clerk_admin, curator


logger = logging.getLogger(__name__)

CURATOR_NAME = "isol8"
FALLBACK_NAME = "Community member"

_TTL_SECONDS = 3600
_cache: dict[str, tuple[str, float]] = {}
_lock = asyncio.Lock()


async def resolve_display_name(seller_id: str) -> str:
    """Return a human-readable display name for the given seller_id.

    Always returns a string — never raises, never returns the raw id.
    On any lookup failure (no Clerk key, network blip, unknown user) the
    caller gets :data:`FALLBACK_NAME` so the UI has something to render.
    """
    if not seller_id:
        return FALLBACK_NAME

    now = time.time()
    cached = _cache.get(seller_id)
    if cached and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    async with _lock:
        # Re-check under the lock so concurrent first-callers collapse to one
        # Clerk fetch per seller_id.
        cached = _cache.get(seller_id)
        if cached and time.time() - cached[1] < _TTL_SECONDS:
            return cached[0]

        name = await _resolve_uncached(seller_id)
        _cache[seller_id] = (name, time.time())
        return name


async def resolve_display_names(seller_ids: list[str]) -> dict[str, str]:
    """Batch-resolve a list of seller_ids to display names.

    Deduplicates the input so repeated ids only hit the resolver once.
    Returns a dict keyed by the original seller_ids; any failures resolve
    to :data:`FALLBACK_NAME` rather than being omitted.
    """
    unique = list(dict.fromkeys(s for s in seller_ids if s))
    results: dict[str, str] = {}
    for sid in unique:
        results[sid] = await resolve_display_name(sid)
    return results


async def attach_seller_names(items: list[dict]) -> list[dict]:
    """Mutate each item in-place, adding ``seller_display_name``.

    Returns the same list for caller chaining convenience. Items without
    a ``seller_id`` get :data:`FALLBACK_NAME`. Items that already carry
    the field are not overwritten.
    """
    seller_ids = [item.get("seller_id", "") for item in items]
    names = await resolve_display_names(seller_ids)
    for item in items:
        if "seller_display_name" in item and item["seller_display_name"]:
            continue
        sid = item.get("seller_id")
        item["seller_display_name"] = names.get(sid, FALLBACK_NAME) if sid else FALLBACK_NAME
    return items


async def attach_seller_name(item: dict) -> dict:
    """Single-item variant of :func:`attach_seller_names`."""
    if not item:
        return item
    if item.get("seller_display_name"):
        return item
    sid = item.get("seller_id")
    item["seller_display_name"] = await resolve_display_name(sid) if sid else FALLBACK_NAME
    return item


async def _resolve_uncached(seller_id: str) -> str:
    curator_id = await curator.get_curator_user_id()
    if curator_id and seller_id == curator_id:
        return CURATOR_NAME

    user = await clerk_admin.get_user(seller_id)
    if not user:
        return FALLBACK_NAME

    name = _name_from_user(user)
    return name or FALLBACK_NAME


def _name_from_user(user: dict) -> str | None:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()

    emails = user.get("email_addresses") or []
    primary_id = user.get("primary_email_address_id")
    for entry in emails:
        if entry.get("id") == primary_id:
            address = entry.get("email_address") or ""
            local = address.split("@", 1)[0].strip()
            if local:
                return local
            break

    # Fall back to the first email if no primary marker is set.
    for entry in emails:
        address = entry.get("email_address") or ""
        local = address.split("@", 1)[0].strip()
        if local:
            return local
    return None


def _reset_cache_for_tests() -> None:
    """Test hook — clear the module-level cache."""
    _cache.clear()
