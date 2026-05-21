"""Credit ledger — owner-scoped prepaid balance + immutable transaction log.

Backed by two DDB tables: `credits` (single row per owner, atomic counter)
and `credit-transactions` (immutable audit log, PK owner_id + SK tx_id).
Per spec §6. Card 3 only — cards 1 and 2 don't touch this module.

The DDB attribute name is still `user_id` for backward-compat, but the
value we write is the **owner_id** (org_id for org context, user_id for
personal). Org members all draw from one pooled balance keyed on the
org's owner_id. Renaming the DDB attribute is a separate migration.

Concurrency:
- Top-up: atomic ADD on balance_microcents (cannot overflow on writes).
- Deduct: atomic ADD with negative + ConditionExpression that the result
  stays non-negative. If the condition fails (race with another chat),
  we accept the small overdraft per spec §6.3 step 6 and force balance
  to zero with an unconditional SET — better UX than refunding a chat.
- Get balance: eventually-consistent read by default; the caller can
  pass consistent=True if the freshness matters (the pre-chat hard-stop
  check sets consistent=True so a top-up that just landed via webhook
  unblocks the next message immediately).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from core.config import settings


logger = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    """Reserved for callers that want to fail-closed instead of overdraft.

    NOT raised by deduct() under normal use — deduct() accepts the
    overdraft per spec §6.3. Provided for use cases like the pre-chat
    hard-stop check.
    """


def _credits_table():
    table_name = os.environ.get("CREDITS_TABLE") or settings.CREDITS_TABLE
    if not table_name:
        raise RuntimeError("CREDITS_TABLE is empty — backend is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(table_name)


def _txns_table():
    table_name = os.environ.get("CREDIT_TRANSACTIONS_TABLE") or settings.CREDIT_TRANSACTIONS_TABLE
    if not table_name:
        raise RuntimeError("CREDIT_TRANSACTIONS_TABLE is empty — backend is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(table_name)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_tx_id() -> str:
    # Time-prefixed so the SK sorts chronologically.
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _put_txn(item: dict) -> None:
    """Write a transaction row with collision-retry. Audit log is immutable;
    we never silently overwrite. Two collisions in a row → raise."""
    for attempt in range(2):
        try:
            _txns_table().put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(tx_id)",
            )
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            if attempt == 0:
                # Regenerate tx_id and retry once.
                item["tx_id"] = _new_tx_id()
                continue
            raise
    raise RuntimeError("credit_ledger._put_txn: 2 tx_id collisions in a row")


async def get_balance(owner_id: str, *, consistent: bool = False) -> int:
    """Returns balance in microcents. 0 if the owner has no row yet."""
    resp = _credits_table().get_item(Key={"user_id": owner_id}, ConsistentRead=consistent)
    item = resp.get("Item")
    if not item:
        return 0
    return int(item.get("balance_microcents", 0))


async def top_up(
    owner_id: str,
    *,
    amount_microcents: int,
    stripe_payment_intent_id: str,
) -> int:
    """Add credits to an owner's balance. Returns the new balance.

    Idempotent on stripe_payment_intent_id at the webhook layer (handler
    dedupes by event.id via Plan 1's webhook_dedup helper). This function
    itself is NOT idempotent — calling twice will credit twice.
    """
    if amount_microcents <= 0:
        raise ValueError(f"amount_microcents must be positive, got {amount_microcents}")

    resp = _credits_table().update_item(
        Key={"user_id": owner_id},
        UpdateExpression="ADD balance_microcents :amt SET updated_at = :now, last_top_up_at = :now",
        ExpressionAttributeValues={
            ":amt": amount_microcents,
            ":now": _now_iso(),
        },
        ReturnValues="UPDATED_NEW",
    )
    new_balance = int(resp["Attributes"]["balance_microcents"])

    _put_txn(
        {
            "user_id": owner_id,
            "tx_id": _new_tx_id(),
            "type": "top_up",
            "amount_microcents": amount_microcents,
            "balance_after_microcents": new_balance,
            "stripe_payment_intent_id": stripe_payment_intent_id,
            "created_at": _now_iso(),
        }
    )
    return new_balance


async def deduct(
    owner_id: str,
    *,
    amount_microcents: int,
    chat_session_id: str,
    raw_cost_microcents: int,
    markup_multiplier: float,
    bedrock_invocation_id: str | None = None,
) -> int:
    """Deduct credits for one chat. Returns the new balance.

    Per spec §6.3: tries an atomic conditional decrement; on race-induced
    overdraft, falls back to setting balance=0 and logs a warning.
    """
    if amount_microcents <= 0:
        raise ValueError(f"amount_microcents must be positive, got {amount_microcents}")

    try:
        resp = _credits_table().update_item(
            Key={"user_id": owner_id},
            UpdateExpression="ADD balance_microcents :neg SET updated_at = :now",
            ConditionExpression="balance_microcents >= :amt",
            ExpressionAttributeValues={
                ":neg": -amount_microcents,
                ":amt": amount_microcents,
                ":now": _now_iso(),
            },
            ReturnValues="UPDATED_NEW",
        )
        new_balance = int(resp["Attributes"]["balance_microcents"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Race-induced overdraft: chat already completed, can't refund.
        # Decrement by the deduct amount with a guard that the balance is
        # still negative — this races correctly against a concurrent top-up
        # that lands between the failed conditional decrement and this
        # write. Without the guard we'd zero the balance and erase the
        # top-up entirely.
        logger.warning(
            "Credit overdraft for owner_id=%s session=%s amount=%d — applying overdraft deduct",
            owner_id,
            chat_session_id,
            amount_microcents,
        )
        try:
            resp = _credits_table().update_item(
                Key={"user_id": owner_id},
                UpdateExpression=("ADD balance_microcents :neg SET updated_at = :now"),
                # Only apply the overdraft deduct if the balance is still
                # below the requested amount. If a top-up arrived first and
                # the balance is now sufficient, do nothing — the chat is
                # already over, the top-up should be retained.
                ConditionExpression="balance_microcents < :amt",
                ExpressionAttributeValues={
                    ":neg": -amount_microcents,
                    ":amt": amount_microcents,
                    ":now": _now_iso(),
                },
                ReturnValues="UPDATED_NEW",
            )
            new_balance = int(resp["Attributes"]["balance_microcents"])
        except ClientError as e2:
            if e2.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Top-up already restored a positive balance — read it and use
            # that for the audit row instead of writing a zero we'd regret.
            current = await get_balance(owner_id)
            new_balance = current

    txn_item = {
        "user_id": owner_id,
        "tx_id": _new_tx_id(),
        "type": "deduct",
        "amount_microcents": -amount_microcents,
        "balance_after_microcents": new_balance,
        "chat_session_id": chat_session_id,
        "raw_cost_microcents": raw_cost_microcents,
        "markup_multiplier": Decimal(str(markup_multiplier)),
        "created_at": _now_iso(),
    }
    if bedrock_invocation_id:
        txn_item["bedrock_invocation_id"] = bedrock_invocation_id
    _put_txn(txn_item)
    return new_balance


async def adjustment(
    owner_id: str,
    *,
    amount_microcents: int,
    reason: str,
    operator: str,
) -> int:
    """Operator-only manual adjustment (e.g. refund, support credit).

    Positive amount adds, negative subtracts. Always succeeds; if subtracting
    would go negative, balance becomes 0 (consistent with deduct overdraft).
    """
    new_balance = max(0, await get_balance(owner_id, consistent=True) + amount_microcents)
    _credits_table().update_item(
        Key={"user_id": owner_id},
        UpdateExpression="SET balance_microcents = :bal, updated_at = :now",
        ExpressionAttributeValues={":bal": new_balance, ":now": _now_iso()},
    )
    _put_txn(
        {
            "user_id": owner_id,
            "tx_id": _new_tx_id(),
            "type": "adjustment",
            "amount_microcents": amount_microcents,
            "balance_after_microcents": new_balance,
            "reason": reason,
            "operator": operator,
            "created_at": _now_iso(),
        }
    )
    return new_balance


async def set_auto_reload(
    owner_id: str,
    *,
    enabled: bool,
    threshold_cents: int | None = None,
    amount_cents: int | None = None,
) -> None:
    """Configure auto-reload. When enabled, threshold and amount are required.

    Always writes all four fields in a single SET (we use 0 as the sentinel
    when a value isn't supplied while disabling — should_auto_reload only
    looks at threshold when auto_reload_enabled is true, so the sentinel is
    inert).
    """
    if enabled and (threshold_cents is None or amount_cents is None):
        raise ValueError("threshold_cents and amount_cents required when enabling")

    _credits_table().update_item(
        Key={"user_id": owner_id},
        UpdateExpression=(
            "SET auto_reload_enabled = :en, "
            "auto_reload_threshold_cents = :th, "
            "auto_reload_amount_cents = :am, "
            "updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":en": enabled,
            ":th": threshold_cents if threshold_cents is not None else 0,
            ":am": amount_cents if amount_cents is not None else 0,
            ":now": _now_iso(),
        },
    )


async def should_auto_reload(owner_id: str) -> bool:
    """True iff auto-reload is enabled and balance < threshold."""
    resp = _credits_table().get_item(Key={"user_id": owner_id}, ConsistentRead=True)
    item = resp.get("Item")
    if not item or not item.get("auto_reload_enabled"):
        return False
    threshold_cents = int(item.get("auto_reload_threshold_cents", 0))
    threshold_microcents = threshold_cents * 10_000  # 1 cent = 10_000 microcents
    balance = int(item.get("balance_microcents", 0))
    return balance < threshold_microcents
