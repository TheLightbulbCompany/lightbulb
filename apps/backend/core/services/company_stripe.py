"""The company's own Stripe account on our Connect platform (Accounts v2).

A ``merchant``-configured v2 account with the full Dashboard; Stripe collects
the account's fees and carries its losses, so the platform pays nothing and
takes no application fee. The owner keeps the account and logs into
dashboard.stripe.com. Two doors — create via ``v2.core.accounts`` + hosted
onboarding, or connect an existing account via Standard OAuth (v1-only by
Stripe's own limitations list) — both land the same ``acct_…`` on the
billing row, and v2 endpoints manage either kind.

Status booleans are read through the v1 shape (``details_submitted`` /
``charges_enabled`` / ``payouts_enabled``) for both doors — v1 endpoints
accept v2 ids — so the webhook, repo and pages share one vocabulary.

Blocking SDK calls run on a worker thread, as in ``payout_service``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from urllib.parse import urlencode

import stripe
from stripe import StripeClient

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

CONNECT_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"

# v2 defaults: Stripe collects fees from the account and carries its losses
# (the platform pays nothing); full Dashboard; merchant config = accept
# card payments and receive payouts to its own bank.
ACCOUNT_DEFAULTS = {
    "currency": "usd",
    "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
}
MERCHANT_CONFIGURATION = {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}

# Verbatim from the guide's "Create the connected account" example. `include`
# is what makes the developer configuration and requirements come back on the
# create response instead of a second retrieve.
DEVELOPER_CONFIGURATION = {"developer": {"capabilities": {"projects": {"requested": True}}}}
DEVELOPER_INCLUDE = ["configuration.developer", "requirements", "identity"]


def account_configuration() -> dict:
    if not settings.STRIPE_PROJECTS_DEVELOPER_CONFIG:
        return dict(MERCHANT_CONFIGURATION)
    return {**MERCHANT_CONFIGURATION, **DEVELOPER_CONFIGURATION}


def onboarding_configurations() -> list[str]:
    """A return to `return_url` proves only that the browser came back — "it
    doesn't prove that the projects capability is active" (guide). The
    workflow's eligibility read is the truth."""
    return ["merchant", "developer"] if settings.STRIPE_PROJECTS_DEVELOPER_CONFIG else ["merchant"]


def accounts_v2_options(**extra) -> dict:
    """Per-request options for the Accounts v2 calls. Pins `Stripe-Version`
    only when configured, so an unset value keeps the SDK default."""
    options = dict(extra)
    if settings.STRIPE_ACCOUNTS_V2_VERSION:
        options["stripe_version"] = settings.STRIPE_ACCOUNTS_V2_VERSION
    return options


def _client() -> StripeClient:
    return StripeClient(settings.STRIPE_SECRET_KEY)


def _set_key() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY


async def resolve_company_name(owner_id: str) -> str:
    """The Paperclip company name (the owner named it in Teams); falls back to
    the owner id so a Stripe account is never created with an empty name.

    Lives here, not in the router: the approval service needs it too, and a
    service importing a router is an import cycle waiting to happen."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        return owner_id
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    company = await admin.get_company(session_cookie=cookie, company_id=company_id)
    return (company or {}).get("name") or owner_id


async def create_company_account(*, owner_id: str, email: str, company_name: str) -> str:
    """Create the account shell. ``identity`` carries only the country —
    entity type and everything else are collected by Stripe in hosted
    onboarding (``requirements_collector`` defaults to ``stripe``). Fresh
    uuid4 idempotency key on purpose — a stable key would return a deleted
    account from Stripe's 24h cache; the router de-dups on the billing row."""
    client = _client()
    params: dict = {
        "display_name": company_name,
        "dashboard": "full",
        "identity": {"country": "us"},
        "configuration": account_configuration(),
        "defaults": ACCOUNT_DEFAULTS,
        "metadata": {"owner_id": owner_id, "kind": "company"},
    }
    # v2 rejects an empty contact_email; Stripe collects one in onboarding.
    if email:
        params["contact_email"] = email
    if settings.STRIPE_PROJECTS_DEVELOPER_CONFIG:
        params["include"] = list(DEVELOPER_INCLUDE)
    account = await asyncio.to_thread(
        client.v2.core.accounts.create,
        params,
        options=accounts_v2_options(idempotency_key=f"company_account_create:{uuid.uuid4()}"),
    )
    return account.id


async def create_company_onboarding_link(*, account_id: str) -> str:
    """Single-use, ~10-minute hosted onboarding link (v2). Mint per request."""
    client = _client()
    link = await asyncio.to_thread(
        client.v2.core.account_links.create,
        {
            "account": account_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "configurations": onboarding_configurations(),
                    "refresh_url": settings.STRIPE_COMPANY_REFRESH_URL,
                    "return_url": settings.STRIPE_COMPANY_RETURN_URL,
                },
            },
        },
        options=accounts_v2_options(),
    )
    return link.url


def build_connect_authorize_url(*, state: str) -> str:
    """Standard OAuth for an owner who already runs Stripe."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.STRIPE_CONNECT_CLIENT_ID,
            "scope": "read_write",
            "state": state,
            "redirect_uri": settings.STRIPE_COMPANY_OAUTH_REDIRECT_URL,
        }
    )
    return f"{CONNECT_AUTHORIZE_URL}?{query}"


async def exchange_connect_code(*, code: str) -> str:
    _set_key()
    resp = await asyncio.to_thread(stripe.OAuth.token, grant_type="authorization_code", code=code)
    return resp["stripe_user_id"]


async def deauthorize(*, account_id: str) -> bool:
    """Revoke the platform's access. The account itself keeps working as a
    normal Stripe account — this is the owner's exit, never a delete
    ("After revocation, the account can't be accessed by your platform in the
    Dashboard or through the API. The account can continue to access its own
    data and functionality through the account's Dashboard.").

    Returns False, having called nothing, when there is no platform client id
    to revoke with. `STRIPE_CONNECT_CLIENT_ID` was `""` in prod until the
    Connect app was configured, and `client_id=""` buys an `invalid_client`
    error rather than a revoke — so the call is pure downside. Callers must
    unbind regardless; see `release_and_unbind`.
    """
    if not settings.STRIPE_CONNECT_CLIENT_ID:
        logger.warning("company_stripe: no STRIPE_CONNECT_CLIENT_ID; not revoking %s at Stripe", account_id)
        put_metric("company_stripe.release", dimensions={"result": "unconfigured"})
        return False
    _set_key()
    await asyncio.to_thread(
        stripe.OAuth.deauthorize,
        client_id=settings.STRIPE_CONNECT_CLIENT_ID,
        stripe_user_id=account_id,
    )
    return True


async def release_and_unbind(*, owner_id: str, account_id: str) -> bool:
    """The owner's exit, in the one order that is safe: **always unbind**.

    Every caller (the account wipe, the owner's revoke-all, the disconnect
    route) used to open-code "deauthorize, THEN clear" — so any Stripe
    failure skipped the unbind and left the row bound to an account we claim
    to have let go of. Ordering was the bug; this function is the fix, and no
    caller may branch the unbind on the return value.

    `OAuth.deauthorize` is attempted for BOTH doors, created and connected.
    It is the only Stripe primitive with the right semantics — v2 `close`
    "removes access to the Account and its associated resources", a delete
    that spec §3.6 forbids and that Stripe refuses for our account shape
    anyway (`stripe_loss_liable_cannot_be_deleted`). But a platform-created
    v2 account was never an OAuth grant, and Stripe's contract requires the
    account be "connected to this application", answering `invalid_client`
    otherwise. So the call is best-effort by construction.

    Returns True only when Stripe confirmed the revoke. False means our side
    is clean but the platform link may still stand — record it, never hide it.
    """
    try:
        released = await deauthorize(account_id=account_id)
    except Exception:  # noqa: BLE001 — a vendor failure must not strand the binding
        logger.exception("company_stripe: deauthorize failed owner_id=%s account_id=%s", owner_id, account_id)
        put_metric("company_stripe.release", dimensions={"result": "error"})
        released = False
    if released:
        put_metric("company_stripe.release", dimensions={"result": "ok"})
    await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
    return released


async def retrieve_status(*, account_id: str) -> dict:
    _set_key()
    account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
    return {
        "details_submitted": bool(account.get("details_submitted")),
        "charges_enabled": bool(account.get("charges_enabled")),
        "payouts_enabled": bool(account.get("payouts_enabled")),
    }
