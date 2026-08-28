"""Read an entrant's OWN Stripe revenue through a pasted restricted key.

Why a restricted key and not Connect OAuth: Stripe grants `read_only` OAuth
scope to Extension-registered apps only, and ours is a Platform (marketplace
payouts). A restricted key with Balance:Read + Charges:Read is read-only by
construction. It is stored on the BYO-key Secrets Manager rail (only the
backend task role and the per-user task EXECUTION role can read that prefix;
a running agent cannot) and is never logged or echoed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import boto3
import stripe

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret

logger = logging.getLogger(__name__)

KEY_PATTERN_PROD = re.compile(r"^rk_live_[A-Za-z0-9]+$")
KEY_PATTERN_NONPROD = re.compile(r"^rk_(live|test)_[A-Za-z0-9]+$")
REVENUE_TYPES = frozenset({"charge", "payment"})
REFUND_TYPES = frozenset({"refund", "payment_refund"})
_PAGE = 100


class KeyRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MixedCurrency(ValueError):
    pass


def key_pattern() -> re.Pattern:
    return KEY_PATTERN_PROD if settings.ENVIRONMENT == "prod" else KEY_PATTERN_NONPROD


def _list_page(key: str, **kwargs):
    return stripe.BalanceTransaction.list(api_key=key, **kwargs)


def _map_stripe_error(e: Exception) -> KeyRejected:
    if isinstance(e, stripe.AuthenticationError):
        return KeyRejected("stripe_auth")
    if isinstance(e, stripe.PermissionError):
        return KeyRejected("stripe_permission")
    return KeyRejected("stripe_error")


async def validate_key(key: str) -> None:
    if not key_pattern().match(key or ""):
        raise KeyRejected("bad_prefix")
    try:
        await run_in_thread(_list_page, key, limit=1)
    except stripe.StripeError as e:
        raise _map_stripe_error(e) from None


async def sum_net_revenue(key: str, start: datetime, end: datetime) -> tuple[int, str]:
    """Gross charges minus refunds in [start, end), in the account's balance
    currency. Payouts, fees, transfers and everything else are not revenue."""
    cents = 0
    currency: str | None = None
    kwargs: dict = {"created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())}, "limit": _PAGE}
    while True:
        try:
            page = await run_in_thread(_list_page, key, **kwargs)
        except stripe.StripeError as e:
            raise _map_stripe_error(e) from None
        for txn in page.data:
            if txn.type not in REVENUE_TYPES and txn.type not in REFUND_TYPES:
                continue
            if currency is None:
                currency = txn.currency
            elif txn.currency != currency:
                raise MixedCurrency(f"{currency} vs {txn.currency}")
            cents += int(txn.amount)
        if not page.has_more or not page.data:
            break
        kwargs["starting_after"] = page.data[-1].id
    return cents, currency or "usd"


# --- Secrets Manager (BYO-key rail) ---------------------------------------


def secret_name(owner_id: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/user-keys/{owner_id}/stripe-launch"


def _sm():
    return boto3.client("secretsmanager", region_name=getattr(settings, "AWS_REGION", "us-east-1"))


async def store_key(owner_id: str, key: str) -> None:
    sm = _sm()
    await run_in_thread(create_or_update_secret, sm, secret_name(owner_id), key)


async def load_key(owner_id: str) -> str | None:
    sm = _sm()
    try:
        resp = await run_in_thread(sm.get_secret_value, SecretId=secret_name(owner_id))
    except sm.exceptions.ResourceNotFoundException:
        return None
    return resp.get("SecretString")


async def delete_key(owner_id: str) -> None:
    sm = _sm()
    try:
        await run_in_thread(sm.delete_secret, SecretId=secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        return
