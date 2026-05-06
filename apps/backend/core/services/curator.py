"""Resolve the marketplace curator's Clerk user_id.

The chat sidebar's "From the marketplace" gallery shows listings published
by a single curator account — admin@isol8.co — treated as a regular
marketplace seller (no special flag, no priviliged publish path). This
module turns the configured curator email into the Clerk user_id that
sits on those listings' ``seller_id`` field, so the sidebar's featured
endpoint can filter the published-listings snapshot by it.

Resolution is lazy + cached for the app's lifetime: the first caller does
the Clerk lookup, stores the result, and every subsequent call returns
the cached value. The cache is intentionally not TTL'd — the
email→user_id mapping is stable in practice; restart the backend if it
ever changes.
"""

from __future__ import annotations

import asyncio
import logging

from core.constants import ISOL8_CURATOR_EMAIL
from core.services import clerk_admin


logger = logging.getLogger(__name__)


_curator_user_id: str | None = None
_lock = asyncio.Lock()


async def get_curator_user_id() -> str | None:
    """Return the Clerk user_id of the curator account (admin@isol8.co), or
    None if Clerk has no such user yet (returned to callers as "no featured
    listings", which the sidebar renders as the empty-state).

    Idempotent + concurrent-safe: a lock guards against duplicate Clerk
    calls during the first concurrent burst on a fresh backend.
    """
    global _curator_user_id
    if _curator_user_id is not None:
        return _curator_user_id
    async with _lock:
        if _curator_user_id is not None:
            return _curator_user_id
        user = await clerk_admin.find_user_by_email(ISOL8_CURATOR_EMAIL)
        if not user:
            logger.info(
                "curator account %s not found in Clerk yet — sidebar gallery will render empty until it onboards",
                ISOL8_CURATOR_EMAIL,
            )
            return None
        _curator_user_id = user.get("id")
        return _curator_user_id


def _reset_cache_for_tests() -> None:
    """Test hook — reset the module-level cache so each test starts fresh."""
    global _curator_user_id
    _curator_user_id = None
