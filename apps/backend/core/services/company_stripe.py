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

Blocking SDK calls run on a worker thread.
"""

from __future__ import annotations

import asyncio
import logging

import stripe
from stripe import StripeClient

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


def _client() -> StripeClient:
    return StripeClient(settings.STRIPE_SECRET_KEY)


def _set_key() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY


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


async def load_api_key(owner_id: str) -> str | None:
    cred = await integration_secret_store.get(owner_id, API_KEY_SECRET_PROVIDER)
    return (cred or {}).get("api_key") or None


async def delete_api_key(owner_id: str) -> None:
    await integration_secret_store.delete(owner_id, API_KEY_SECRET_PROVIDER)
