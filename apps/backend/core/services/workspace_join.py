"""Workspace join links — mint / describe / revoke / redeem.

A join link is the ONE way a human gets into an existing Lightbulb workspace.
Everything downstream of the Clerk membership already exists and is untouched by
this module: ``routers/webhooks.py`` handles ``organizationMembership.created``,
and the new member's container access is resolved by ``ensure_user_ready`` on
their first ``/container/status`` poll (org members ride the owner's shared
container). This module's whole job is the missing front door.

Shape, per the owner's decisions:

* ONE link per workspace, not one per invitee.
* Regenerating invalidates the previous link (a single PutItem over the org's
  row — see ``workspace_join_repo``).
* No expiry.
* Open join: whoever holds the link and is signed in joins, no approval step.
* Everyone joins as ``org:member``. There is no role parameter anywhere in this
  module, by design — billing is per workspace, not per seat, and an admin role
  is granted in Clerk, not handed out by a link.

**The link is a credential.** It admits its holder to a workspace containing the
owner's agents, files and connected accounts. So:

* 32 CSPRNG bytes (``secrets.token_urlsafe(32)``, ~256 bits) — brute-forcing the
  hash space is not a threat model anyone can reach through a GetItem-per-guess
  endpoint, which is why this module adds no rate limiter (the repo has no
  rate-limiting primitive to reuse; see the router's note).
* Stored as a SHA-256 hash (what redeem compares against) PLUS a Fernet
  ciphertext under the platform ``ENCRYPTION_KEY``, so ``describe_join_link``
  can hand the admin the SAME link again. Hash-only was the first design, and
  it made every re-share a regenerate — which invalidates the link for everyone
  who received it earlier and has not clicked yet (a classroom workspace hit
  seven mints and nine stale-link rejections in two days). A leaked table row
  is still not a usable link without the key.
* Compared with ``hmac.compare_digest`` — the hashes are not secret-equal-length
  comparisons that leak much, but a constant-time compare costs nothing.
* Never logged. The token is not an argument to any ``logger`` call here, and
  every redeem failure returns the SAME opaque message so a caller cannot tell a
  malformed token from a revoked one from an org that never had a link.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import HTTPException

from core import encryption
from core.config import settings
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.repositories import workspace_join_repo
from core.services import clerk_admin

logger = logging.getLogger(__name__)

# Everyone joins as a plain member. Not configurable — see the module docstring.
JOIN_ROLE = "org:member"

# 32 bytes -> 43 url-safe chars, ~256 bits of entropy.
_TOKEN_BYTES = 32

# One message for every redeem failure. Distinguishing "revoked" from "never
# existed" from "wrong org" would turn the endpoint into an oracle.
_INVALID_LINK = "This join link is no longer valid. Ask a workspace admin for a new one."


def _hash_token(token: str) -> str:
    """SHA-256 hex of the plaintext token — what the row stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_join_url(org_id: str, token: str) -> str:
    """The shareable URL for a freshly minted token.

    ``/join/{org_id}#{token}`` — the public route identifies the workspace and
    the fragment carries the credential without sending it in an HTTP request.

    The token lives in the URL FRAGMENT, never in a request path or query: the
    browser does not send fragments to Vercel, Clerk, API Gateway, or ALB, so
    the credential stays out of access logs and Referer headers. The client
    stores it in sessionStorage while a signed-out holder authenticates, then
    POSTs it in the redeem request body.
    """
    return f"{settings.FRONTEND_URL.rstrip('/')}/join/{org_id}#{token}"


async def mint_join_link(org_id: str, *, created_by: str) -> dict:
    """Create (or replace) the org's join link.

    Returns ``{join_url, created_at, created_by}``. Keep the plaintext out of
    logs; ``describe_join_link`` can re-derive it from the stored ciphertext.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = await workspace_join_repo.put_link(
        org_id,
        token_hash=_hash_token(token),
        token_enc=encryption.encrypt(token),
        created_by=created_by,
    )
    put_metric("orgs.join_link.minted")
    logger.info("orgs.join_link.minted org_id=%s created_by=%s", org_id, created_by)
    return {
        "join_url": build_join_url(org_id, token),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


async def describe_join_link(org_id: str) -> dict | None:
    """The org's live link — its URL plus metadata — or None when there is none.

    ``join_url`` is None for a row the hash-only code wrote (no ciphertext) or
    one the current key cannot decrypt; the UI reads that as "regenerate once
    to get a copyable link". Never includes the hash or the ciphertext.
    """
    row = await workspace_join_repo.get_link(org_id)
    if not row:
        return None
    join_url = None
    if row.get("token_enc"):
        try:
            join_url = build_join_url(org_id, encryption.decrypt(row["token_enc"]))
        except (ValueError, RuntimeError):
            # ValueError: rotated key or corrupt row. RuntimeError: no key
            # configured at all. Either way the link still redeems; it just
            # cannot be shown.
            logger.warning("orgs.join_link.undecryptable org_id=%s", org_id)
    return {
        "join_url": join_url,
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by"),
    }


async def revoke_join_link(org_id: str) -> None:
    """Turn the org's link off. Idempotent."""
    await workspace_join_repo.delete_link(org_id)
    put_metric("orgs.join_link.revoked")
    logger.info("orgs.join_link.revoked org_id=%s", org_id)


async def redeem_join_link(org_id: str, token: str, *, user_id: str) -> dict:
    """Add ``user_id`` to ``org_id`` if ``token`` is that org's live join link.

    Returns ``{"status": "joined" | "already_member", "org_id", "org_name",
    "org_slug"}``. Raises ``HTTPException(404, _INVALID_LINK)`` for every
    rejection — unknown org, revoked link, wrong token.

    Already-a-member is an idempotent SUCCESS, not an error: the owner clicking
    their own link, or a member re-opening it from an old message, should land
    in the workspace rather than on an error page.
    """
    row = await workspace_join_repo.get_link(org_id)
    stored = (row or {}).get("token_hash") or ""
    if not stored or not hmac.compare_digest(stored, _hash_token(token)):
        # Deliberately identical for every cause, and the token is not logged.
        put_metric("orgs.join_link.rejected")
        logger.info("orgs.join_link.rejected org_id=%s user_id=%s", org_id, user_id)
        raise HTTPException(status_code=404, detail=_INVALID_LINK)

    result = await clerk_admin.create_organization_membership(
        org_id=org_id,
        user_id=user_id,
        role=JOIN_ROLE,
    )
    status = result.get("status") or "joined"
    put_metric("orgs.join_link.redeemed", dimensions={"status": status})
    logger.info("orgs.join_link.redeemed org_id=%s user_id=%s status=%s", org_id, user_id, status)

    org = await clerk_admin.get_organization(org_id, include_members_count=True) or {}
    # The growth funnel's one durable fact, on the PERSON who joined: who let them in and
    # how many people the workspace now has. Content-free; the token is never a property.
    capture_product_event(
        distinct_id=user_id,
        event="join_link_redeemed",
        properties={
            "workspace_id": org_id,
            "status": status,
            "invited_by": (row or {}).get("created_by"),
            "members_count": org.get("members_count"),
        },
    )
    return {
        "status": status,
        "org_id": org_id,
        "org_name": org.get("name") or "",
        "org_slug": org.get("slug") or "",
    }
