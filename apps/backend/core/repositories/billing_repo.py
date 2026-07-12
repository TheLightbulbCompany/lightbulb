"""Billing account repository -- DynamoDB operations for the billing_accounts table."""

import logging
import time
import uuid
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
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


async def get_by_metronome_customer_id(metronome_customer_id: str) -> dict | None:
    """Resolve the billing row from a Metronome customer UUID.

    Used by the Metronome webhook, whose payloads identify the customer by
    the Metronome UUID — not our owner_id. A paginated scan with a filter is
    acceptable here: webhook volume is one event per threshold crossing, far
    off any hot path, and the table carries no GSI on this attribute.
    """
    table = _get_table()
    kwargs: dict = {
        "FilterExpression": Attr("metronome_customer_id").eq(metronome_customer_id),
    }
    while True:
        response = await run_in_thread(table.scan, **kwargs)
        items = response.get("Items", [])
        if items:
            return items[0]
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return None
        kwargs["ExclusiveStartKey"] = last_key


async def set_metronome_ids(
    owner_id: str,
    *,
    metronome_customer_id: str,
    metronome_contract_id: str,
) -> None:
    """Persist the Metronome customer UUID + per-customer contract id on the
    billing row.

    Written once at provision time; ``ensure_metronome_customer`` keys its
    short-circuit on these attributes, so the write must land on an EXISTING
    row — an unconditional update would upsert a phantom
    ``{owner_id, metronome_customer_id}`` row with no stripe_customer_id
    (same trap documented on ``clear_provider_choice``).
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET metronome_customer_id = :m, metronome_contract_id = :c, updated_at = :t",
        ExpressionAttributeValues={
            ":m": metronome_customer_id,
            ":c": metronome_contract_id,
            ":t": utc_now_iso(),
        },
        ConditionExpression="attribute_exists(owner_id)",
    )


async def set_metronome_customer_id(
    owner_id: str,
    *,
    metronome_customer_id: str,
) -> None:
    """Persist ONLY the Metronome customer UUID — the partial-provision
    state where the Stripe billing-provider link failed and contract
    creation was deferred. Leaving metronome_contract_id absent keeps
    ``ensure_metronome_customer`` re-entering on every status poll until
    the link lands and the contract (with billing config) can be created.
    Same existing-row condition as ``set_metronome_ids``.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET metronome_customer_id = :m, updated_at = :t",
        ExpressionAttributeValues={
            ":m": metronome_customer_id,
            ":t": utc_now_iso(),
        },
        ConditionExpression="attribute_exists(owner_id)",
    )


async def set_stripe_customer_id(
    owner_id: str,
    *,
    stripe_customer_id: str,
) -> None:
    """Backfill a Stripe customer id onto an EXISTING billing row.

    The only row that reaches this today is the card-less ``trial_local``
    row (``create_trial_local_if_absent`` deliberately writes no
    ``stripe_*`` fields so a brand-new signup can chat before any Stripe
    customer exists). ``billing_service.create_customer_for_owner`` calls
    this when it finds a row that exists but has no ``stripe_customer_id`` —
    without this backfill, ``create_flat_fee_checkout`` always sees a
    Stripe-customer-less account and raises, and every free-tier user hits
    an HTTP 400 on Upgrade. Same existing-row condition as the other
    billing-row setters (``set_metronome_ids``, ``set_metronome_customer_id``,
    ``set_credits_auto_charge``) — an unconditional update would upsert a
    phantom ``{owner_id, stripe_customer_id}`` row for a caller that got the
    owner_id wrong, silently orphaning it from the rest of the billing state.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET stripe_customer_id = :c, updated_at = :t",
        ExpressionAttributeValues={
            ":c": stripe_customer_id,
            ":t": utc_now_iso(),
        },
        ConditionExpression="attribute_exists(owner_id)",
    )


async def set_credits_auto_charge(owner_id: str, *, enabled: bool) -> None:
    """Persist the owner's one-click top-up consent.

    True is set ONLY by the checkout webhook when the owner ticked the
    opt-in box on a real Stripe Checkout — that checkout carries the
    ``setup_future_usage=off_session`` consent artifact Stripe requires
    for later card-on-file charges, so the flag must never be flipped on
    through any path that lacks it (the settings surface may only turn it
    OFF). Same existing-row condition as the other billing-row setters.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET credits_auto_charge = :e, updated_at = :t",
        ExpressionAttributeValues={
            ":e": enabled,
            ":t": utc_now_iso(),
        },
        ConditionExpression="attribute_exists(owner_id)",
    )


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


_TRIAL_LOCAL_DURATION_SECONDS = 3 * 24 * 60 * 60


async def create_trial_local_if_absent(owner_id: str, owner_type: str) -> bool:
    """Atomically create a card-less local trial billing row if one doesn't
    exist for this owner.

    Uses the same conditional put (``attribute_not_exists(owner_id)``) as
    ``create_if_not_exists`` so concurrent callers are serialized: exactly
    one write lands, and any other caller (or a pre-existing row of any
    status) silently no-ops rather than overwriting. No ``stripe_*`` fields
    are written -- this row exists purely so a brand-new signup can chat
    before any Stripe customer/subscription is created.

    Returns ``True`` iff THIS call actually created the row (the "create
    branch" callers key a one-time side effect off, e.g. the ``trial_started``
    product event), ``False`` on every no-op path.
    """
    table = _get_table()
    now = utc_now_iso()
    item = {
        "owner_id": owner_id,
        "owner_type": owner_type,
        "subscription_status": "trial_local",
        "trial_end": int(time.time()) + _TRIAL_LOCAL_DURATION_SECONDS,
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
            return False
        raise
    return True


async def set_subscription(
    *,
    owner_id: str,
    subscription_id: str | None,
    status: str,
    trial_end: int | None = None,
    clear_seat_fields: bool = False,
) -> dict | None:
    """Persist Stripe subscription identity + status onto the billing account.

    Used on trial signup (Plan 3 §7.1) + every subscription state change
    via the customer.subscription.updated/deleted webhook. Records
    ``subscription_id`` (or ``None`` on cancellation) + ``status`` + the
    optional ``trial_end`` epoch so the rest of the system can read state
    without re-querying Stripe.

    ``clear_seat_fields`` (default False, opt-in only) also removes
    ``subscription_item_id`` + ``seat_count`` from the row. Cancellation
    passes this so a stale ``subscription_item_id`` doesn't linger and make
    ``is_seat_subscription`` (routers/billing.py) keep reporting a per-seat
    subscription for an org that no longer has one -- e.g. after it's
    re-comped onto a flat price, whose webhook never touches
    ``subscription_item_id``. The created/updated webhook path
    (``handle_subscription_state``) leaves this False since it calls
    ``set_team_subscription`` separately/afterward to (re)populate seat
    fields for live per-seat subs.
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
    if clear_seat_fields:
        existing.pop("subscription_item_id", None)
        existing.pop("seat_count", None)
    existing["updated_at"] = utc_now_iso()

    table = _get_table()
    await run_in_thread(table.put_item, Item=existing)
    return existing


async def set_team_subscription(
    owner_id: str,
    *,
    subscription_id: str | None,
    subscription_item_id: str,
    status: str,
    seat_count: int,
) -> dict | None:
    """Persist Teams per-seat subscription identity + seat count.

    Mirrors ``set_subscription``'s read-then-put: reads the existing row and
    re-puts it with the seat fields added. Returns None (no write) when no
    billing row exists, so a stray team webhook can never upsert a phantom
    ``{owner_id, ...}`` row with no stripe_customer_id.
    """
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    existing["stripe_subscription_id"] = subscription_id
    existing["subscription_status"] = status
    existing["subscription_item_id"] = subscription_item_id
    existing["seat_count"] = seat_count
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
    connector_user_id: str | None = None,
) -> dict:
    """Persist the provider choice on a billing row.

    Args:
        owner_id: org_id or personal user_id (the billing row's PK).
        provider_choice: one of ``bedrock_claude``, ``byo_key``, ``chatgpt_oauth``.
        byo_provider: optional even when ``provider_choice == "byo_key"`` —
            the picker submits ``byo_key`` alone at /trial-checkout time, and
            the BYO wizard fills in ``byo_provider`` (openai vs anthropic) +
            the actual API key in a later step. REMOVE'd whenever ``None``.
        connector_user_id: the Clerk user whose ChatGPT OAuth tokens back this
            owner's container (the workspace owner). Persisted only for
            ``chatgpt_oauth``; for other providers any prior value is
            REMOVE'd. ``None`` on a chatgpt_oauth write (e.g. the webhook
            backup, which may not carry it) leaves an existing connector
            untouched so the backup can never strip the value the synchronous
            trial-checkout/change-provider write set.

    Raises:
        ValueError: unknown provider_choice.
    """
    if provider_choice not in _VALID_PROVIDER_CHOICES:
        raise ValueError(f"unknown provider_choice: {provider_choice!r}")

    now = utc_now_iso()
    table = _get_table()
    sets = ["provider_choice = :pc", "updated_at = :t"]
    removes: list[str] = []
    values: dict = {":pc": provider_choice, ":t": now}

    if provider_choice == "byo_key" and byo_provider is not None:
        sets.append("byo_provider = :bp")
        values[":bp"] = byo_provider
    else:
        # DDB rejects SETting None — REMOVE is the correct verb. Dropping the
        # stale value covers both "switched away from byo_key" and "re-picked
        # byo_key before the wizard chose a provider".
        removes.append("byo_provider")

    if provider_choice == "chatgpt_oauth":
        if connector_user_id is not None:
            sets.append("oauth_connector_user_id = :cu")
            values[":cu"] = connector_user_id
        # connector_user_id None: leave any existing connector in place.
    else:
        removes.append("oauth_connector_user_id")

    update_expr = "SET " + ", ".join(sets)
    if removes:
        update_expr += " REMOVE " + ", ".join(removes)

    response = await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


async def claim_oauth_connector(owner_id: str, *, connector_user_id: str) -> None:
    """Record the org OWNER as the ChatGPT connector — set-once.

    The connector is the workspace owner (one ChatGPT account ↔ one
    container) and is immutable once recorded: the condition refuses to
    overwrite an existing value, so a later sign-in by a different admin
    can never repoint the org. The no-overwrite failure and the
    missing-row failure (same phantom-row hazard as clear_provider_choice)
    are both silent no-ops."""
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET oauth_connector_user_id = :cu, updated_at = :t",
            ExpressionAttributeValues={":cu": connector_user_id, ":t": utc_now_iso()},
            ConditionExpression=("attribute_exists(owner_id) AND attribute_not_exists(oauth_connector_user_id)"),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise


async def clear_provider_choice(owner_id: str) -> None:
    """Remove provider_choice, byo_provider, and oauth_connector_user_id from
    a billing row.

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
            UpdateExpression="REMOVE provider_choice, byo_provider, oauth_connector_user_id SET updated_at = :t",
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


async def add_meta_daily_spend(owner_id: str, date: str, delta_microcents: int) -> int:
    """Atomically add ``delta_microcents`` to the free-tier daily-spend
    counter for ``date`` (a ``YYYY-MM-DD`` UTC date key) and return the NEW
    total for that date.

    Backs the free-tier $3/day cost cap read by
    ``core/services/trial_cap.trial_block_reason``. The **Bedrock
    invocation-log meter Lambda is the sole writer** of this counter — it
    prices every model invocation at the AWS layer and ADDs it here. No
    in-band backend path writes it; two writers would double-count.

    The attribute name (``meta_daily_spend_microcents``) is a historical
    holdover from the Meta free tier and is kept deliberately — live rows
    carry it. It is now simply the trial daily-spend counter.

    The increment is a nested-path ``SET`` with ``if_not_exists`` rather than
    an ``ADD``. Both forms are accepted by a real DynamoDB engine on a nested
    leaf (verified against DynamoDB Local, which is AWS's own engine — the
    "ADD is top-level-only" folklore is not in the API reference, whose sole
    stated ADD restriction is the Number/set data type), but AWS explicitly
    recommends ``SET`` over ``ADD``, and this counter is the trial spend
    fence: if it silently fails to apply, nothing stops the burn. Prefer the
    construct that needs no argument to defend.

    A nested ``SET`` still requires the parent map to already exist, and a
    single combined expression (``SET meta_daily_spend_microcents =
    if_not_exists(...), meta_daily_spend_microcents.#d = ...``) is REJECTED —
    ``ValidationException: Two document paths overlap with each other`` — so
    this stays a guarded two-step: (1) initialize the map to ``{}`` only if
    the row exists and the map is absent (a no-op, swallowed
    ConditionalCheckFailedException, if another caller won the init race),
    then (2) the atomic increment on the nested date leaf, still guarded by
    ``attribute_exists(owner_id)`` so a missing row raises on step 2 either
    way. No phantom rows — same invariant as the other billing-row setters.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET meta_daily_spend_microcents = :empty",
            ExpressionAttributeValues={":empty": {}},
            ConditionExpression=("attribute_exists(owner_id) AND attribute_not_exists(meta_daily_spend_microcents)"),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Map already exists, or the owner row doesn't exist -- the ADD
        # below re-checks attribute_exists(owner_id) and raises the same
        # error in the no-row case.

    response = await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=(
            "SET meta_daily_spend_microcents.#d = if_not_exists(meta_daily_spend_microcents.#d, :zero) + :delta"
        ),
        ExpressionAttributeNames={"#d": date},
        ExpressionAttributeValues={
            ":delta": Decimal(str(int(delta_microcents))),
            ":zero": Decimal(0),
        },
        ConditionExpression="attribute_exists(owner_id)",
        ReturnValues="ALL_NEW",
    )
    attrs = response.get("Attributes", {})
    return int(attrs.get("meta_daily_spend_microcents", {}).get(date, 0))


async def get_meta_daily_spend(owner_id: str, date: str) -> int:
    """Read-only lookup of the free-tier daily-spend counter for ``date``.
    Returns 0 when the owner has no billing row, or the row exists but has no
    spend recorded for that date."""
    item = await get_by_owner_id(owner_id)
    if not item:
        return 0
    return int(item.get("meta_daily_spend_microcents", {}).get(date, 0))
