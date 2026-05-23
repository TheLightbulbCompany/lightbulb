"""Billing account repository -- DynamoDB operations for the billing_accounts table."""

import logging
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

logger = logging.getLogger(__name__)


class AlreadyExistsError(Exception):
    """Raised when a conditional put fails because the item already exists."""


def _get_table():
    return get_table("billing-accounts")


async def get_by_owner_id(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id})
    return response.get("Item")


async def get_by_stripe_customer_id(stripe_customer_id: str) -> dict | None:
    """Return the first billing row for ``stripe_customer_id``.

    Stripe customers are now keyed by email and can be shared across
    multiple billing rows (one human → personal + org rows reusing the same
    Stripe customer). For ambiguous cases prefer
    :func:`list_by_stripe_customer_id` so callers can disambiguate
    explicitly.
    """
    items = await list_by_stripe_customer_id(stripe_customer_id)
    return items[0] if items else None


async def list_by_stripe_customer_id(stripe_customer_id: str) -> list[dict]:
    """Return all billing rows pointing at ``stripe_customer_id``.

    Used by the Stripe webhook owner-resolver to disambiguate when a
    customer is shared across personal + org rows: the resolver filters
    the returned rows by ``stripe_subscription_id`` from the event.
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="stripe-customer-index",
        KeyConditionExpression=Key("stripe_customer_id").eq(stripe_customer_id),
    )
    return response.get("Items", [])


async def create_if_not_exists(
    owner_id: str,
    stripe_customer_id: str,
    owner_type: str = "personal",
) -> dict:
    """Atomically create a billing account if one doesn't exist for this owner.

    Uses a DynamoDB conditional put (``attribute_not_exists(owner_id)``)
    so that concurrent calls are serialized: exactly one wins, the rest
    raise ``AlreadyExistsError``. This is the single source of truth for
    preventing duplicate Stripe customers — Stripe's search API is
    eventually consistent and can't be trusted for dedup.
    """
    table = _get_table()
    now = utc_now_iso()
    item = {
        "owner_id": owner_id,
        "owner_type": owner_type,
        "id": str(uuid.uuid4()),
        "stripe_customer_id": stripe_customer_id,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(owner_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyExistsError(owner_id) from e
        raise
    return item


async def set_subscription(
    *,
    owner_id: str,
    subscription_id: str | None,
    status: str,
    trial_end: int | None = None,
) -> dict | None:
    """Persist Stripe subscription identity + status onto the billing account.

    Used on trial signup (Plan 3 §7.1) + every subscription state change
    via the customer.subscription.updated/deleted webhook. Records
    ``subscription_id`` (or ``None`` on cancellation) + ``status`` + the
    optional ``trial_end`` epoch so the rest of the system can read state
    without re-querying Stripe.
    """
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    existing["stripe_subscription_id"] = subscription_id
    existing["subscription_status"] = status
    # Always overwrite trial_end so a cancellation (trial_end=None) actually
    # clears the field (otherwise a stale trial countdown lingers after
    # subscription.deleted).
    if trial_end is not None:
        existing["trial_end"] = trial_end
    else:
        existing.pop("trial_end", None)
    existing["updated_at"] = utc_now_iso()

    table = _get_table()
    await run_in_thread(table.put_item, Item=existing)
    return existing


_VALID_PROVIDER_CHOICES = frozenset({"bedrock_claude", "byo_key", "chatgpt_oauth"})


async def set_provider_choice(
    owner_id: str,
    *,
    provider_choice: str,
    byo_provider: str | None,
    owner_type: str,
) -> dict:
    """Persist the provider choice on a billing row.

    Args:
        owner_id: org_id or personal user_id (the billing row's PK).
        provider_choice: one of ``bedrock_claude``, ``byo_key``, ``chatgpt_oauth``.
        byo_provider: optional even when ``provider_choice == "byo_key"`` —
            the picker submits ``byo_key`` alone at /trial-checkout time, and
            the BYO wizard fills in ``byo_provider`` (openai vs anthropic) +
            the actual API key in a later step. The
            row's ``byo_provider`` attribute is REMOVE'd whenever it's
            ``None`` so a switch away from byo_key, or a re-pick of byo_key
            without a chosen provider yet, doesn't leave stale data.
        owner_type: ``"personal"`` or ``"org"``. Used for the org invariant.

    Raises:
        ValueError: unknown provider_choice, or chatgpt_oauth on an org row.
    """
    if provider_choice not in _VALID_PROVIDER_CHOICES:
        raise ValueError(f"unknown provider_choice: {provider_choice!r}")
    if owner_type == "org" and provider_choice == "chatgpt_oauth":
        # ChatGPT OAuth is personal-only — orgs use Bedrock or BYO API key
        # (OpenAI Plus terms forbid reselling).
        raise ValueError(
            "chatgpt_oauth is not allowed for org owners; orgs must use bedrock_claude or byo_key",
        )

    now = utc_now_iso()
    table = _get_table()
    if provider_choice == "byo_key" and byo_provider is not None:
        update_expr = "SET provider_choice = :pc, byo_provider = :bp, updated_at = :t"
        values: dict = {":pc": provider_choice, ":bp": byo_provider, ":t": now}
    else:
        # provider_choice != byo_key, OR byo_key without a chosen byo_provider
        # yet. Drop any prior byo_provider so a switch away from byo_key, or a
        # re-pick of byo_key before the BYO wizard fills in the provider,
        # doesn't leak a stale value onto the row. DDB also rejects SETting
        # an attribute to None — REMOVE is the correct verb here.
        update_expr = "SET provider_choice = :pc, updated_at = :t REMOVE byo_provider"
        values = {":pc": provider_choice, ":t": now}

    response = await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


async def clear_provider_choice(owner_id: str) -> None:
    """Remove provider_choice and byo_provider from a billing row.

    No-op when no billing row exists — DynamoDB ``update_item`` would
    otherwise upsert and create a phantom ``{owner_id, updated_at}`` row
    with no ``stripe_customer_id``. That phantom row would then block
    ``/billing/trial-checkout`` from creating a Stripe customer (the
    ``if not account`` branch sees the phantom and skips customer
    creation).
    """
    now = utc_now_iso()
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE provider_choice, byo_provider SET updated_at = :t",
            ExpressionAttributeValues={":t": now},
            ConditionExpression="attribute_exists(owner_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # No billing row to clear — disconnect should be idempotent
            # even when the user never had a billing row.
            return
        raise


async def delete(owner_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"owner_id": owner_id})
