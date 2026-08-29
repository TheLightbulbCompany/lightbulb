"""The provisioning rail — /api/v1/company/provisioning/*.

The backend owns the Provisioning API loop (spec §3.2). Friday asks through the
container-authed ``/u/{owner_id}/…`` routes; the owner acts through the
Clerk-authed routes and the Inbox cards. No credential value, checkout URL,
redirect URL or deep link ever crosses the container door.

Order on every route: authenticate (hook token or Clerk), then the paid gate,
then side effects.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services import provisioning_landing
from core.services.owner_facts import write_owner_facts
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

# `provider` becomes part of a Secrets Manager secret NAME and the integrations
# sort key; `resource_id` goes into a URL path. The body comes from an
# LLM-driven agent, so this is a trust boundary, not a formality.
#
# Matched with `fullmatch`, never `match`: `$` also matches before a trailing
# newline, so `match` would accept "posthog\n" as a secret-name component.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
# Control characters and whitespace: a newline in a URL or an env value splits
# whatever renders or consumes it (approval card, shell env, config file).
_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_URL_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_MAX_ENV_VARS = 25
_MAX_ENV_VALUE = 8192
_MAX_URL = 2048

PAID_MESSAGE = "Provisioning needs an active subscription"


def _slug(v: str) -> str:
    if not _NAME_RE.fullmatch(v):
        raise ValueError("must be a lowercase slug")
    return v


def _plain(v: str) -> str:
    """Free text the owner READS on an approval card, or that becomes an env
    value / a Secrets Manager-adjacent field. A newline splits whatever renders
    it, so the same control-char rule the env values already get applies here."""
    if _UNSAFE_RE.search(v):
        raise ValueError("must not contain control characters")
    return v


def _url(v: str) -> str:
    """Every URL on this rail is rendered to the owner as a click target."""
    if not v:
        return v
    if _UNSAFE_URL_RE.search(v):
        raise ValueError("URL must not contain whitespace or control characters")
    if not v.startswith("https://"):
        raise ValueError("must be an https:// URL")
    return v


# ---------------------------------------------------------------------------
# Owner-facing (Clerk). Paid-only and org-admin-only for the mutation, exactly
# like P0a's company-Stripe router. There is deliberately NO catalog or
# provision route here: provisioning is Friday's job (spec §3.1's trigger
# split), and the owner's surface is the two approval cards plus this list.
# ---------------------------------------------------------------------------


def _gate_admin(auth: AuthContext) -> None:
    if auth.is_org_context:
        require_org_admin(auth)


@router.get("/resources")
async def resources(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    _state, rows = await provisioning_landing.provisioning_summary(owner_id)
    return {"resources": rows}


@router.post("/resources/{provider}/{resource_id}/revoke")
async def revoke(provider: str, resource_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Delete OUR credential copy and pointer. See ``provisioning_landing.revoke_credentials``
    for what "our copy" means and why it can't just delete the whole secret."""
    if not _NAME_RE.fullmatch(provider) or not _RESOURCE_ID_RE.fullmatch(resource_id):
        raise HTTPException(status_code=422, detail="bad provider or resource id")
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    revoked = await provisioning_landing.revoke_credentials(
        owner_id=owner_id, provider=provider, resource_id=resource_id
    )
    if not revoked:
        raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
    await write_owner_facts(owner_id)
    return {"revoked": True}
