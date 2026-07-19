"""Company display name + platform-unique slug.

The name is auto-generated at provisioning and editable afterwards (via
``PATCH /teams/settings``). The slug is derived from that name ONCE, at
provisioning, and then held independently: renaming the company does NOT
move the slug, because the slug becomes an identifier other people have
been given (it is the local part of the agent's email address), not a
label. Changing the address is a separate, deliberate action.

Both operations are best-effort — ``provision_org`` must survive either
failing.
"""

import logging
import re
import unicodedata

from core.repositories import company_slug_repo
from core.services import clerk_admin

logger = logging.getLogger(__name__)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LEN = 32
_MAX_SUFFIX_ATTEMPTS = 50
_FALLBACK_SLUG = "company"

# These become email local parts, so they must never be handed to a tenant.
RESERVED_SLUGS = frozenset(
    {
        "abuse",
        "admin",
        "administrator",
        "billing",
        "contact",
        "help",
        "hostmaster",
        "info",
        "isol8",
        "mail",
        "no-reply",
        "noreply",
        "postmaster",
        "root",
        "security",
        "support",
        "team",
        "webmaster",
        "www",
    }
)


def slugify(value: str) -> str:
    """Lowercase ``[a-z0-9-]`` form, capped at ``MAX_SLUG_LEN``.

    Non-ASCII is decomposed and stripped rather than transliterated — the
    result has to be a legal email local part, not a faithful rendering.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_only.lower()).strip("-")
    return slug[:MAX_SLUG_LEN].strip("-")


def prettify_email_local_part(email: str) -> str:
    """``jane.doe+tag@gmail.com`` -> ``Jane Doe``."""
    local = email.split("@", 1)[0]
    local = local.split("+", 1)[0]
    words = [w for w in re.split(r"[._\-]+", local) if w]
    return " ".join(w.capitalize() for w in words) or local


async def generate_company_name(*, org_id: str | None, owner_email: str) -> str:
    """The initial company name.

    Org owners get their Clerk organization's real name; personal owners
    get a prettified form of their email local part. Never raises — a
    Clerk hiccup falls back to the email.
    """
    if org_id:
        try:
            org = await clerk_admin.get_organization(org_id)
        except Exception as e:  # noqa: BLE001 — naming must not block provisioning
            logger.warning("company_identity: Clerk org lookup failed for %s: %s", org_id, e)
            org = None
        if org:
            try:
                name = (org.get("name") or "").strip()
            except AttributeError:
                # Malformed Clerk payload (e.g. `name` isn't a string) — fall
                # back to the email rather than propagate.
                name = ""
            if name:
                return name
    return prettify_email_local_part(owner_email)


async def allocate_company_slug(*, owner_id: str, name: str) -> str | None:
    """Reserve a unique slug derived from ``name``.

    Returns the reserved slug, or None if allocation failed. Collisions and
    reserved words take a numeric suffix, with the base shortened as needed
    so the suffixed candidate never exceeds ``MAX_SLUG_LEN``. Never raises.
    """
    try:
        base = slugify(name) or _FALLBACK_SLUG
        for attempt in range(1, _MAX_SUFFIX_ATTEMPTS + 1):
            if attempt == 1:
                candidate = base
            else:
                # Re-cap so `base + suffix` fits in MAX_SLUG_LEN — truncating
                # `f"{base}-{attempt}"` directly would chop the suffix itself
                # and reintroduce collisions. Strip a trailing hyphen left by
                # the truncation so we don't produce "foo--2".
                suffix = f"-{attempt}"
                max_base_len = MAX_SLUG_LEN - len(suffix)
                candidate = f"{base[:max_base_len].rstrip('-')}{suffix}"
            # A reserved word is skipped, not failed: "support" becomes
            # "support-2", which is a fine tenant slug.
            if candidate in RESERVED_SLUGS:
                continue
            if await company_slug_repo.reserve(slug=candidate, owner_id=owner_id):
                return candidate
        logger.warning(
            "company_identity: no free slug for owner %s after %d attempts (base=%s)",
            owner_id,
            _MAX_SUFFIX_ATTEMPTS,
            base,
        )
        return None
    except Exception as e:  # noqa: BLE001 — allocation must not block provisioning
        logger.warning("company_identity: slug allocation failed for %s: %s", owner_id, e)
        return None
