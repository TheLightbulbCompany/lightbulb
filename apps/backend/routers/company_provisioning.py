"""The provisioning rail — /api/v1/company/provisioning/*.

The backend owns the Provisioning API loop (spec §3.2). Friday asks through the
container-authed ``/u/{owner_id}/…`` routes; the owner acts through the
Clerk-authed routes and the Inbox cards. No credential value, checkout URL,
redirect URL or deep link ever crosses the container door.

Order on every route: authenticate (hook token or Clerk), then the paid gate,
then validation of an LLM-written body, then side effects. The container
routes' bodies are deliberately loose Pydantic models checked in ``_validate``
rather than in field validators, so a caller who fails a gate is never told
which field was wrong.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import provisioning_workflow_repo as workflow_repo
from core.services import provisioning_driver, provisioning_landing, provisioning_workflow
from core.services import stripe_provisioning as api
from core.services.owner_facts import write_owner_facts
from core.services.provisioning_workflow import WorkflowRefused, is_offerable, is_transient
from core.services.stripe_provisioning import ProvisioningError
from routers.agent_builder import _require_hook_token
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


# ---------------------------------------------------------------------------
# Container-authed (hook token). Spec §3.2: Friday reaches the rail through
# `catalog`, `requests`, `requests/{id}` and `isol8-facts.json`. She never sees
# a credential value, a checkout URL, a redirect URL, a deep link or a Stripe
# account id — `_safe_service` and `_safe_workflow` are the two serializers
# that guarantee it.
#
# Order on every route: hook token -> paid gate -> validation -> work.
# ---------------------------------------------------------------------------

_MAX_CATALOG_ROWS = 200
# A `configuration` is a JSON-schema payload the provider will render, not an
# env block: flat, scalar and small. The bound is deliberately tighter than
# `_MAX_ENV_VALUE` — nothing legitimate here is a kilobyte of prose.
_MAX_CONFIG_KEYS = 25
_MAX_CONFIG_KEY = 128
_MAX_CONFIG_VALUE = 1024
_MAX_ISSUE_ID = 128
_MAX_REASON = 500

FILED_MESSAGE = "Filed an approval card for your owner. Keep working on everything that does not need it."


def _refusal(e: WorkflowRefused) -> HTTPException:
    """Every refusal is a 409: the door is shut for a reason the agent can read
    off `code` and relay. `requirements` rides along for `requirements_pending`,
    which is the one refusal the OWNER can clear themselves."""
    detail = {"code": e.code, "message": e.message}
    if e.requirements:
        detail["requirements"] = e.requirements
    return HTTPException(status_code=409, detail=detail)


def _stripe_failure(e: ProvisioningError) -> HTTPException:
    """503 = Stripe is unreachable or undecided, so re-reading is the right
    move. 502 = Stripe (or the provider) REFUSED, and repeating it changes
    nothing. The CLASS decides, never the code string — see `is_transient`."""
    if is_transient(e):
        return HTTPException(
            status_code=503,
            detail={
                "code": "stripe_unavailable",
                "stripe_code": e.code,
                "message": "Could not reach Stripe; try again shortly",
            },
        )
    logger.warning("company_provisioning: Stripe refused (%s): %s", e.code, e.message)
    return HTTPException(status_code=502, detail={"code": "stripe_error", "stripe_code": e.code, "message": e.message})


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_request", "message": message})


def _safe_service(service: dict, provider: dict) -> dict:
    """One catalog row, reduced to what an agent may reason about."""
    pricing = service.get("pricing") or {}
    entries = pricing.get("paid_pricing") or []
    default = next((e for e in entries if e.get("is_default")), entries[0] if entries else None)
    return {
        "provider": service.get("provider_name") or provider.get("name"),
        "service": service.get("service_id"),
        "description": (service.get("description") or "")[:1000],
        # Untrusted catalog data, passed through as DATA for explanation only.
        # It is sometimes a URL (`https://here.now/skill.md`) rather than prose;
        # either way it is a string we never fetch server-side.
        "llm_context": (service.get("llm_context") or "")[:2000],
        "categories": [str(c)[:64] for c in (service.get("categories") or [])[:20]],
        "pricing_type": pricing.get("type"),
        # Display text, kept verbatim: "Don't derive a structured cost from
        # freeform price text."
        "price_display": ((default or {}).get("freeform") or "")[:500],
        "configuration_schema": service.get("configuration_schema") or {},
        "scope": service.get("scope"),
        "kind": service.get("kind"),
        "terms_url": provider.get("tos_url"),
        "website_url": provider.get("website_url"),
    }


def _safe_workflow(row: dict) -> dict:
    """The ONE serializer for a workflow row, on every door — container or
    browser. Allowlisted, never a filtered copy of the row: the row carries the
    account request id, the resource id, the project id and the approval
    snapshot, and a blocklist would leak the next field somebody adds."""
    return {
        "workflow_id": row.get("workflow_id"),
        "state": row.get("state"),
        "provider": row.get("provider_name"),
        "service": row.get("service_ref"),
        "awaiting": row.get("awaiting"),
        "information_target": row.get("information_target"),
        "error_code": row.get("error_code"),
        "message": (row.get("error_message") or "")[:400] or None,
        # Re-reduced rather than trusted: the row already holds the safe form,
        # and running it through the same serializer means a field added to the
        # stored shape cannot escape here by default.
        "provider_account": provisioning_workflow.safe_account_details(row.get("provider_account")),
        "resource_name": row.get("resource_name"),
        # NAMES only. The values live in Secrets Manager and are injected into
        # the container's env — they are never served over an HTTP door.
        "env_keys": [str(k)[:128] for k in (row.get("env_keys") or [])[:50]],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def _owned(owner_id: str, workflow_id: str) -> dict:
    """404 rather than 403 for another owner's id: a different status confirms
    the id exists."""
    row = await workflow_repo.get(workflow_id)
    if not row or row.get("owner_id") != owner_id:
        raise HTTPException(status_code=404, detail={"code": "workflow_not_found"})
    return row


class ProvisionRequestBody(BaseModel):
    """Loose on purpose. The real checks run in `_validate`, AFTER the two
    gates, so an unauthenticated caller never learns which field was wrong."""

    provider: str = ""
    service: str = ""
    configuration: dict = Field(default_factory=dict)
    issue_id: str | None = None
    # Accepted because Friday's prompt carries one; the consent card is built
    # from the catalog snapshot, so it is validated and then dropped rather
    # than rendered as agent prose the owner might read as ours.
    reason: str = ""


def _validate(body: ProvisionRequestBody) -> None:
    """The body is written by an LLM. `provider` becomes a Secrets Manager
    secret name component and an integrations sort key; `configuration` is
    forwarded to Stripe and rendered on the owner's approval card."""
    for field in ("provider", "service"):
        try:
            _slug(getattr(body, field))
        except ValueError as e:
            raise _invalid(f"{field} {e}") from e
    if len(body.configuration) > _MAX_CONFIG_KEYS:
        raise _invalid(f"configuration may hold at most {_MAX_CONFIG_KEYS} keys")
    for key, value in body.configuration.items():
        if len(key) > _MAX_CONFIG_KEY or _UNSAFE_RE.search(key):
            raise _invalid("configuration keys must be short and free of control characters")
        if isinstance(value, str):
            if len(value) > _MAX_CONFIG_VALUE or _UNSAFE_RE.search(value):
                raise _invalid(f"configuration value for {key!r} is too long or has control characters")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise _invalid(f"configuration value for {key!r} must be a scalar")
    if body.issue_id and len(body.issue_id) > _MAX_ISSUE_ID:
        raise _invalid("issue_id is too long")
    if len(body.reason) > _MAX_REASON:
        raise _invalid("reason is too long")
    # Both end up on the owner's approval card, where a newline splits whatever
    # renders it.
    for field in ("issue_id", "reason"):
        try:
            _plain(getattr(body, field) or "")
        except ValueError as e:
            raise _invalid(f"{field} {e}") from e


@router.get("/u/{owner_id}/catalog")
async def container_catalog(owner_id: str, request: Request, q: str = Query(default="", max_length=120)):
    """Policy first, the agent's query second (guide: "apply country, scope,
    Provider, pricing, and security rules → pass only eligible options to a
    model for explanation or ranking"). The filter is `is_offerable`, shared
    with `start`, so the agent can never be offered something the workflow
    would then refuse."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    try:
        account_id = await provisioning_workflow.account_id_for(owner_id)
        elig = await api.eligibility(account_id)
        providers = {p.get("id"): p for p in await api.catalog_providers(account_id)}
        services = await api.catalog_services(account_id)
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e

    rows = [
        _safe_service(s, providers[s["provider_id"]])
        for s in services
        # A service whose provider is not in the catalog is one `start` refuses
        # outright, so it is not an offer we can keep.
        if s.get("provider_id") in providers and is_offerable(s, providers[s["provider_id"]])
    ]
    needle = q.strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle
            in f"{r['provider'] or ''} {r['service'] or ''} {r['description']} {' '.join(r['categories'])}".lower()
        ]
    return {
        "services": rows[:_MAX_CATALOG_ROWS],
        "eligible": bool(elig.get("is_eligible")),
        "requirements": list(elig.get("requirements") or []),
    }


@router.post("/u/{owner_id}/requests")
async def container_request(owner_id: str, body: ProvisionRequestBody, request: Request):
    """Friday asks. The backend owns everything after this line — she cannot
    approve her own request, and the driver, not this call, does the work."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    _validate(body)
    try:
        row = await provisioning_workflow.start(
            owner_id=owner_id,
            provider_name=body.provider,
            service_id=body.service,
            configuration=body.configuration,
            issue_id=body.issue_id,
        )
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    await provisioning_driver.kick(row["workflow_id"])
    return {**_safe_workflow(row), "message": FILED_MESSAGE}


@router.get("/u/{owner_id}/requests")
async def container_requests(owner_id: str, request: Request):
    """Friday enumerating her own asks — the authoritative read when she needs
    a workflow_id she did not keep."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    rows = await workflow_repo.list_for_owner(owner_id)
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"requests": [_safe_workflow(r) for r in rows]}


@router.get("/u/{owner_id}/requests/{workflow_id}")
async def container_request_status(owner_id: str, workflow_id: str, request: Request):
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    return _safe_workflow(await _owned(owner_id, workflow_id))


@router.delete("/u/{owner_id}/requests/{workflow_id}")
async def container_cancel_request(owner_id: str, workflow_id: str, request: Request):
    """Friday withdrawing her own ask — the task changed, the owner said no in
    chat, she asked for the wrong service.

    "Withdraw the ask", never "undo the infrastructure": `cancel` refuses the
    moment an account request or a resource exists, because there is no
    idempotency contract to lean on and closing our row would orphan an object
    on the owner's own Stripe account.
    """
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    row = await _owned(owner_id, workflow_id)
    try:
        moved = await provisioning_workflow.cancel(workflow_id)
    except WorkflowRefused as e:
        raise _refusal(e) from e
    return _safe_workflow(moved or {**row, "state": "cancelled"})
