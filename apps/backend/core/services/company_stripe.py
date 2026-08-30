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
import uuid
from urllib.parse import urlencode

import stripe
from stripe import StripeClient

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

CONNECT_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"

# v2 defaults: Stripe collects fees from the account and carries its losses
# (the platform pays nothing); full Dashboard; merchant config = accept
# card payments and receive payouts to its own bank.
ACCOUNT_DEFAULTS = {
    "currency": "usd",
    "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
}
MERCHANT_CONFIGURATION = {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}


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
        "configuration": MERCHANT_CONFIGURATION,
        "defaults": ACCOUNT_DEFAULTS,
        "metadata": {"owner_id": owner_id, "kind": "company"},
    }
    # v2 rejects an empty contact_email; Stripe collects one in onboarding.
    if email:
        params["contact_email"] = email
    account = await asyncio.to_thread(
        client.v2.core.accounts.create,
        params,
        options={"idempotency_key": f"company_account_create:{uuid.uuid4()}"},
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
                    "configurations": ["merchant"],
                    "refresh_url": settings.STRIPE_COMPANY_REFRESH_URL,
                    "return_url": settings.STRIPE_COMPANY_RETURN_URL,
                },
            },
        },
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
    normal Stripe account — this is the owner's exit, never a delete."""
    _set_key()
    await asyncio.to_thread(
        stripe.OAuth.deauthorize,
        client_id=settings.STRIPE_CONNECT_CLIENT_ID,
        stripe_user_id=account_id,
    )
    return True


async def retrieve_status(*, account_id: str) -> dict:
    _set_key()
    account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
    return {
        "details_submitted": bool(account.get("details_submitted")),
        "charges_enabled": bool(account.get("charges_enabled")),
        "payouts_enabled": bool(account.get("payouts_enabled")),
    }
