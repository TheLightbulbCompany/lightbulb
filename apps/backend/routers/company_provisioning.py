"""The provisioning rail — /api/v1/company/provisioning/*.

Friday drives the Stripe CLI inside her own container (spec §3.2: "Friday runs
`next_step`"). The backend never shells into a container. These `/u/{owner_id}/*`
routes are container-authed with the owner's per-container hook token and carry
the platform's half: file the two approval cards the owner must click, and land
the credentials she reports back.

Order on every route: hook token, then the paid gate, then side effects.
Provisioning is autonomy — the same thing the subscription buys everywhere else
— and a trial container that scales to zero cannot run the rail it would be
starting, so a card filed for a trial owner is a card nobody can act on.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import integrations_repo
from core.services import config_patcher, provisioning_approvals, provisioning_landing
from core.services.config_patcher import ConfigPatchError
from core.services.credentials import integration_secret_store
from core.services.owner_facts import write_owner_facts
from routers.agent_builder import _require_hook_token
from routers.company_board import file_card
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

# The pairing URL is shown to the owner as a click target, so it may only ever
# be Stripe's own confirm host.
_STRIPE_LOGIN_PREFIX = "https://dashboard.stripe.com/"

PAID_MESSAGE = "Provisioning needs an active subscription"


def _slug(v: str) -> str:
    if not _NAME_RE.fullmatch(v):
        raise ValueError("must be a lowercase slug")
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


class PairingBody(BaseModel):
    browser_url: str = Field(max_length=_MAX_URL)
    verification_code: str = Field(min_length=1, max_length=64)
    issue_id: str | None = Field(default=None, max_length=128)

    @field_validator("browser_url")
    @classmethod
    def _stripe_only(cls, v: str) -> str:
        _url(v)
        if not v.startswith(_STRIPE_LOGIN_PREFIX):
            raise ValueError("browser_url must be a dashboard.stripe.com URL")
        return v


@router.post("/u/{owner_id}/pairing")
async def request_pairing(owner_id: str, body: PairingBody, request: Request):
    """Friday already ran `stripe login --non-interactive`; file the card."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    approval_id = await file_card(
        owner_id,
        provisioning_approvals.request_cli_pairing(
            owner_id=owner_id,
            issue_id=body.issue_id,
            browser_url=body.browser_url,
            verification_code=body.verification_code,
        ),
    )
    return {"approval_id": approval_id, "status": "filed" if approval_id else "no_company"}


class ConsentBody(BaseModel):
    provider: str
    service: str
    terms_url: str = Field(default="", max_length=_MAX_URL)
    pricing: str = Field(default="", max_length=500)
    issue_id: str | None = Field(default=None, max_length=128)

    _check_slug = field_validator("provider", "service")(_slug)
    _check_url = field_validator("terms_url")(_url)


@router.post("/u/{owner_id}/consent")
async def request_consent(owner_id: str, body: ConsentBody, request: Request):
    """The owner's one decision per provider: the terms and the price."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    approval_id = await file_card(
        owner_id,
        provisioning_approvals.request_provider_consent(
            owner_id=owner_id,
            issue_id=body.issue_id,
            provider=body.provider,
            service=body.service,
            terms_url=body.terms_url,
            pricing=body.pricing,
        ),
    )
    return {"approval_id": approval_id, "status": "filed" if approval_id else "no_company"}


class LandedBody(BaseModel):
    provider: str
    service: str
    resource_id: str
    resource_name: str = Field(default="", max_length=200)
    env: dict[str, str] = Field(default_factory=dict)
    dashboard_url: str | None = Field(default=None, max_length=_MAX_URL)

    _check_slug = field_validator("provider", "service")(_slug)

    @field_validator("resource_id")
    @classmethod
    def _resource_id(cls, v: str) -> str:
        if not _RESOURCE_ID_RE.fullmatch(v):
            raise ValueError("resource_id has an unexpected shape")
        return v

    @field_validator("dashboard_url")
    @classmethod
    def _dashboard(cls, v: str | None) -> str | None:
        return None if v is None else _url(v)

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_ENV_VARS:
            raise ValueError(f"at most {_MAX_ENV_VARS} environment variables")
        for key, value in v.items():
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError("environment variable names must be UPPER_SNAKE")
            if not value:
                # A blank would overwrite a live credential with nothing.
                raise ValueError("environment variable value must not be empty")
            if len(value) > _MAX_ENV_VALUE:
                raise ValueError("environment variable value is too large")
            if _UNSAFE_RE.search(value):
                raise ValueError("environment variable value must not contain control characters")
        return v


@router.post("/u/{owner_id}/landed")
async def landed(owner_id: str, body: LandedBody, request: Request):
    """Friday reports a successful `stripe projects add`. We store OUR copy.

    Never logs a value. The response echoes the POINTER only — env var names,
    never values (spec §2: we hold pointers, the provider holds the truth).
    `env_keys` is what a later reprovision replays through
    ``provisioning_landing.build_provisioned_env``, so it must be the exact set
    of names landed here.
    """
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    resource = {
        "resource_id": body.resource_id,
        "provider": body.provider,
        "service": body.service,
        "resource_name": body.resource_name,
        "status": "active",
        "env_keys": sorted(body.env),
        "dashboard_url": body.dashboard_url,
        # Everything P0b provisions is company-level (spec §3.4). Agent-level
        # resources ride their own rails; the Accounts row renders this.
        "level": "company",
    }
    await provisioning_landing.land_credentials(owner_id=owner_id, resource=resource, env=body.env)
    await write_owner_facts(owner_id)
    return resource


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
    stripe_cli, rows = await provisioning_landing.provisioning_summary(owner_id)
    return {"resources": rows, "stripe_cli": stripe_cli}


@router.post("/resources/{provider}/{resource_id}/revoke")
async def revoke(provider: str, resource_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Delete OUR credential copy and pointer.

    Deliberately does NOT remove the provider resource: it is the owner's and
    stays theirs (spec §3.6). Removing it is a separate, explicit action they
    take in the provider's own dashboard.

    The ``(owner, provider)`` Secrets Manager blob is SHARED with the OAuth
    integrations rail (same as ``provisioning_landing`` — see its module
    docstring), so this must never delete the whole blob: that would also
    destroy an OAuth credential the owner connected separately. Instead it
    reads the REVOKED resource's own ``env_keys``, drops only those names from
    the blob, and only force-deletes the secret when nothing is left.
    """
    if not _NAME_RE.fullmatch(provider) or not _RESOURCE_ID_RE.fullmatch(resource_id):
        raise HTTPException(status_code=422, detail="bad provider or resource id")
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)

    existing = await integrations_repo.list_provider_resources(owner_id, provider)
    target = next((r for r in existing if r.get("resource_id") == resource_id), None)
    env_keys = list((target or {}).get("env_keys") or [])

    if env_keys:
        blob = await integration_secret_store.get(owner_id, provider) or {}
        for key in env_keys:
            blob.pop(key, None)
        if blob:
            await integration_secret_store.put(owner_id, provider, blob)
        else:
            await integration_secret_store.delete(owner_id, provider)

    await integrations_repo.remove_provider_resource(owner_id, provider, resource_id)

    for key in env_keys:
        try:
            await config_patcher.delete_openclaw_config_path(owner_id, ["env", "vars", key])
        except ConfigPatchError:
            # No container config yet, or the path is already gone — the
            # Secrets Manager copy is already durable, which is what matters.
            pass

    await write_owner_facts(owner_id)
    return {"revoked": True}
