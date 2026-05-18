"""Resolve a marketplace seller's public byline.

Layered lookup — the first source that returns a non-empty value wins:

  1. ``seller_profile`` — the seller has picked a handle in the new
     ``marketplace-sellers`` table. We surface ``@{handle}`` as the
     byline. This is the canonical post-rollout path.
  2. Curator special-case — the configured ``admin@isol8.co`` account
     resolves to ``@isol8`` even before it has a profile row, so
     featured listings always read correctly.
  3. Clerk derivation — first_name + last_name from the Clerk profile,
     falling back to the email's local-part. Used for sellers who
     published before the handle gate landed.
  4. Final fallback — ``"Community member"`` so the UI never has to
     handle a missing byline.

Resolution is cached per-process for ``_TTL_SECONDS`` so a 24-row
listing page makes at most N unique back-end calls.
:func:`invalidate_seller` lets the profile-write path evict a single
seller's cache entry so a new handle is reflected immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.services import clerk_admin, curator


logger = logging.getLogger(__name__)

CURATOR_HANDLE = "isol8"
CURATOR_BYLINE = f"@{CURATOR_HANDLE}"
FALLBACK_NAME = "Community member"

_TTL_SECONDS = 3600
_cache: dict[str, tuple[str, float]] = {}
_lock = asyncio.Lock()


def invalidate_seller(seller_id: str) -> None:
    """Drop a single seller's cache entry. Safe to call on miss."""
    _cache.pop(seller_id, None)


async def resolve_display_name(seller_id: str) -> str:
    if not seller_id:
        return FALLBACK_NAME

    now = time.time()
    cached = _cache.get(seller_id)
    if cached and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    async with _lock:
        cached = _cache.get(seller_id)
        if cached and time.time() - cached[1] < _TTL_SECONDS:
            return cached[0]

        name = await _resolve_uncached(seller_id)
        _cache[seller_id] = (name, time.time())
        return name


async def resolve_display_names(seller_ids: list[str]) -> dict[str, str]:
    unique = list(dict.fromkeys(s for s in seller_ids if s))
    results: dict[str, str] = {}
    for sid in unique:
        results[sid] = await resolve_display_name(sid)
    return results


async def attach_seller_names(items: list[dict]) -> list[dict]:
    seller_ids = [item.get("seller_id", "") for item in items]
    names = await resolve_display_names(seller_ids)
    for item in items:
        if item.get("seller_display_name"):
            continue
        sid = item.get("seller_id")
        item["seller_display_name"] = names.get(sid, FALLBACK_NAME) if sid else FALLBACK_NAME
    return items


async def attach_seller_name(item: dict) -> dict:
    if not item:
        return item
    if item.get("seller_display_name"):
        return item
    sid = item.get("seller_id")
    item["seller_display_name"] = await resolve_display_name(sid) if sid else FALLBACK_NAME
    return item


async def _resolve_uncached(seller_id: str) -> str:
    # 1. New: marketplace-sellers profile row.
    # Deferred import — seller_profile.upsert_profile imports back into
    # this module to invalidate the cache. Top-level import would create
    # a cycle at module-load time.
    from core.services import seller_profile

    try:
        profile = await seller_profile.get_profile(seller_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("seller_profile lookup failed for %s: %s", seller_id, exc)
        profile = None
    if profile and profile.get("handle"):
        return f"@{profile['handle']}"

    # 2. Curator special-case — always render as @isol8 even pre-onboard.
    curator_id = await curator.get_curator_user_id()
    if curator_id and seller_id == curator_id:
        return CURATOR_BYLINE

    # 3. Clerk fallback for sellers who haven't onboarded the new flow.
    user = await clerk_admin.get_user(seller_id)
    if not user:
        return FALLBACK_NAME

    return _name_from_user(user) or FALLBACK_NAME


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
    for entry in emails:
        address = entry.get("email_address") or ""
        local = address.split("@", 1)[0].strip()
        if local:
            return local
    return None


def _reset_cache_for_tests() -> None:
    _cache.clear()
