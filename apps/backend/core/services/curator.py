"""Resolve the marketplace curator's Clerk user_id.

The chat sidebar's "From the marketplace" gallery shows listings published
by a single curator account — admin@isol8.co — treated as a regular
marketplace seller (no special flag, no priviliged publish path). This
module turns the configured curator email into the Clerk user_id that
sits on those listings' ``seller_id`` field, so the sidebar's featured
endpoint can filter the published-listings snapshot by it.

Resolution is lazy + cached for the app's lifetime: the first caller does
the Clerk lookup, stores the result, and every subsequent call returns
the cached value. Only a HIT is cached, so a miss is retried on the next
call; and the cached value is a Clerk user_id, which survives the account
changing its email — so there is nothing for a TTL to refresh.

What a stale literal DID break is the lookup itself: the curator mailbox
moving to a new domain made ``find_user_by_email`` miss forever and the
gallery render empty with no other signal. So the address is derived from
the product apex first (``admin@<root domain>``) and the historical
constant is tried as a fallback — either one resolving is enough, and the
two are the same address until the domain moves.
"""

from __future__ import annotations

import asyncio
import logging

from core.config import settings
from core.constants import ISOL8_CURATOR_EMAIL
from core.services import clerk_admin


logger = logging.getLogger(__name__)


_curator_user_id: str | None = None
_lock = asyncio.Lock()


def _candidate_emails() -> tuple[str, ...]:
    """Curator addresses to try, current-domain first.

    The product apex moves (a rebrand, a domain migration) before the Clerk
    account's primary email does, so try the same local part at the current
    apex and fall back to the historical constant. Deduped — today both are
    ``admin@isol8.co``, so this is exactly one Clerk lookup.
    """
    local = ISOL8_CURATOR_EMAIL.split("@", 1)[0]
    apex = settings.root_domain
    candidates = (f"{local}@{apex}" if apex else "", ISOL8_CURATOR_EMAIL)
    return tuple(dict.fromkeys(e for e in candidates if e))


async def get_curator_user_id() -> str | None:
    """Return the Clerk user_id of the curator account, or None if Clerk has
    no such user yet (returned to callers as "no featured listings", which the
    sidebar renders as the empty-state).

    Idempotent + concurrent-safe: a lock guards against duplicate Clerk
    calls during the first concurrent burst on a fresh backend.
    """
    global _curator_user_id
    if _curator_user_id is not None:
        return _curator_user_id
    async with _lock:
        if _curator_user_id is not None:
            return _curator_user_id
        candidates = _candidate_emails()
        for email in candidates:
            user = await clerk_admin.find_user_by_email(email)
            if user:
                _curator_user_id = user.get("id")
                return _curator_user_id
        logger.info(
            "curator account not found in Clerk yet (tried %s) — sidebar gallery will render empty until it onboards",
            ", ".join(candidates),
        )
        return None
