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
from typing import Annotated, Literal

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.dynamodb import utc_now_iso
from core.repositories import apps_repo, billing_repo, integrations_repo
from core.repositories import provisioning_workflow_repo as workflow_repo
from core.services import provisioning_approvals, provisioning_landing, provisioning_questions, provisioning_workflow
from core.services import stripe_provisioning as api
from core.services.owner_facts import write_owner_facts
from core.services.provisioning_workflow import WorkflowRefused, is_offerable, is_transient, provider_of
from core.services.stripe_provisioning import ProvisioningError
from core.services.container_auth import _require_hook_token
from routers.company_board import file_card
from routers.company_money import day_range
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
# Owner-facing (Clerk). Paid-only and org-admin-only, exactly like P0a's
# company-Stripe router. There is deliberately NO catalog or provision route
# here: provisioning is Friday's job (spec §3.1's trigger split), and the
# owner's surface is the approval cards, the resource list, and the answers to
# them at the bottom of this file.
# ---------------------------------------------------------------------------


def _gate_admin(auth: AuthContext) -> None:
    if auth.is_org_context:
        require_org_admin(auth)


async def _owner_gate(auth: Annotated[AuthContext, Depends(get_current_user)]) -> str:
    """The three lines every owner route below opens with: money is
    org-admin-only (spec §3.6), the rail is paid-only, and the owner is the
    org, never the member who clicked."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    return owner_id


Owner = Annotated[str, Depends(_owner_gate)]


def _safe_resource(pointer: dict) -> dict:
    """One provisioned-resource pointer, allowlisted for the browser.

    Never the stored dict: it also carries the Stripe ``project_id``, and a
    blocklist would leak the next field somebody adds to it. Same rule, same
    reason, as ``_safe_workflow``.
    """
    out = {
        "resource_id": pointer.get("resource_id"),
        "provider": pointer.get("provider"),
        "service": pointer.get("service"),
        "resource_name": pointer.get("resource_name"),
        "status": pointer.get("status"),
        # NAMES only — the values live in Secrets Manager and the container env.
        "env_keys": [str(k)[:128] for k in (pointer.get("env_keys") or [])[:50]],
        "level": pointer.get("level"),
        "workflow_id": pointer.get("workflow_id"),
    }
    return out


@router.get("/resources")
async def resources(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    rows = await provisioning_landing.provisioning_summary(owner_id)
    return {"resources": [_safe_resource(r) for r in rows]}


@router.get("/charges")
async def charges(owner_id: Owner, start: str | None = None, end: str | None = None):
    """What agents bought on the owner's behalf — Spend's second source.

    Spend now adds three sources up: agent inference (``/billing/spend``),
    these provider charges, and the company's own Stripe money
    (``/company/rollup``'s revenue column, which moves the other way — hence
    the explicit ``direction``). Every amount here is money OUT.

    Always ``provisioning_available: false`` today: the 2026-09-30.preview
    Provisioning API has no charges/usage endpoint, so Stripe is never called
    and the ``reason`` says why.
    """
    start_day, end_day = day_range(start, end)
    unavailable = {
        "start": start_day,
        "end": end_day,
        "provisioning_available": False,
        "charges": [],
        "truncated": False,
        "direction": "out",
    }
    try:
        await provisioning_workflow.account_id_for(owner_id)
    except WorkflowRefused as e:
        return {**unavailable, "reason": e.code}
    # The 2026-09-30.preview API has no usage or charges endpoint, so there is
    # nothing to ask Stripe. Kept as an honest "not available" so Spend's second
    # source degrades instead of 404ing.
    return {**unavailable, "reason": "usage_unavailable"}


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
# `catalog`, `requests`, `requests/{id}` and `lightbulb-facts.json`. She never sees
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
# One open workflow per (provider, service): asking twice is a no-op, not a
# conflict. Friday retrying after a timeout must not be told the door is shut.
REUSED_MESSAGE = "You already asked for this one — the same request is still open. Nothing new was filed."
# Spec 05 P1: asked from a task run, the consent is a Question on the task. The Worker's turn ends on it; the answer
# comes back as the next turn's input, and reading the request again then moves it on.
QUESTION_MESSAGE = (
    "Asked for approval on this task's card. End your turn now: the task waits for the answer and you are resumed "
    "with it. Then read this request again (GET …/requests/{workflow_id}) to see whether it was approved."
)


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


def _safe_workflow(row: dict, app_name: str | None = None) -> dict:
    """The ONE serializer for a workflow row, on every door — container or
    browser. Allowlisted, never a filtered copy of the row: the row carries the
    provider connection request id, the resource id, the project id and the approval
    snapshot, and a blocklist would leak the next field somebody adds."""
    return {
        "workflow_id": row.get("workflow_id"),
        "state": row.get("state"),
        "provider": row.get("provider_name"),
        "service": row.get("service_ref"),
        # R53: an app's own row says which app and environment it serves.
        "app_id": row.get("app_id"),
        "environment": row.get("environment"),
        "app_name": app_name,
        "awaiting": row.get("awaiting"),
        "information_target": row.get("information_target"),
        "error_code": row.get("error_code"),
        "message": (row.get("error_message") or "")[:400] or None,
        # Re-reduced rather than trusted: the row already holds the safe form,
        # and running it through the same serializer means a field added to the
        # stored shape cannot escape here by default.
        "provider_account": provisioning_workflow.safe_account_details(row.get("provider_account")),
        "resource_name": row.get("resource_name"),
        # `removed` once the owner deprovisioned it: the row stays `active`
        # (the request WAS fulfilled), so without this the history says the
        # credential is still live.
        "resource_status": row.get("resource_status"),
        # NAMES only. The values live in Secrets Manager and are injected into
        # the container's env — they are never served over an HTTP door.
        "env_keys": [str(k)[:128] for k in (row.get("env_keys") or [])[:50]],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def _safe_workflows(rows: list[dict]) -> list[dict]:
    """A list's rows, app rows named after their app (one read per app)."""
    names: dict[str, str | None] = {}
    for app_id in {r["app_id"] for r in rows if r.get("app_id")}:
        names[app_id] = (await apps_repo.get(app_id) or {}).get("name")
    return [_safe_workflow(r, names.get(r.get("app_id") or "")) for r in rows]


# Stripe's Provisioning API has no webhooks, so a READ is what moves a
# workflow. A list read steps the open rows it returns — bounded, because each
# step can be a Stripe call and a page load must not turn into a dozen.
#
# ponytail: 5 rows per read; raise it (or step them concurrently) if owners
# routinely have more open at once than one page load can carry.
_MAX_STEPS_PER_READ = 5


async def _stepped(rows: list[dict]) -> list[dict]:
    """Move the OLDEST open rows one step each, up to the cap, and hand the
    list back in the order it came in.

    Oldest first because `rows` arrives NEWEST first: with more open rows than
    the cap, taking them in presentation order would step the same recent few
    on every read and starve the old ones forever — and an old open row is
    precisely the one nothing else is going to move.
    """
    budget = _MAX_STEPS_PER_READ
    moved: dict[str, dict] = {}
    for row in reversed(rows):
        if not budget:
            break
        if row.get("state") in workflow_repo.TERMINAL_STATES:
            continue
        budget -= 1
        moved[row["workflow_id"]] = await provisioning_workflow.advance_on_read(row["workflow_id"]) or row
    return [moved.get(row["workflow_id"], row) for row in rows]


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
    # WHY she is asking. The catalog says what the service is; only Friday can
    # say what it is for, and the owner is approving spend on her word.
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
        _safe_service(s, providers[provider_of(s)])
        for s in services
        # A service whose provider is not in the catalog is one `start` refuses
        # outright, so it is not an offer we can keep.
        if provider_of(s) in providers and is_offerable(s, providers[provider_of(s)])
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
    approve her own request, and the backend, not this call, does the work."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    _validate(body)
    task = await provisioning_questions.task_for_run(owner_id, request.headers.get("X-Lightbulb-Run"))
    try:
        # `start` files the consent card, so a board outage arrives here as a
        # PaperclipApiError/httpx error. Unmapped it is a 500 the agent reads as
        # a platform fault; `file_card` splits it into 503 (down, retry) and 502
        # (refused) and passes every other exception through untouched.
        row = await file_card(
            owner_id,
            provisioning_workflow.start(
                owner_id=owner_id,
                provider_name=body.provider,
                service_id=body.service,
                configuration=body.configuration,
                issue_id=body.issue_id,
                reason=body.reason,
                task_id=(task or {}).get("taskId"),
            ),
        )
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    reused = bool(row.get("reused"))
    asked = row.get("question") if row.get("state") == "awaiting_approval" else None
    return {
        **_safe_workflow(row),
        "reused": reused,
        **({"questionId": asked["qid"]} if asked else {}),
        "message": QUESTION_MESSAGE if asked else REUSED_MESSAGE if reused else FILED_MESSAGE,
    }


@router.get("/u/{owner_id}/requests")
async def container_requests(owner_id: str, request: Request):
    """Friday enumerating her own asks — the authoritative read when she needs
    a workflow_id she did not keep."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    rows = await workflow_repo.list_for_owner(owner_id)
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"requests": await _safe_workflows(await _stepped(rows))}


@router.get("/u/{owner_id}/requests/{workflow_id}")
async def container_request_status(owner_id: str, workflow_id: str, request: Request):
    """Friday checking back. The READ is what moves the workflow — see
    ``_stepped``."""
    await _require_hook_token(owner_id, request)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    row = await _owned(owner_id, workflow_id)
    if row.get("state") in workflow_repo.TERMINAL_STATES:
        return _safe_workflow(row)
    return _safe_workflow(await provisioning_workflow.advance_on_read(workflow_id) or row)


@router.delete("/u/{owner_id}/requests/{workflow_id}")
async def container_cancel_request(owner_id: str, workflow_id: str, request: Request):
    """Friday withdrawing her own ask — the task changed, the owner said no in
    chat, she asked for the wrong service.

    "Withdraw the ask", never "undo the infrastructure": `cancel` refuses the
    moment a provider connection request or a resource exists, because there is no
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


# ---------------------------------------------------------------------------
# Owner-facing answers (Clerk). Every button on an Inbox card and in
# Settings -> Connections lands here.
#
# Nothing minted below is ever written to the row or logged: checkout URLs,
# provider redirect URLs and deep links are short-lived and effectively
# bearer-like ("Present it immediately and do not persist, cache, or log it"),
# so each one is minted for THIS click and returned once.
#
# Order on every route: the gate (`Owner`), then ownership, then state, then
# the body, then the side effect. None of them steps the workflow: the click's
# own transition IS its step, and the next READ — the Settings page reloading,
# Friday checking back — takes the one after it. Stepping here too would spend
# a second Stripe call inside a click that already made one.
# ---------------------------------------------------------------------------

# $1,000 per interval. Not Stripe's ceiling — ours, so a fat-fingered limit
# cannot authorise four figures of provider spend on the owner's own card.
_MAX_USAGE_AMOUNT = 100_000

REMOVED_MESSAGE = (
    "{name} was deprovisioned at {provider} and its credentials were removed from your agents. "
    "The provider stops billing your Stripe account for it; anything already used this period is still invoiced."
)


class UsageLimit(BaseModel):
    """The spending cap the owner signs for. `usd`/week|month|year is the only
    shape the payment profile is exercised with in P0b — widen it the day a
    non-USD owner exists rather than forwarding an untested currency."""

    max_amount: int = Field(gt=0, le=_MAX_USAGE_AMOUNT)
    currency: Literal["usd"] = "usd"
    recurring_interval: Literal["week", "month", "year"] = "month"


class ApproveBody(BaseModel):
    usage_limit: UsageLimit | None = None


class InformationBody(BaseModel):
    information: dict = Field(default_factory=dict)


class RemoveBody(BaseModel):
    confirm: str = Field(max_length=200)


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


async def _owned_resource(owner_id: str, provider: str, resource_id: str) -> dict:
    """The pointer for one provisioned resource of this owner's, or 404 — the
    same "missing and not yours are indistinguishable" rule as `_owned`."""
    if not _NAME_RE.fullmatch(provider) or not _RESOURCE_ID_RE.fullmatch(resource_id):
        raise HTTPException(status_code=422, detail="bad provider or resource id")
    pointers = await integrations_repo.list_provider_resources(owner_id, provider)
    pointer = next((p for p in pointers if p.get("resource_id") == resource_id), None)
    if pointer is None:
        raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
    return pointer


async def _company_project_id(owner_id: str) -> str | None:
    """The company's ONE Provisioning Project. It lives on the BILLING row, not
    on the workflow — Connect deauth clears it there, in one place."""
    return (await billing_repo.get_by_owner_id(owner_id) or {}).get("company_stripe_project_id")


async def _stamp_removed(workflow_id: str | None) -> None:
    """Mark the workflow whose resource just went away. BEST-EFFORT by
    construction: the provider resource is already gone and our copy with it,
    so a purged or missing row must never turn a completed removal into a 500
    the owner would retry."""
    if not workflow_id:
        return
    try:
        await workflow_repo.patch(workflow_id, {"resource_status": "removed", "removed_at": utc_now_iso()})
    except Exception as e:  # noqa: BLE001 — the removal already happened
        logger.warning("company_provisioning: could not stamp workflow %s as removed: %s", workflow_id, e)


async def _resumed(row: dict, awaiting: str) -> None:
    """The owner came back to a `stale` row.

    Un-stale it, or the next read polls a `stale` row for another seven days.
    `user_action_since` restarts with it — the days measure how long they have
    been away, and they are demonstrably back.
    """
    if row.get("state") == "stale":
        await workflow_repo.transition(
            row["workflow_id"],
            from_state="stale",
            to_state="awaiting_user_action",
            fields={"awaiting": awaiting, "user_action_since": utc_now_iso()},
        )


@router.get("/workflows")
async def list_workflows(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """What the owner has been asked for, newest first — the Settings list and
    the source of every card's live state.

    A READ, gated like `GET /resources`: any member of the org may see what
    their company was asked for. Only the answers below are admin-and-paid.
    """
    owner_id = resolve_owner_id(auth)
    rows = await workflow_repo.list_for_owner(owner_id)
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"workflows": await _safe_workflows(await _stepped(rows))}


@router.post("/workflows/{workflow_id}/approve")
async def approve_workflow(
    workflow_id: str,
    body: ApproveBody,
    owner_id: Owner,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """The owner approved the consent card. Records who signed and resumes.

    A limit from the body is honoured only while the card said it was editable
    — i.e. while no Payment profile exists. Afterwards it is a live billing
    setting on the account, and a card must not silently move it.
    """
    row = await _owned(owner_id, workflow_id)
    if row.get("state") != "awaiting_approval":
        raise _conflict("not_awaiting_approval", "This request is not waiting for your approval")
    usage_limit = row.get("usage_limit")
    try:
        if body.usage_limit is not None and await provisioning_workflow.limit_is_editable(
            await provisioning_workflow.account_id_for(owner_id)
        ):
            usage_limit = body.usage_limit.model_dump()
        moved = await provisioning_workflow.on_approved(
            workflow_id,
            usage_limit=usage_limit,
            # Who signed for the spend. The audit line for a limit the company
            # will be billed against.
            approved_by=auth.user_id,
        )
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    return _safe_workflow(moved or row)


@router.post("/workflows/{workflow_id}/reject")
async def reject_workflow(workflow_id: str, owner_id: Owner):
    """The owner said no. Terminal, and a clean stop: nothing exists on their
    Stripe account yet, so there is nothing to unwind."""
    row = await _owned(owner_id, workflow_id)
    if row.get("state") != "awaiting_approval":
        raise _conflict("not_awaiting_approval", "This request is not waiting for your approval")
    moved = await provisioning_workflow.on_rejected(workflow_id)
    await write_owner_facts(owner_id)
    # No step: `rejected` is terminal, and nothing reads a terminal row again.
    return _safe_workflow(moved or {**row, "state": "rejected"})


@router.post("/workflows/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str, owner_id: Owner):
    """Withdraw an ask nothing was created for. Same fence as the container's
    DELETE: once a state-changing POST has been made there is no undo, only
    `remove`."""
    row = await _owned(owner_id, workflow_id)
    try:
        moved = await provisioning_workflow.cancel(workflow_id)
    except WorkflowRefused as e:
        raise _refusal(e) from e
    await write_owner_facts(owner_id)
    # No step: `cancelled` is terminal.
    return _safe_workflow(moved or {**row, "state": "cancelled"})


@router.post("/workflows/{workflow_id}/information")
async def submit_information(workflow_id: str, body: InformationBody, owner_id: Owner):
    """Provider-requested fields, validated against the schema WE stored — not
    one the browser supplied.

    The two submit endpoints take DIFFERENT bodies (`information` vs
    `submitted_information`) against different objects, so the ROW says which
    one this schema came from. Never inferred from whichever id happens to be
    set: during a reauth both are.
    """
    row = await _owned(owner_id, workflow_id)
    target = row.get("information_target")
    target_id = provisioning_workflow.request_id_of(row) if target == "account_request" else row.get("resource_id")
    if row.get("awaiting") != "provider_information" or target not in ("account_request", "resource") or not target_id:
        raise _conflict("no_information_pending", "This request is not waiting for information")
    if len(body.information) > _MAX_CONFIG_KEYS:
        raise _invalid(f"information may hold at most {_MAX_CONFIG_KEYS} fields")
    # Same bounds a `configuration` gets: the provider renders these, and only
    # a renderable schema (`_renderable`) ever reaches an owner, so a nested
    # value is not an answer to any form we filed.
    for key, value in body.information.items():
        # The key is a field name off a schema WE stored, but the body is not:
        # bound it exactly like `_validate` bounds a configuration key.
        if len(key) > _MAX_CONFIG_KEY or _UNSAFE_RE.search(key):
            raise _invalid("information field names must be short and free of control characters")
        if isinstance(value, str):
            if len(value) > _MAX_CONFIG_VALUE or _UNSAFE_RE.search(value):
                raise _invalid(f"the value for {key!r} is too long or has control characters")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise _invalid(f"the value for {key!r} must be a single value, not a list or an object")
    try:
        jsonschema.validate(body.information, row.get("information_schema") or {})
    except jsonschema.ValidationError as e:
        # `schema_reason`, never `e.message` — that quotes the offending value
        # back, and a provider's form can ask for a credential.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_information",
                "message": f"{provisioning_workflow.schema_reason(e)} for this form",
            },
        ) from e
    try:
        account_id = await provisioning_workflow.account_id_for(owner_id)
        if target == "account_request":
            await api.submit_provider_connection_request_information(
                account_id, target_id, information=body.information
            )
            to_state = "connecting_provider"
        else:
            await api.submit_resource_information(account_id, target_id, submitted_information=body.information)
            to_state = "provisioning"
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    # The schema goes with the question it belonged to: a provider that asks
    # again publishes a new one, and a leftover would validate the next answer
    # against the wrong form.
    moved = await workflow_repo.transition(
        workflow_id,
        from_state=row["state"],
        to_state=to_state,
        fields={
            "awaiting": None,
            "information_target": None,
            "information_schema": None,
            "user_action_since": None,
        },
    )
    return _safe_workflow(moved or {**row, "state": to_state})


@router.post("/workflows/{workflow_id}/payment-link")
async def payment_link(workflow_id: str, owner_id: Owner):
    """Mint the hosted payment URL for THIS click and return it once.

    The card carries no URL: by the time it is clicked the checkout session
    behind an old one would be long expired.
    """
    row = await _owned(owner_id, workflow_id)
    if row.get("state") in workflow_repo.TERMINAL_STATES or row.get("awaiting") != "payment_setup":
        raise _conflict("no_payment_pending", "This request is not waiting for a payment method")
    # The limit the owner already approved. Merged over the default so a row
    # written before the limit existed still names all three fields.
    limit = {**provisioning_approvals.DEFAULT_USAGE_LIMIT, **(row.get("usage_limit") or {})}
    try:
        result = await api.create_payment_method_request(
            await provisioning_workflow.account_id_for(owner_id),
            max_amount=int(limit["max_amount"]),
            currency=str(limit["currency"]),
            recurring_interval=str(limit["recurring_interval"]),
        )
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    await _resumed(row, "payment_setup")
    # `complete` is the answer when a card is already on file — no URL, and the
    # payment-profile poll on the next read is what moves the row on from here.
    return {"checkout_session_url": result.get("checkout_session_url"), "status": result.get("status")}


@router.post("/workflows/{workflow_id}/connect-link")
async def connect_link(workflow_id: str, owner_id: Owner):
    """Mint the provider OAuth URL for THIS click.

    Never served from the card: the provider connection request may have
    completed, errored or vanished since it was filed. Re-read it, and create a
    fresh one when the old one is unusable ("Create a new Provider connection
    request and ask the user to reconnect").
    """
    row = await _owned(owner_id, workflow_id)
    if row.get("state") in workflow_repo.TERMINAL_STATES or row.get("awaiting") != "provider_connect":
        raise _conflict("no_connect_pending", "This request is not waiting for a provider connection")
    try:
        account_id = await provisioning_workflow.account_id_for(owner_id)
        request_ = None
        if provisioning_workflow.request_id_of(row):
            try:
                request_ = await api.get_provider_connection_request(
                    account_id, provisioning_workflow.request_id_of(row)
                )
            except ProvisioningError as e:
                # Gone, or the connection behind it expired: both are "make a
                # new one", not "fail the click".
                if e.code not in ("not_found", "provider_reauth_required"):
                    raise
            if provisioning_workflow.connection_request_status(request_ or {}) == "error":
                request_ = None
        if request_ is None:
            scope = (row.get("approval") or {}).get("scope")
            request_ = await api.create_provider_connection_request(
                account_id,
                provider=row["provider_id"],
                project=await _company_project_id(owner_id) if scope == "project" else None,
            )
            # Persisted BEFORE the URL is handed over: a request carries no
            # name, so an id lost here is unreconcilable.
            await workflow_repo.patch(workflow_id, {"provider_connection_request_id": request_["id"]})
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    await _resumed(row, "provider_connect")
    if provisioning_workflow.connection_request_status(request_) != "pending_auth" or not request_.get("redirect_url"):
        # `complete`, `requested` or `needs_information`: there is no browser
        # step to take right now, and the next read is what moves the row.
        raise _conflict("no_redirect_available", "This provider connection does not need a browser step right now")
    return {"redirect_url": request_["redirect_url"]}


@router.post("/resources/{provider}/{resource_id}/dashboard-link")
async def dashboard_link(provider: str, resource_id: str, owner_id: Owner):
    """No deep-link endpoint exists on 2026-09-30.preview, so this answers a
    null url (the card says to open the provider's own site)."""
    await _owned_resource(owner_id, provider, resource_id)
    # The 2026-09-30.preview API has no deep-link endpoint. The card already
    # treats a null url as "open it from the provider's own site".
    return {"url": None, "expires_time": None}


@router.post("/resources/{provider}/{resource_id}/remove")
async def remove_provider_resource(provider: str, resource_id: str, body: RemoveBody, owner_id: Owner):
    """DEPROVISION the owner's provider infrastructure, then drop our copy.

    The ONLY path in the codebase that reaches a provider (spec §3.6) — wipe,
    entitlement teardown and revoke never do. The owner types the resource's
    own name back, which is the "separate confirmation" the guide requires
    alongside stating the billing and infrastructure effect.

    Partial failure has a cleanup door: if the deprovision lands but dropping
    our copy does not, the resource is gone at the provider and our pointer
    survives — `POST /resources/{provider}/{id}/revoke` removes exactly that.
    """
    pointer = await _owned_resource(owner_id, provider, resource_id)
    expected = str(pointer.get("resource_name") or resource_id)
    if body.confirm.strip() != expected:
        raise _conflict("confirm_mismatch", f'Type "{expected}" to confirm that it is deprovisioned')
    try:
        await api.remove_resource(await provisioning_workflow.account_id_for(owner_id), resource_id)
    except WorkflowRefused as e:
        raise _refusal(e) from e
    except ProvisioningError as e:
        raise _stripe_failure(e) from e
    # Our copy goes only after the provider accepted the deprovision, so a
    # failure never leaves a live resource we can no longer name.
    await provisioning_landing.revoke_credentials(owner_id=owner_id, provider=provider, resource_id=resource_id)
    await _stamp_removed(pointer.get("workflow_id"))
    await write_owner_facts(owner_id)
    return {"removed": True, "message": REMOVED_MESSAGE.format(name=expected, provider=provider)}
