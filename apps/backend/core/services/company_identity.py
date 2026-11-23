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

logger = logging.getLogger(__name__)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LEN = 32
_FALLBACK_SLUG = "company"

# These become email local parts, so they must never be handed to a tenant.
#
# Reservations are PERMANENT — `company_slug_repo` has no delete path, so a
# name added here after the namespace goes live may already be held by a
# tenant. This list must be complete BEFORE `AGENT_EMAIL_DOMAIN` goes live.
RESERVED_SLUGS = frozenset(
    {
        "abuse",
        "admin",
        "administrator",
        "billing",
        "bounce",
        "contact",
        "dmarc",
        "flickmeon",
        "ftp",
        "help",
        "hostmaster",
        "info",
        "isol8",
        "lightbulb",
        "lightbulbai",
        "mail",
        "mailer-daemon",
        "no-reply",
        "noc",
        "noreply",
        "news",
        "postmaster",
        "root",
        "security",
        "support",
        "team",
        "usenet",
        "uucp",
        "webmaster",
        "www",
        # Company slugs are now subdomains of AGENT_EMAIL_DOMAIN (e.g.
        # {slug}.isol8.co), so these guard against a tenant slug shadowing
        # one of our own infrastructure subdomains.
        "api",
        "ws",
        "app",
        "dev",
        "staging",
        "prod",
        "cdn",
        "assets",
        "static",
        "control",
        "gateway",
        "auth",
        "login",
        "status",
        "docs",
        "blog",
        "smtp",
        "imap",
        "pop",
        "ns",
        "ns1",
        "ns2",
        "mx",
        "vpn",
        "test",
        # Compound infra subdomains — slugs are single hyphenated DNS labels,
        # and these real hosts are themselves hyphenated compounds, so the
        # single-word entries above (`api`, `ws`, `admin`, `app`, `test`)
        # don't cover them.
        "api-dev",
        "api-staging",
        "api-prod",
        "api-test",
        "ws-dev",
        "ws-staging",
        "ws-prod",
        "admin-dev",
        "admin-staging",
        "app-dev",
        "app-staging",
        # The product moved onto this domain (2026-10): these are its hosts.
        # `company` is deliberately absent: company.{root} is an auth origin
        # string that never resolves in DNS, and it is the fallback slug.
        "bv",
        "bv-dev",
        "clerk",
        "accounts",
        "clkmail",
        "about",
    }
)


def slugify(value: str) -> str:
    """Lowercase ``[a-z0-9-]`` form, capped at ``MAX_SLUG_LEN``.

    Non-ASCII is decomposed and stripped rather than transliterated — the
    result has to be a legal email local part, not a faithful rendering. That
    requirement is why this is not shared with ``agent_export._slugify`` (a
    frozen legacy-compat derivation).
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_only.lower()).strip("-")
    return slug[:MAX_SLUG_LEN].strip("-")


def slug_format_problem(slug: str) -> str | None:
    """``invalid`` or ``reserved`` -- the reasons that need no lookup."""
    if not slug or slugify(slug) != slug:
        return "invalid"
    return "reserved" if slug in RESERVED_SLUGS else None


async def slug_unavailable_reason(slug: str, *, user_id: str) -> str | None:
    """Why ``slug`` cannot be this user's email slug -- ``invalid``, ``reserved`` or
    ``taken`` -- or None when it is free (or already theirs). Read-only: the hold is the
    atomic claim, so a later race is still possible and the hold reports it."""
    problem = slug_format_problem(slug)
    if problem:
        return problem
    row = await company_slug_repo.get_by_slug(slug)
    if (
        row
        and row.get("owner_id") != company_slug_repo.pending_owner(user_id)
        and not company_slug_repo.is_stale_hold(row)
    ):
        return "taken"
    return None


async def allocate_company_slug(*, owner_id: str, name: str) -> str | None:
    """The owner's email slug: the one they chose at "Create your workspace" (handed over
    by the ``organization.created`` webhook), else -- only for workspaces created before
    that choice existed -- ``slugify(name)`` if it is free.

    There are NO numeric suffixes (owner ruling 2026-09-24): a derived slug that is taken
    or reserved returns None, and the caller reports ``slug_allocation_failed`` rather than
    minting ``name-2``. Never raises.
    """
    try:
        # `get_by_owner` reads the eventually consistent `by-owner` GSI; a same-slug re-entry
        # is still idempotent because `reserve`'s conditional put accepts the same owner.
        existing = await company_slug_repo.get_by_owner(owner_id)
        if existing:
            return existing["slug"]
        slug = slugify(name) or _FALLBACK_SLUG
        if slug not in RESERVED_SLUGS and await company_slug_repo.reserve(slug=slug, owner_id=owner_id):
            return slug
        logger.warning("company_identity: derived slug %r unavailable for owner %s", slug, owner_id)
        return None
    except Exception as e:  # noqa: BLE001 — allocation must not block provisioning
        logger.warning("company_identity: slug allocation failed for %s: %s", owner_id, e)
        return None
