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
from core.services.credentials import integration_secret_store
from core.services.workspace_name import workspace_name as resolve_company_name  # noqa: F401 -- routers, board_cards

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
    """Every account requests the developer configuration: a workflow is
    refused until the `projects` capability is active, so an account created
    without it could never satisfy the gate."""
    return {**MERCHANT_CONFIGURATION, **DEVELOPER_CONFIGURATION}


def accounts_v2_options(**extra) -> dict:
    """Per-request options for the Accounts v2 calls. Pins `Stripe-Version` to
    `STRIPE_ACCOUNTS_V2_VERSION`, else the provisioning preview: the
    `developer` configuration exists only there, and the SDK default rejects it
    as "configuration.developer: Unknown field" (dev sandbox, 2026-10-27)."""
    return {**extra, "stripe_version": settings.STRIPE_ACCOUNTS_V2_VERSION or "2026-09-30.preview"}


def _client() -> StripeClient:
    return StripeClient(settings.STRIPE_SECRET_KEY)


def _set_key() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY


async def create_company_account(*, owner_id: str, email: str, company_name: str, kind: str = "company") -> str:
    """Create the account shell. ``identity`` carries only the country —
    entity type and everything else are collected by Stripe in hosted
    onboarding (``requirements_collector`` defaults to ``stripe``). Fresh
    uuid4 idempotency key on purpose — a stable key would return a deleted
    account from Stripe's 24h cache; the router de-dups on the billing row."""
    client = _client()
    # kind "company_services" = the services account (see projects_account_of).
    # It only provisions, so it asks for `developer` alone: on that shape Stripe
    # requires just the contact email for `projects`; `merchant` adds ~15
    # KYC/bank fields (dev sandbox, 10-28). Stripe refuses `dashboard` and
    # `defaults.responsibilities` without merchant ("You cannot set … unless the
    # account is configured as a merchant", prod + dev sandbox 10-29).
    services = kind == "company_services"
    params: dict = {
        "display_name": company_name,
        "identity": {"country": "us"},
        "configuration": DEVELOPER_CONFIGURATION if services else account_configuration(),
        "defaults": {"currency": ACCOUNT_DEFAULTS["currency"]} if services else ACCOUNT_DEFAULTS,
        "metadata": {"owner_id": owner_id, "kind": kind},
    }
    if not services:
        params["dashboard"] = "full"
    # v2 rejects an empty contact_email; Stripe collects one in onboarding.
    if email:
        params["contact_email"] = email
    params["include"] = list(DEVELOPER_INCLUDE)
    account = await asyncio.to_thread(
        client.v2.core.accounts.create,
        params,
        options=accounts_v2_options(idempotency_key=f"company_account_create:{uuid.uuid4()}"),
    )
    return account.id


async def create_company_onboarding_link(*, account_id: str, update: bool = False) -> str:
    """Single-use, ~10-minute hosted link (v2). Mint per request.

    ``update`` is for an account that already finished onboarding once: the
    guide's ``account_update`` use case is "subsequent corrections", and it
    takes no ``configurations`` list.

    The call runs on the provisioning preview, which REJECTS
    ``configurations`` ("Unknown field", dev sandbox 2026-10-27) and collects
    what the account's own configurations require; the provisioning guide
    sends ``collection_options`` instead. A return to `return_url` proves only
    that the browser came back; the workflow's eligibility read is the truth."""
    client = _client()
    urls = {"refresh_url": settings.STRIPE_COMPANY_REFRESH_URL, "return_url": settings.STRIPE_COMPANY_RETURN_URL}
    onboarding = {"collection_options": {"fields": "currently_due"}, **urls}
    use_case = (
        {"type": "account_update", "account_update": urls}
        if update
        else {"type": "account_onboarding", "account_onboarding": onboarding}
    )
    link = await asyncio.to_thread(
        client.v2.core.account_links.create,
        {"account": account_id, "use_case": use_case},
        options=accounts_v2_options(),
    )
    return link.url


def projects_account_of(row: dict | None) -> str | None:
    """The ONE answer to "which account does Stripe Projects run on": the
    services account when there is one, else the main account when the
    platform created it. A `connected` account is v1 Standard, which Accounts
    v2 refuses (`v1_account_instead_of_v2_account`) and Projects answers 403
    (prod, 2026-10-27); an `api_key` row has no platform grant. Both need a
    services account; None says so."""
    row = row or {}
    if row.get("company_stripe_projects_account_id"):
        return row["company_stripe_projects_account_id"]
    if row.get("company_stripe_source") == "created":
        return row.get("company_stripe_account_id")
    return None


async def create_account_session(*, account_id: str) -> str:
    """A client secret for Connect embedded onboarding (v1 Account Sessions
    accept v2 ids). Never log the return value."""
    session = await asyncio.to_thread(
        _client().v1.account_sessions.create,
        {"account": account_id, "components": {"account_onboarding": {"enabled": True}}},
    )
    return session.client_secret


async def request_projects_capability(*, account_id: str) -> None:
    """Add the developer configuration to an account created without it
    (Accounts v2: a configuration is added by updating the account). The link
    that follows then collects whatever the new capability makes due. Safe to
    repeat: it only sets `requested: true`."""
    await asyncio.to_thread(
        _client().v2.core.accounts.update,
        account_id,
        {"configuration": DEVELOPER_CONFIGURATION},
        options=accounts_v2_options(),
    )


async def projects_ready(*, account_id: str) -> bool:
    """The `projects` capability is active — the guide's gate to provisioning
    ("Don't treat the Account Link redirect alone as proof of completion").
    Outstanding requirements are not read here: the workflow's eligibility
    read refuses on those, and this answers the one thing the card can fix."""
    account = await asyncio.to_thread(
        _client().v2.core.accounts.retrieve,
        account_id,
        {"include": ["configuration.developer", "requirements"]},
        options=accounts_v2_options(),
    )
    caps = ((account.get("configuration") or {}).get("developer") or {}).get("capabilities") or {}
    return (caps.get("projects") or {}).get("status") == "active"


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


async def release_and_unbind(*, owner_id: str, account_id: str, source: str | None = None) -> bool:
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
    if source == "api_key":
        # No platform grant exists to revoke — the only thing to let go of is
        # our copy of their key. The key goes FIRST and the binding is cleared
        # only once it is confirmed gone: a stranded row is recoverable (the
        # owner clicks Disconnect again and this runs again), a stranded
        # credential behind no row is not.
        try:
            await delete_api_key(owner_id)
        except Exception:  # noqa: BLE001 — keep the row so the next Disconnect retries the delete
            logger.exception("company_stripe: api key delete failed owner_id=%s; binding kept", owner_id)
            return False
        await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
        return True
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


def _status_of(account: dict) -> dict:
    return {
        "details_submitted": bool(account.get("details_submitted")),
        "charges_enabled": bool(account.get("charges_enabled")),
        "payouts_enabled": bool(account.get("payouts_enabled")),
    }


async def retrieve_status(*, account_id: str, api_key: str | None = None) -> dict:
    """Connect rows: the platform key retrieves the connected account by id.
    ``api_key`` rows: the owner's own key retrieves ITS account (the id is
    implied by the key, so it is ignored for addressing and only used by the
    caller to condition the write)."""
    if api_key:
        account = await asyncio.to_thread(stripe.Account.retrieve, api_key=api_key)
        return {**_status_of(account), "livemode": key_livemode(api_key)}
    _set_key()
    account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
    return _status_of(account)


# --- The api_key path ------------------------------------------------------
# The owner's own restricted key, held backend-only. It never reaches a
# container, openclaw.json, env.vars or EFS — the agent-tools MCP grant is a
# separate OAuth token with the opposite posture, and nothing here shares or
# derives one from the other.

API_KEY_SECRET_PROVIDER = "company_stripe"
# Restricted keys are what we ask for; a secret key is accepted because Stripe
# itself treats them as interchangeable server-side and the owner may only
# have one. Publishable keys cannot read anything — refuse those up front.
_ACCEPTED_KEY_PREFIXES = ("rk_live_", "rk_test_", "sk_live_", "sk_test_")


class InvalidApiKey(ValueError):
    """The pasted string is not a usable Stripe server key."""


async def validate_api_key(api_key: str) -> dict:
    """Prove the key works and learn whose it is, in ONE call. Returns
    ``{"account_id", "livemode", **status}``. Raises ``InvalidApiKey`` for a
    malformed prefix; lets ``stripe.error.StripeError`` propagate for a key
    Stripe rejects so the router can map it."""
    key = (api_key or "").strip()
    if not key.startswith(_ACCEPTED_KEY_PREFIXES):
        raise InvalidApiKey("Paste a restricted key (rk_…) or secret key (sk_…) from your Stripe Dashboard")
    # Either mode binds. A test-mode key is a real use case — a sandbox
    # install of the Stripe App, or Stripe's own reviewers, who may only use
    # test accounts — so it is accepted and LABELLED ("Test mode" on the
    # card) rather than refused; the label is what keeps sandbox figures from
    # reading as revenue.
    account = await asyncio.to_thread(stripe.Account.retrieve, api_key=key)
    await asyncio.to_thread(_probe_read_permissions, key)
    return {"account_id": account["id"], "livemode": key_livemode(key), **_status_of(account)}


def key_livemode(api_key: str) -> bool:
    return api_key.startswith(("rk_live_", "sk_live_"))


# Every list the Money surface performs, probed at bind time with limit=1, so a
# key missing one of the six read permissions is refused WITH THE PERMISSION
# NAMED instead of binding and 403ing on the first page load. `Account` is
# covered by the retrieve above.
_READ_PROBES = (
    ("Charges", lambda c: c.v1.charges.list(params={"limit": 1})),
    ("Customers", lambda c: c.v1.customers.list(params={"limit": 1})),
    ("Products", lambda c: c.v1.products.list(params={"limit": 1})),
    ("Prices", lambda c: c.v1.prices.list(params={"limit": 1})),
    ("Balance", lambda c: c.v1.balance_transactions.list(params={"limit": 1})),
)


def _probe_read_permissions(key: str) -> None:
    """Blocking — call via ``asyncio.to_thread``."""
    client = StripeClient(key)
    for label, call in _READ_PROBES:
        try:
            call(client)
        except stripe.error.PermissionError as e:
            raise InvalidApiKey(f"That key can't read {label} — add {label}: Read to it and paste it again") from e


async def store_api_key(owner_id: str, api_key: str) -> None:
    await integration_secret_store.put(owner_id, API_KEY_SECRET_PROVIDER, {"api_key": api_key.strip()})


async def load_api_key(owner_id: str) -> str | None:
    cred = await integration_secret_store.get(owner_id, API_KEY_SECRET_PROVIDER)
    return (cred or {}).get("api_key") or None


async def delete_api_key(owner_id: str) -> None:
    await integration_secret_store.delete(owner_id, API_KEY_SECRET_PROVIDER)
