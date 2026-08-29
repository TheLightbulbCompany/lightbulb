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

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from core.repositories import billing_repo
from core.services import provision_gate, provisioning_approvals, provisioning_landing
from core.services.owner_facts import write_owner_facts
from core.services.paperclip_admin_client import PaperclipApiError
from routers.agent_builder import _require_hook_token

logger = logging.getLogger(__name__)
router = APIRouter()

# `provider` becomes part of a Secrets Manager secret NAME and the integrations
# sort key; `resource_id` goes into a URL path. The body comes from an
# LLM-driven agent, so this is a trust boundary, not a formality.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_MAX_ENV_VARS = 25
_MAX_ENV_VALUE = 8192
_MAX_URL = 2048

# The pairing URL is shown to the owner as a click target, so it may only ever
# be Stripe's own confirm host.
_STRIPE_LOGIN_PREFIX = "https://dashboard.stripe.com/"


def _https_only(v: str) -> str:
    """Every URL on this rail is rendered to the owner as a click target."""
    if v and not v.startswith("https://"):
        raise ValueError("must be an https:// URL")
    return v


async def _require_paid(owner_id: str) -> None:
    """402 unless the owner is on a paid plan — before any side effect."""
    if not provision_gate.has_always_on_entitlement(await billing_repo.get_by_owner_id(owner_id)):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "routines_require_subscription",
                "message": "Provisioning needs an active subscription",
            },
        )


def _board_unavailable(owner_id: str, e: Exception) -> HTTPException:
    logger.warning("provisioning: could not reach the board owner_id=%s: %s", owner_id, e)
    return HTTPException(
        status_code=503,
        detail={"code": "paperclip_unavailable", "message": "Could not reach the company board; try again later"},
    )


async def _file_card(owner_id: str, coro):
    """Run one card-filing coroutine with the P0a error split: 503 = the board
    is down (retry), 502 = the board refused (retrying changes nothing)."""
    try:
        return await coro
    except httpx.HTTPError as e:
        raise _board_unavailable(owner_id, e) from e
    except PaperclipApiError as e:
        if e.retryable:
            raise _board_unavailable(owner_id, e) from e
        logger.warning("provisioning: board rejected the request owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail={"code": "paperclip_rejected", "message": e.message}) from e


class PairingBody(BaseModel):
    browser_url: str = Field(max_length=_MAX_URL)
    verification_code: str = Field(min_length=1, max_length=64)
    issue_id: str | None = Field(default=None, max_length=128)

    @field_validator("browser_url")
    @classmethod
    def _stripe_only(cls, v: str) -> str:
        if not v.startswith(_STRIPE_LOGIN_PREFIX):
            raise ValueError("browser_url must be a dashboard.stripe.com URL")
        return v


@router.post("/u/{owner_id}/pairing")
async def request_pairing(owner_id: str, body: PairingBody, request: Request):
    """Friday already ran `stripe login --non-interactive`; file the card."""
    await _require_hook_token(owner_id, request)
    await _require_paid(owner_id)
    approval_id = await _file_card(
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

    @field_validator("provider", "service")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("must be a lowercase slug")
        return v

    _terms = field_validator("terms_url")(_https_only)


@router.post("/u/{owner_id}/consent")
async def request_consent(owner_id: str, body: ConsentBody, request: Request):
    """The owner's one decision per provider: the terms and the price."""
    await _require_hook_token(owner_id, request)
    await _require_paid(owner_id)
    approval_id = await _file_card(
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

    @field_validator("provider", "service")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("must be a lowercase slug")
        return v

    @field_validator("resource_id")
    @classmethod
    def _resource_id(cls, v: str) -> str:
        if not _RESOURCE_ID_RE.match(v):
            raise ValueError("resource_id has an unexpected shape")
        return v

    @field_validator("dashboard_url")
    @classmethod
    def _dashboard(cls, v: str | None) -> str | None:
        return None if v is None else _https_only(v)

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_ENV_VARS:
            raise ValueError(f"at most {_MAX_ENV_VARS} environment variables")
        for key, value in v.items():
            if not _ENV_KEY_RE.match(key):
                raise ValueError("environment variable names must be UPPER_SNAKE")
            if not value:
                # A blank would overwrite a live credential with nothing.
                raise ValueError("environment variable value must not be empty")
            if len(value) > _MAX_ENV_VALUE:
                raise ValueError("environment variable value is too large")
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
    await _require_paid(owner_id)
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
