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
import logging
from urllib.parse import urlencode, urlsplit

import stripe
from stripe import StripeClient

from core.config import settings

logger = logging.getLogger(__name__)

BASE = "/v2/provisioning"
PROVISIONING_VERSION = "unsafe-development"
# Guide, "Build a server-side API client": "Set a 45-second or shorter network
# timeout that suits your platform."
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
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status


class TransientProvisioningError(ProvisioningError):
    """The call may not have been decided: a 5xx from the preview, or a
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
    return StripeClient(settings.STRIPE_SECRET_KEY)


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
    """Stripe returns an absolute next_page_url; raw_request takes a path.
    Split rather than string-trim so a host change cannot silently break it."""
    parts = urlsplit(next_page_url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _error(e: Exception) -> ProvisioningError:
    if isinstance(e, stripe.error.StripeError):
        code = getattr(e, "code", None)
        if not code:
            code = ((getattr(e, "json_body", None) or {}).get("error") or {}).get("code")
        status = getattr(e, "http_status", None)
        cls = TransientProvisioningError if (status or 0) >= 500 else ProvisioningError
        return cls(
            code or "provisioning_error",
            str(getattr(e, "user_message", None) or "Stripe refused the request"),
            request_id=getattr(e, "request_id", None),
            http_status=status,
        )
    return TransientProvisioningError("transport_error", "Could not reach Stripe; try again")


async def request(
    account_id: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> dict:
    url = _url(path, params)
    options = {
        "stripe_context": account_id,
        "stripe_version": PROVISIONING_VERSION,
        "api_key": settings.STRIPE_SECRET_KEY,
    }
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_raw, method, url, options, body), timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — every failure becomes one typed error
        err = _error(e)
        # Method, path, code, request id. Never a body and never a URL from one.
        logger.warning("provisioning: %s %s failed code=%s request_id=%s", method, path, err.code, err.request_id)
        raise err from e
    return result if isinstance(result, dict) else dict(result)


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


async def catalog_providers(account_id: str) -> list[dict]:
    return await paginate(
        account_id,
        f"{BASE}/catalog/providers",
        params={"catalog": settings.STRIPE_PROJECTS_CATALOG, "limit": 100},
    )


async def catalog_services(account_id: str, *, provider_name: str | None = None) -> list[dict]:
    """`catalog` selects the partition (`prod | dev | testing`). It is
    configuration, never a literal — dev runs `testing` and prod runs `prod`,
    and reading one partition while writing another is how you provision into a
    catalog the owner never saw."""
    return await paginate(
        account_id,
        f"{BASE}/catalog/services",
        params={
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


async def update_payment_limit(
    account_id: str,
    *,
    max_amount: int,
    currency: str,
    recurring_interval: str,
    provider: str | None = None,
) -> dict:
    """Omit `provider` for the account-wide default; set it to move only that
    Provider's override (spec.txt 4.6)."""
    body: dict = {"usage_limits": _usage_limits(max_amount, currency, recurring_interval)}
    if provider:
        body["provider"] = provider
    return await request(account_id, "post", f"{BASE}/payment_profile/update_limit", body=body)


# --- 4.7 Deep links + usage -------------------------------------------------


async def create_deep_link(
    account_id: str, *, provider: str, project: str | None = None, purpose: str = "dashboard"
) -> dict:
    """Short-lived and pre-authenticated: "Present it immediately and do not
    persist, cache, or log it." Only call it for a purpose the Provider
    advertises in `deep_link_purposes`."""
    body: dict = {"purpose": purpose, "provider": provider}
    if project:
        body["project"] = project
    return await request(account_id, "post", f"{BASE}/deep_links", body=body)


async def charges(
    account_id: str, *, start_time: str, end_time: str, livemode: bool, project: str | None = None
) -> dict:
    """`start_time`, `end_time` and `livemode` are non-optional current proto
    fields (spec.txt 4.7)."""
    return await request(
        account_id,
        "get",
        f"{BASE}/charges",
        params={"start_time": start_time, "end_time": end_time, "livemode": livemode, "project": project},
    )
