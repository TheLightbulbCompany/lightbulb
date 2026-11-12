"""Agent purchases: whether a purchase needs a person's yes before the effects route lets it through.

The person's yes is the Needs-you Question. The effects route asks one for a purchase unless "ask
before moving money" is off and the merchant is allowlisted (``needs_person``).
"""

from __future__ import annotations

from core.repositories import billing_repo
from core.services import website_logins


def _allowlisted(limits: dict, site: str) -> bool:
    return any(website_logins.site_of(m) == site for m in limits["merchants"])


async def needs_person(owner_id: str, url: str) -> bool:
    """A person's yes (an answered Question) must come first: "ask before moving money" (``money_moves``,
    default on) is on, or the merchant is not on the owner's allowlist."""
    if (await billing_repo.get_capability_policy(owner_id))["money_moves"]:
        return True
    try:
        site = website_logins.site_of(website_logins.normalize_origin(url))
    except website_logins.LoginError:
        return True
    return not _allowlisted(await billing_repo.get_purchase_limits(owner_id), site)
