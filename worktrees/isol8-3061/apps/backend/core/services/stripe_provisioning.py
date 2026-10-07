"""Stripe Provisioning API client — platform key + Stripe-Context.

Allowlisted private preview; every request carries
``Stripe-Version: unsafe-development`` and "the contract can change"
(spec.txt header; guide's Private preview note). The platform key acts on every
connected account by changing one header, so it never leaves this process and
this module is the ONLY place that speaks to ``/v2/provisioning``.

Transport is the Stripe SDK's ``raw_request``: verified in this venv to extract
``stripe_context`` and ``stripe_version`` per request
(``_request_options.extract_options_from_dict``, called from
``_stripe_client.raw_request``), to select V2 mode for any ``/v2`` path
(``stripe._util.get_api_mode``), to JSON-encode a V2 body, and to surface the
Provisioning ``code`` on the raised error
(``_api_requestor.specific_v2_api_error``). It is synchronous, so every call
runs on ``asyncio.to_thread`` — the same shape as ``company_stripe``. Query
params are encoded into the URL here rather than handed to the SDK, which keeps
the transport swappable and makes ``reveal``/``scope`` explicit at the call
site.

Logging carries method, path, request id and error code. Nothing else: the
guide requires redacting "bodies that might contain credentials, access
configuration, callback secrets, or pre-authenticated URLs".

Deliberately absent: ``GET /features`` and ``GET /identity`` polling. Both
returned HTTP 500 ``api_error`` in live mode on 2026-08-29 while eligibility
and catalog worked, and nothing in this rail depends on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlencode, urlsplit

import stripe
from stripe import StripeClient

from core.config import settings

logger = logging.getLogger(__name__)

BASE = "/v2/provisioning"
PROVISIONING_VERSION = "unsafe-development"
# Stripe's message is surfaced to the caller but never logged; on the
# non-object-response path it embeds the whole raw body, so it is capped.
_MAX_MESSAGE_CHARS = 200
# Guide, "Build a server-side API client": "Set a 45-second or shorter network
# timeout that suits your platform." It is the socket timeout on the HTTP
# client, so a hung read is cut at the socket; `request`'s `asyncio.wait_for`
# is only the outer backstop for a worker thread that never returns at all
# (`wait_for` cannot kill the thread — it only frees the caller).
TIMEOUT_S = 45

# spec.txt §6: "Treat access_configuration, Project-variable value,
# ephemeral-key key, callback confirmation_secret, OAuth code_verifier, and
# Provider credentials as secrets" — plus the two pre-authenticated URL classes
# ("Do not expose Provider Deep Link URLs or hosted-payment URLs as durable
# URLs; they are short-lived and effectively bearer-like") and the OAuth
# redirect we open for the owner.
_SECRET_KEYS = frozenset(
    {
        "access_configuration",
        "value",
        "key",
        "confirmation_secret",
        "code_verifier",
        "code_challenge",
        "credentials",
        "checkout_session_url",
        "redirect_url",
        "url",
    }
)


class ProvisioningError(RuntimeError):
    """A Provisioning API refusal, reduced to a stable code and a safe message.

    ``code`` is one of spec.txt §5's public error codes (or ``transport_error``
    for anything below the API), and it — not an HTTP status — is what callers
    branch on: "Provider HTTP errors are translated; rely on Provisioning API
    codes rather than Provider status numbers."
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status
        # Seconds Stripe asked us to wait, when it said so (429 `retry-after`).
        self.retry_after = retry_after


class TransientProvisioningError(ProvisioningError):
    """The call may not have been decided: a 5xx from the preview, a 429, or a
    transport timeout with the request possibly in flight.

    A caller may re-READ (poll the resource / account request / payment
    profile) but must NEVER re-POST — a retried create is a second resource the
    owner pays for. Subclasses ProvisioningError so `except ProvisioningError`
    still catches everything.
    """


def redact(body: dict | None) -> dict:
    """A shallow copy safe to log. Never mutates the caller's dict."""
    return {k: ("[redacted]" if k in _SECRET_KEYS else v) for k, v in (body or {}).items()}


def _client() -> StripeClient:
    """``StripeClient`` takes no ``timeout``; the socket timeout lives on the
    HTTP client (``RequestsClient(timeout=…)``, default 80s), so it is set
    here or a hung preview read blocks a worker thread for 80 seconds."""
    return StripeClient(settings.STRIPE_SECRET_KEY, http_client=stripe.RequestsClient(timeout=TIMEOUT_S))


def _raw(method: str, url: str, options: dict, body: dict | None):
    """The one blocking call. Patched wholesale in tests, and the single line
    to change if the transport ever moves to httpx."""
    return _client().raw_request(method, url, **(body or {}), **options)


def _url(path: str, params: dict | None) -> str:
    if not params:
        return path
    # Booleans go over the wire as `true`/`false`, not Python's `True`.
    flat = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}
    return f"{path}?{urlencode(flat)}" if flat else path


def _relative(next_page_url: str) -> str:
    """Stripe returns an ABSOLUTE next_page_url, and it must not be handed to
    `raw_request` as-is: `stripe._util.get_api_mode` classifies a url by
    `startswith("/v2")`, so an absolute one would be sent as a V1 form-encoded
    request. Split rather than string-trim, so a host change cannot break it."""
    parts = urlsplit(next_page_url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _retry_after(e: Exception) -> float | None:
    """Seconds from the `retry-after` header, when it is a plain number.
    An HTTP-date form yields None — the caller's backoff covers that."""
    headers = getattr(e, "headers", None) or {}
    try:
        return float(headers.get("retry-after") or headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _error(e: Exception) -> ProvisioningError:
    if not isinstance(e, stripe.error.StripeError):
        return TransientProvisioningError("transport_error", "Could not reach Stripe; try again")
    # `json_body` is whatever came back off the wire. An intermediary can answer
    # a string or a list, and the SDK passes that straight through
    # (`_api_requestor.handle_error_response` raises APIError with the raw body
    # as `json_body`), so NOTHING here may assume a dict — an AttributeError
    # here escapes `request` untyped and, worse, unlogged.
    body = getattr(e, "json_body", None)
    envelope = body.get("error") if isinstance(body, dict) else None
    code = getattr(e, "code", None) or (envelope.get("code") if isinstance(envelope, dict) else None)
    status = getattr(e, "http_status", None)
    # Bounded: on that same non-object path the SDK's message interpolates the
    # WHOLE raw response body. It is carried for the caller, never logged.
    message = str(getattr(e, "user_message", None) or "Stripe refused the request")[:_MAX_MESSAGE_CHARS]
    transient = (status or 0) >= 500 or status == 429 or isinstance(e, stripe.error.RateLimitError)
    return (TransientProvisioningError if transient else ProvisioningError)(
        code or "provisioning_error",
        message,
        request_id=getattr(e, "request_id", None),
        http_status=status,
        retry_after=_retry_after(e),
    )


async def request(
    account_id: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> dict:
    if method == "get" and body:
        # The SDK would fold it into the query string instead of dropping it,
        # which is how a body silently becomes a filter. Caller error, loudly.
        raise ValueError("a GET carries its arguments in `params`, not `body`")
    url = _url(path, params)
    options = {
        "stripe_context": account_id,
        "stripe_version": PROVISIONING_VERSION,
        "api_key": settings.STRIPE_SECRET_KEY,
    }
    try:
        # TIMEOUT_S is the socket timeout; +5 leaves the HTTP client room to
        # raise its own timeout, which arrives typed, before this fires.
        result = await asyncio.wait_for(asyncio.to_thread(_raw, method, url, options, body), timeout=TIMEOUT_S + 5)
    except Exception as e:  # noqa: BLE001 — every failure becomes one typed error
        err = _error(e)
        # Method, path, code, request id. Never a body, never a message, and
        # never a URL from one — the query is stripped because a caller (or
        # `paginate`) can hand us a path that already carries filters.
        logger.warning(
            "provisioning: %s %s failed code=%s request_id=%s",
            method,
            path.split("?")[0],
            err.code,
            err.request_id,
        )
        raise err from e
    if isinstance(result, dict):
        return result
    # StripeClient.raw_request returns a StripeResponse: `.data` is the body
    # ALREADY parsed (json.loads with OrderedDict pairs), `.body` is the raw
    # text. The first unwrap attempt fed `.data` to json.loads as if it were
    # text and fell through to dict(result), which TypeErrors — seen live on
    # dev twice. Return the parsed dict; keep the text branch for safety.
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, (str, bytes)):
        return json.loads(data)
    return dict(result)


async def paginate(account_id: str, path: str, *, params: dict | None = None, limit_pages: int = 20) -> list[dict]:
    """Every ``data`` row across pages. Follows ``next_page_url`` verbatim —
    "Use a returned next_page_url or previous_page_url to continue. […] The
    page value is opaque." ``limit_pages`` is a runaway guard, not a contract."""
    rows: list[dict] = []
    url = _url(path, params)
    for _ in range(limit_pages):
        page = await request(account_id, "get", url)
        rows.extend(page.get("data") or [])
        nxt = page.get("next_page_url")
        if not nxt:
            break
        url = _relative(nxt)
    return rows


# --- 4.1 Projects, identity, access ----------------------------------------


async def eligibility(account_id: str) -> dict:
    return await request(account_id, "get", f"{BASE}/eligibility")


async def identity(account_id: str, *, project: str | None = None) -> dict:
    return await request(account_id, "get", f"{BASE}/identity", params={"project": project})


async def create_project(account_id: str, *, name: str, catalog: str | None = None) -> dict:
    """`catalog` is `prod | dev | testing` — configuration, never a literal."""
    body: dict = {"name": name}
    if catalog:
        body["catalog"] = catalog
    return await request(account_id, "post", f"{BASE}/projects", body=body)


async def list_projects(account_id: str) -> list[dict]:
    return await paginate(account_id, f"{BASE}/projects", params={"limit": 100})


# --- 4.2 Catalog (account context) -----------------------------------------


async def _catalog_with_fallback(account_id: str, path: str, params: dict) -> list[dict]:
    """Filter by the configured partition, but never let the filter blank the
    catalog: the spec calls `catalog` "a Preview implementation field" and the
    sandbox returns services with `catalog: null` (verified live 2026-08-31),
    so a partition filter over unpopulated rows matches nothing. When the
    filtered read is empty, re-read unfiltered — account context already
    scopes what this owner may see; the partition is a selector, not a fence.
    """
    rows = await paginate(account_id, path, params=params)
    if rows or not params.get("catalog"):
        return rows
    unfiltered = {k: v for k, v in params.items() if k != "catalog"}
    rows = await paginate(account_id, path, params=unfiltered)
    if rows:
        logger.warning(
            "provisioning: catalog filter %r matched nothing but the unfiltered "
            "read has %d rows — Stripe left the partition field unpopulated",
            params.get("catalog"),
            len(rows),
        )
    return rows


async def catalog_providers(account_id: str) -> list[dict]:
    return await _catalog_with_fallback(
        account_id,
        f"{BASE}/catalog/providers",
        {"catalog": settings.STRIPE_PROJECTS_CATALOG, "limit": 100},
    )


async def catalog_services(account_id: str, *, provider_name: str | None = None) -> list[dict]:
    """`catalog` selects the partition (`prod | dev | testing`). It is
    configuration, never a literal — dev runs `testing` and prod runs `prod`,
    and reading one partition while writing another is how you provision into a
    catalog the owner never saw. (When Stripe leaves the partition field
    unpopulated the filter falls back to unfiltered — see _catalog_with_fallback.)"""
    return await _catalog_with_fallback(
        account_id,
        f"{BASE}/catalog/services",
        {
            "provider_name": provider_name,
            "catalog": settings.STRIPE_PROJECTS_CATALOG,
            "limit": 100,
        },
    )


# --- 4.3 Provider account linking ------------------------------------------


async def list_provider_connections(account_id: str) -> list[dict]:
    return await paginate(account_id, f"{BASE}/provider_connections", params={"limit": 100})


async def create_account_request(account_id: str, *, provider: str, project_id: str | None = None) -> dict:
    """`provider` is the current field; `project_id` is the documented
    exception where a `_id` suffix is still current (spec.txt 4.3: "project_id
    is the current Account-request field"). PKCE fields are omitted: "Use PKCE
    fields only if your integration owns the OAuth callback"."""
    body: dict = {"provider": provider, "configuration": {}}
    if project_id:
        body["project_id"] = project_id
    return await request(account_id, "post", f"{BASE}/account_requests", body=body)


async def get_account_request(account_id: str, request_id_: str) -> dict:
    return await request(account_id, "get", f"{BASE}/account_requests/{request_id_}")


async def submit_account_request_information(account_id: str, request_id_: str, *, information: dict) -> dict:
    return await request(
        account_id,
        "post",
        f"{BASE}/account_requests/{request_id_}/submit_information",
        body={"information": information},
    )


# --- 4.4 Resources ----------------------------------------------------------


async def create_resource(
    account_id: str,
    *,
    service_ref: str,
    provider: str,
    project: str | None,
    name: str,
    configuration: dict,
    catalog: str | None = None,
) -> dict:
    body: dict = {
        "service_ref": service_ref,
        "provider": provider,
        "name": name,
        "configuration": configuration,
    }
    if project:
        body["project"] = project
    if catalog:
        body["catalog"] = catalog
    return await request(account_id, "post", f"{BASE}/resources", body=body)


async def get_resource(account_id: str, resource_id: str) -> dict:
    return await request(account_id, "get", f"{BASE}/resources/{resource_id}")


async def submit_resource_information(account_id: str, resource_id: str, *, submitted_information: dict) -> dict:
    return await request(
        account_id,
        "post",
        f"{BASE}/resources/{resource_id}/submit_information",
        body={"submitted_information": submitted_information},
    )


async def list_resources(account_id: str, *, project: str | None = None) -> list[dict]:
    return await paginate(account_id, f"{BASE}/resources", params={"project": project, "limit": 100})


async def remove_resource(account_id: str, resource_id: str) -> dict:
    """Asks the Provider to deprovision the underlying infrastructure. Only
    ever reached from the owner's own separately confirmed click (spec §3.6)."""
    return await request(account_id, "post", f"{BASE}/resources/{resource_id}/remove")


async def unlink_resource(account_id: str, resource_id: str) -> dict:
    return await request(account_id, "post", f"{BASE}/resources/{resource_id}/unlink")


async def rotate_credentials(account_id: str, resource_id: str) -> dict:
    return await request(account_id, "post", f"{BASE}/resources/{resource_id}/rotate_credentials")


# --- 4.5 Credentials --------------------------------------------------------


async def environment_info(account_id: str, *, project: str, provider: str, service: str, reveal: bool) -> dict:
    """`reveal` and `scope` are non-optional proto fields, so both are always
    sent. Scope is SERVICE: the API "can scope by all, Provider, or Service,
    but not by one Resource", so the caller still selects by `resource_id`."""
    return await request(
        account_id,
        "get",
        f"{BASE}/environment_info",
        params={
            "reveal": reveal,
            "scope": "SERVICE",
            "project": project,
            "provider": provider,
            "service": service,
        },
    )


# --- 4.6 Payment ------------------------------------------------------------


async def payment_profile(account_id: str) -> dict:
    """A missing profile returns `not_found` (spec.txt 4.6)."""
    return await request(account_id, "get", f"{BASE}/payment_profile")


def _usage_limits(max_amount: int, currency: str, recurring_interval: str) -> dict:
    """`max_amount` MUST be a string. Verified 2026-08-29: Stripe rejects an
    integer with "Potentially large numbers must be String values for safety"."""
    return {"max_amount": str(max_amount), "currency": currency, "recurring_interval": recurring_interval}


async def create_payment_method_request(
    account_id: str, *, max_amount: int, currency: str, recurring_interval: str
) -> dict:
    """Hosted collection — the owner's own card, on Stripe's page.

    Verified working under `Stripe-Context` on 2026-08-29 (HTTP 200,
    `checkout_initiated` + `checkout_session_url`), so no `source_*` fields and
    no `payment_method_owner`. The platform-owned-source alternative
    (`payment_method_owner=platform`) is documented in spec §3 and not built.
    """
    return await request(
        account_id,
        "post",
        f"{BASE}/payment_method_requests",
        body={"usage_limits": _usage_limits(max_amount, currency, recurring_interval)},
    )


# --- 4.7 Deep links + usage -------------------------------------------------


async def create_deep_link(
    account_id: str,
    *,
    provider: str,
    project: str | None = None,
    purpose: str = "dashboard",
    catalog: str | None = None,
) -> dict:
    """Short-lived and pre-authenticated: "Present it immediately and do not
    persist, cache, or log it." Only call it for a purpose the Provider
    advertises in `deep_link_purposes`."""
    body: dict = {"purpose": purpose, "provider": provider}
    if project:
        body["project"] = project
    if catalog:
        body["catalog"] = catalog
    return await request(account_id, "post", f"{BASE}/deep_links", body=body)


async def charges(
    account_id: str,
    *,
    start_time: str,
    end_time: str,
    livemode: bool,
    project: str | None = None,
    limit: int | None = None,
    page: str | None = None,
) -> dict:
    """`start_time`, `end_time` and `livemode` are non-optional current proto
    fields (spec.txt 4.7).

    NOT walkable with `paginate`: this endpoint answers `usage[]`, not the
    `v2.list` `data[]` every other list route returns, so `paginate` would
    collect nothing. Pass `page` from the response's `next_page_url` — or add
    a usage-shaped walker the day a fleet outgrows one page."""
    return await request(
        account_id,
        "get",
        f"{BASE}/charges",
        params={
            "start_time": start_time,
            "end_time": end_time,
            "livemode": livemode,
            "project": project,
            "limit": limit,
            "page": page,
        },
    )


def is_livemode() -> bool:
    """Which mode's charges exist to be asked for.

    The platform key decides: a live key's ``Stripe-Context`` sees only
    live-mode objects, a Sandbox key's only test ones. Deliberately NOT derived
    from ``STRIPE_PROJECTS_CATALOG`` — that is a catalog selector, and the
    guide is explicit that a TESTING catalog can still make real charges.
    """
    return settings.STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_"))


async def charges_usage(
    account_id: str,
    *,
    start_time: str,
    end_time: str,
    livemode: bool,
    project: str | None = None,
    limit_pages: int = 5,
) -> tuple[list[dict], bool]:
    """Every ``usage`` row in the window, and whether the walk stopped early.

    The usage-shaped walker ``charges`` promises: ``paginate`` collects nothing
    here because this endpoint answers ``usage[]`` rather than ``data[]``.
    Pages are followed by ``next_page_url`` verbatim, never constructed.
    """
    page = await charges(account_id, start_time=start_time, end_time=end_time, livemode=livemode, project=project)
    rows: list[dict] = list(page.get("usage") or [])
    for _ in range(limit_pages - 1):
        nxt = page.get("next_page_url")
        if not nxt:
            return rows, False
        page = await request(account_id, "get", _relative(nxt))
        rows.extend(page.get("usage") or [])
    return rows, bool(page.get("next_page_url"))
