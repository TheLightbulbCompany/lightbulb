"""Billing account repository -- DynamoDB operations for the billing_accounts table."""

import logging
import time
from datetime import datetime
import uuid

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)


class AlreadyExistsError(Exception):
    """Raised when a conditional put fails because the item already exists."""


def _get_table():
    return get_table("billing-accounts")


async def get_by_owner_id(owner_id: str, *, consistent_read: bool = False) -> dict | None:
    """``consistent_read=True`` forces a strongly consistent get_item — for
    read-back-after-write callers (e.g. the owner-facts writer) where an
    eventually consistent read could observe the pre-write row."""
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id}, ConsistentRead=consistent_read)
    return _read_view(response.get("Item"))


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
    return [_read_view(item) for item in response.get("Items", [])]


async def scan_all() -> list[dict]:
    """Every billing row, paginated via LastEvaluatedKey.

    Fleet aggregates only (admin growth read). Not for hot paths — a DDB
    Scan is the whole table every call. Mirrors ``container_repo.scan_all``.
    """
    table = _get_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(_read_view(item) for item in page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


async def get_by_apple_original_transaction_id(original_transaction_id: str) -> dict | None:
    """Resolve the billing row an Apple auto-renewable subscription belongs to.

    App Store Server Notifications identify a subscription only by its
    ``originalTransactionId`` — they carry no Isol8 owner id — so this is how a
    renewal, expiry, or refund finds the account to update. The mapping is
    written by :func:`set_apple_subscription` when the purchase is first linked.

    The backing GSI is SPARSE: ``apple_original_transaction_id`` is written only
    for rows that actually have an Apple subscription, and never as an empty
    string, because an empty value on a GSI key attribute makes DynamoDB reject
    the ENTIRE PutItem — which, on this table's read-then-put writers, would
    brick every unrelated billing write for that owner.
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="apple-original-transaction-index",
        KeyConditionExpression=Key("apple_original_transaction_id").eq(original_transaction_id),
    )
    items = response.get("Items", [])
    return _read_view(items[0]) if items else None


def apple_lapsed(row: dict) -> bool:
    """Apple says "active" but its paid-through date (``apple_expires_at``, ms) has passed.

    Nothing re-evaluates entitlement on a timer: the shared status only moves
    when a notification arrives. A sandbox purchase verified by the production
    backend (App Review, TestFlight — see ``apple_iap._verify_signed``) sends
    its lifecycle notifications to the SANDBOX endpoint, not ours, so without
    this check its grant would never expire. A billing grace period stores the
    grace deadline as the expiry, so grace is honoured to its end.
    """
    expires = row.get("apple_expires_at")
    return (
        row.get("apple_subscription_status") == "active"
        and expires is not None
        and int(expires) <= int(time.time() * 1000)
    )


def _read_view(row: dict | None) -> dict | None:
    """Re-derive the shared status on read when an Apple grant has lapsed.

    Only the DERIVED fields change — ``apple_subscription_status`` itself is
    left as stored, because every setter's cross-provider compare-and-set
    compares it against the table.
    """
    if row and apple_lapsed(row):
        status, source = _effective_status(row)
        row["subscription_status"] = status
        if source:
            row["subscription_source"] = source
    return row


# Ranked best-to-worst. The effective status is the BEST any provider reports,
# because an owner paying either Apple or Stripe is entitled — the question the
# gates ask is "may this owner use the platform", and one live subscription is
# a yes regardless of what the other provider says.
_STATUS_RANK = ("active", "trialing", "trial_local", "past_due", "canceled")


def _adopt_legacy_status(row: dict) -> None:
    """Claim a pre-IAP row's shared status for the Stripe side, once.

    Every row written before Apple IAP existed carries only the shared
    ``subscription_status`` — nothing ever set ``stripe_subscription_status``,
    because the field did not exist. Left alone, the first Apple write makes
    :func:`_effective_status` see exactly one candidate (Apple's) and skip the
    fallback, so an Apple expiry, refund, or restored-but-expired transaction
    would set an actively-paying legacy Stripe owner to "canceled" and cut off
    a customer who is still being charged.

    Gated on the row having NO Apple state yet, which is the only condition
    under which the shared field is guaranteed to be purely non-Apple. Gating
    instead on "stripe_subscription_status is missing" is not enough and was the
    first attempt: a row that has only ever paid Apple never gets that field, so
    every subsequent Apple write would copy the DERIVED (Apple-sourced) value
    into the Stripe slot, and the owner would keep access forever after
    canceling. Called before Apple's status joins the row, so "no Apple state"
    means no PRIOR Apple state.
    """
    if row.get("apple_subscription_status"):
        return
    existing = row.get("subscription_status")
    # "trial_local" is NOT a Stripe status. It is written by
    # create_trial_local_if_absent for a card-less free tier with no Stripe
    # object behind it at all, and it is the status of the row in the ORDINARY
    # first-Apple-purchase flow — so adopting it invents a phantom Stripe trial
    # on almost every IAP customer. _STATUS_RANK puts trial_local above
    # "canceled", so that phantom then outranks Apple's own expiry: the shared
    # status stays trial_local, teardown_if_unentitled reads it as still
    # entitled and skips cancellation entirely, and the billing surfaces show a
    # lapsed subscriber as being back on the free plan.
    if existing and existing != "trial_local" and not row.get("stripe_subscription_status"):
        row["stripe_subscription_status"] = existing


def _effective_status(row: dict, *, prefer: str | None = None) -> tuple[str | None, str | None]:
    """Combine per-provider statuses into ``(subscription_status, source)``.

    Exists because both providers used to write the shared field directly,
    making them last-writer-wins: an Apple expiry set the row to "canceled" for
    an owner whose Stripe subscription was still live and still charging them —
    ``is_subscription_active`` reads "canceled" as inactive, so a paying
    customer lost access. The inverse happened when a Stripe cancellation
    landed while Apple was active.

    The winning provider also names ``subscription_source``, which decides
    where the billing surfaces send someone to cancel. Deriving it here rather
    than pinning it at the Apple write is what stops an owner who has since
    moved to Stripe from being sent to iOS Settings, where their subscription
    does not exist and cannot be canceled.

    ``prefer`` breaks a tie toward the provider currently being written, so two
    simultaneously-active subscriptions resolve to the one the owner most
    recently acted on. Entitlement is identical either way — only the
    management destination differs.

    Falls back to the row's existing values when neither provider has recorded
    a status, so a card-less ``trial_local`` row (written by
    ``create_trial_local_if_absent``, not by any provider) is never clobbered.
    """
    candidates = [
        ("stripe", row.get("stripe_subscription_status")),
        ("apple_iap", "canceled" if apple_lapsed(row) else row.get("apple_subscription_status")),
    ]
    present = [(src, status) for src, status in candidates if status]
    if not present:
        return row.get("subscription_status"), row.get("subscription_source")
    if prefer:
        present.sort(key=lambda c: c[0] != prefer)
    for rank in _STATUS_RANK:
        for src, status in present:
            if status == rank:
                # "trial_local" is the card-less free tier — there is no
                # subscription to manage, so naming a provider for it would
                # point the manage button at a portal with nothing in it.
                return status, (None if status == "trial_local" else src)
    # An unranked provider value (a status neither map produces today) is still
    # better information than dropping it.
    return present[0][1], present[0][0]


def _apply_effective_status(row: dict, *, prefer: str) -> None:
    """Write the derived status + source onto ``row`` in place."""
    status, source = _effective_status(row, prefer=prefer)
    row["subscription_status"] = status
    if source:
        row["subscription_source"] = source


# Every setter here is a read-then-whole-item PutItem, so it derives the shared
# status from a snapshot that may be stale by the time it writes — and then
# replaces the WHOLE item, restoring the other provider's overwritten field
# along with it. Two providers writing at once is enough:
#
#   Stripe reads {stripe: active, apple: active}
#   Apple  reads {stripe: active, apple: active}
#   Apple  writes {apple: canceled, stripe: active}   -> derived "active"
#   Stripe writes {stripe: canceled, apple: active}   -> derived "active"
#
# Both subscriptions have ended and the row says active, with a phantom live
# provider that nothing will ever clear. Splitting the field per provider is
# what made this reachable: before it, the second write simply won.
#
# So each setter asserts the OTHER provider's status is still what it read, and
# retries against fresh state when it is not. Cheap because the conflict needs
# simultaneous dual-provider webhooks for one owner.
_CAS_ATTEMPTS = 4


def _unchanged_since_read(row: dict, fields: tuple[str, ...], values: dict) -> str:
    """Condition asserting every one of ``fields`` still holds the value read.

    Plural because a single field is not enough to detect that the other
    provider wrote. Conditioning the Stripe write on ``apple_subscription_status``
    alone passed straight through an Apple RENEWAL, which leaves the status
    "active" while advancing ``apple_last_signed_date`` and ``apple_expires_at``
    — so the whole-item PutItem then restored the pre-renewal watermark, and a
    later out-of-order refund could clear the monotonicity check it should have
    lost and cancel a subscription that had just renewed.
    """
    clauses = []
    for i, field in enumerate(fields):
        # ABSENT and PRESENT-BUT-NULL are different rows, and conflating them
        # breaks ordinary writes: set_subscription stores stripe_subscription_id
        # = None on cancellation, which DynamoDB keeps as a NULL attribute. Ask
        # attribute_not_exists about that and it is false forever, so every
        # later write fails its own condition and the retries exhaust.
        if field not in row:
            clauses.append(f"attribute_not_exists({field})")
        else:
            values[f":cas{i}"] = row[field]
            clauses.append(f"{field} = :cas{i}")
    return " AND ".join(clauses)


# Everything the OTHER provider owns on the row. A whole-item PutItem rewrites
# all of it, so a compare-and-set has to cover all of it — anything omitted is a
# field one provider can silently roll back to its own snapshot.
_APPLE_OWNED = ("apple_subscription_status", "apple_last_signed_date", "apple_expires_at")
_STRIPE_OWNED = ("stripe_subscription_status", "stripe_subscription_id")


class CrossProviderWriteConflict(Exception):
    """Lost the cross-provider compare-and-set too many times running.

    Raised rather than silently returning, because the callers are webhook
    handlers: Stripe and Apple both redeliver on a 5xx, so failing loudly gets
    the update applied later, while swallowing it drops a cancellation on the
    floor and leaves someone with access they stopped paying for.
    """


async def _write_changed_fields(
    owner_id: str,
    before: dict,
    after: dict,
    *,
    condition: str,
    values: dict,
) -> None:
    """Persist ONLY what ``after`` changed relative to ``before``.

    A whole-item ``PutItem`` rewrites every attribute, including ones this
    caller never looked at — so any field another writer changed between the
    read and the write is silently restored from the stale snapshot. Widening
    the compare-and-set to cover each such field in turn is whack-a-mole; three
    separate review findings landed that way (Stripe status, then the Apple
    watermark and expiry, then ``provider_choice``, which no billing writer
    owns at all and which reverting can send a paying owner back to
    ``provider_required``).

    Writing a diff removes the class instead of the instances: a field nobody
    touched is not in the expression, so nobody can roll it back. The
    cross-provider condition still rides along, because the DERIVED status in
    the diff was computed from the snapshot and is only valid if the other
    provider has not moved.

    Every attribute goes through ``ExpressionAttributeNames``. Several plausible
    column names here are DynamoDB reserved words, and a reserved word in a raw
    update expression fails the whole call.
    """
    sets: list[str] = []
    removes: list[str] = []
    names: dict[str, str] = {}
    update_values = dict(values)

    for key in set(before) | set(after):
        if key == "owner_id":
            continue  # the partition key is not updatable
        placeholder = f"#f{len(names)}"
        if key not in after:
            names[placeholder] = key
            removes.append(placeholder)
        elif key not in before or before[key] != after[key]:
            names[placeholder] = key
            update_values[f":v{len(update_values)}"] = after[key]
            sets.append(f"{placeholder} = :v{len(update_values) - 1}")

    if not sets and not removes:
        return

    expression = ""
    if sets:
        expression += "SET " + ", ".join(sets)
    if removes:
        expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)

    kwargs: dict = {
        "Key": {"owner_id": owner_id},
        "UpdateExpression": expression,
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names,
    }
    if update_values:
        kwargs["ExpressionAttributeValues"] = update_values
    await run_in_thread(_get_table().update_item, **kwargs)


async def _put_with_apple_cas(owner_id: str, mutate) -> dict | None:
    """Read-modify-write the billing row, asserting Apple's status held still.

    The Stripe half of the compare-and-set described above. ``mutate(row)``
    applies the Stripe-owned changes in place; everything about reading,
    conditioning, retrying and publishing is identical for both Stripe setters,
    so it lives here rather than being copied into each.

    Returns None only when there is no billing row — the existing "a stray
    webhook must not upsert a phantom row" contract both setters already had.
    """
    for attempt in range(_CAS_ATTEMPTS):
        existing = await get_by_owner_id(owner_id)
        if existing is None:
            return None

        # Read BEFORE mutate, so the condition describes the row we based the
        # derived status on rather than the one we are about to write.
        values: dict = {}
        condition = _unchanged_since_read(existing, _APPLE_OWNED, values)

        before = dict(existing)
        mutate(existing)
        existing["updated_at"] = utc_now_iso()

        try:
            await _write_changed_fields(owner_id, before, existing, condition=condition, values=values)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            logger.info(
                "billing_repo.stripe_cas_retry owner_id=%s attempt=%s — Apple wrote underneath us",
                owner_id,
                attempt + 1,
            )
            continue

        publish_status_changed(owner_id)
        return existing

    raise CrossProviderWriteConflict(f"stripe write for {owner_id} lost the cross-provider CAS {_CAS_ATTEMPTS} times")


async def set_apple_subscription(
    owner_id: str,
    *,
    original_transaction_id: str,
    status: str,
    expires_at: int | None,
    signed_date: int | None = None,
) -> dict | None:
    """Persist Apple IAP subscription identity + status onto the billing account.

    Apple's status is written to ``apple_subscription_status``; the shared
    ``subscription_status`` that every ``provision_gate`` predicate reads is
    DERIVED from both providers by :func:`_effective_status`. Apple used to
    write the shared field directly, which made the two providers
    last-writer-wins and let an Apple expiry cancel an owner who was still
    paying — and still being charged by — Stripe.

    ``subscription_source`` — which channel billing surfaces send the customer
    to for cancellation — is derived alongside the status, not pinned here. An
    Apple subscription can only be managed in iOS Settings and opening the
    Stripe portal for one is a dead end, but the reverse is worse: pinning the
    source at the Apple write stranded an owner who later moved to Stripe in
    iOS Settings, where there was nothing to cancel.

    ``stripe_subscription_id`` is deliberately left alone. An owner who once had
    a Stripe subscription and now pays through Apple must not have that history
    erased by a renewal notification.

    Returns None — meaning NOTHING WAS WRITTEN — in three cases, all of which
    callers must treat as "did not happen" rather than success:
      - no billing row exists (a notification for an owner we do not have),
      - the write lost the ``signedDate`` watermark race (an older notification
        arriving after a newer one),
      - the row is already bound to a DIFFERENT Apple transaction.
    The last two are enforced as conditions on the write itself, because the
    callers' reads come from an eventually-consistent GSI and cannot decide
    either race on their own.
    """
    if not original_transaction_id:
        # Never "" — it keys a GSI, and DynamoDB rejects the whole PutItem.
        raise ValueError("original_transaction_id must be non-empty (it keys a GSI)")

    for attempt in range(_CAS_ATTEMPTS):
        written = await _try_set_apple_subscription(
            owner_id,
            original_transaction_id=original_transaction_id,
            status=status,
            expires_at=expires_at,
            signed_date=signed_date,
        )
        if written is not _CAS_LOST:
            return written  # type: ignore[return-value]
        logger.info(
            "billing_repo.apple_cas_retry owner_id=%s attempt=%s — Stripe wrote underneath us",
            owner_id,
            attempt + 1,
        )
    raise CrossProviderWriteConflict(f"apple write for {owner_id} lost the cross-provider CAS {_CAS_ATTEMPTS} times")


#: Sentinel distinguishing "lost the cross-provider CAS, retry" from None,
#: which means "deliberately not applied" and must never be retried.
_CAS_LOST = object()


async def _try_set_apple_subscription(
    owner_id: str,
    *,
    original_transaction_id: str,
    status: str,
    expires_at: int | None,
    signed_date: int | None,
):
    """One attempt. Returns the row, None (do not retry), or ``_CAS_LOST``."""
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    # Captured from the row AS READ, before _adopt_legacy_status can write to
    # this very field — the condition is about the pre-write state.
    cas_values: dict = {}
    cross_provider = _unchanged_since_read(existing, _STRIPE_OWNED, cas_values)
    # Snapshot BEFORE any mutation, so the write below can be a diff rather than
    # a whole-item replace — see _write_changed_fields.
    before = dict(existing)

    existing["apple_original_transaction_id"] = original_transaction_id
    # Must run BEFORE Apple's status joins the row, while the shared field
    # still holds purely non-Apple state.
    _adopt_legacy_status(existing)
    # Apple's own status lives in its own field; the shared one and the
    # management source are DERIVED from both providers below. Writing
    # subscription_status directly here is what made an Apple expiry cancel a
    # live Stripe subscription.
    existing["apple_subscription_status"] = status
    # The expiry lands BEFORE the derivation: apple_lapsed reads it, and a stale
    # past expiry would derive "canceled" for a resubscribe that is active.
    if expires_at is not None:
        existing["apple_expires_at"] = expires_at
    else:
        existing.pop("apple_expires_at", None)
    _apply_effective_status(existing, prefer="apple_iap")
    # Watermark for the notification out-of-order guard (apple_iap). Only ever
    # advances — a caller with no signedDate (the link path) leaves it alone
    # rather than clearing it, or a purchase-link would reopen the window for
    # every already-applied notification to be replayed.
    if signed_date is not None:
        existing["apple_last_signed_date"] = signed_date
    existing["updated_at"] = utc_now_iso()

    # The watermark comparison is a CONDITION on the write, not a check the
    # caller makes first. Callers read the row through an eventually-consistent
    # GSI, so two overlapping notifications can both observe an old watermark
    # and both decide they are newest; the loser then writes last and restores
    # access that had legitimately ended. Evaluated here, against the item as it
    # actually is at write time, the older one is rejected however the reads
    # interleaved.
    #
    # Returns None on rejection — the same "nothing was written" signal the
    # missing-row case uses, which the caller already handles.
    conditions: list[str] = []
    values: dict = {}

    if signed_date is not None:
        conditions.append("(attribute_not_exists(apple_last_signed_date) OR apple_last_signed_date <= :sd)")
        values[":sd"] = signed_date

    # Ownership fence. The row must either have no Apple transaction yet or
    # already own THIS one. Without it, `link_purchase`'s cross-account check —
    # a query against an eventually-consistent, non-unique GSI followed by a
    # write to a different row — lets two accounts submitting the same JWS both
    # observe "unowned" and both go active on one subscription. The condition is
    # evaluated against the row at write time, so the second writer loses.
    #
    # This fences the row, which is the half that matters for the common race
    # (same transaction, two accounts, one of them wins). It does NOT make the
    # id globally unique across rows — DynamoDB cannot condition on a GSI — so
    # `link_purchase` still runs its lookup first to reject an id already owned
    # elsewhere. See the note there.
    conditions.append("(attribute_not_exists(apple_original_transaction_id) OR apple_original_transaction_id = :otid)")
    values[":otid"] = original_transaction_id

    # Cross-provider compare-and-set. Without it this whole-item write puts back
    # the Stripe status from our snapshot, silently resurrecting a subscription
    # Stripe cancelled a moment ago.
    conditions.append(cross_provider)
    values.update(cas_values)

    try:
        await _write_changed_fields(
            owner_id,
            before,
            existing,
            condition=" AND ".join(conditions),
            values=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # DynamoDB does not say WHICH condition failed, so re-read and ask our
        # own fences directly. If they still refuse, the write was deliberately
        # rejected and must NOT be retried — retrying a stale notification is
        # how an expiry gets undone. Anything else means only the cross-provider
        # value moved, which is exactly what a retry fixes.
        fresh = await get_by_owner_id(owner_id)
        if fresh is None:
            return None
        bound_to = fresh.get("apple_original_transaction_id")
        if bound_to is not None and bound_to != original_transaction_id:
            logger.info(
                "billing_repo.apple_write_rejected owner_id=%s — row bound to a different transaction",
                owner_id,
            )
            return None
        if signed_date is not None and (fresh.get("apple_last_signed_date") or 0) > signed_date:
            logger.info(
                "billing_repo.apple_write_stale owner_id=%s signed_date=%s — newer already applied",
                owner_id,
                signed_date,
            )
            return None
        return _CAS_LOST

    publish_status_changed(owner_id)
    return existing


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
            return _read_view(items[0])
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
    without this backfill, workspace Checkout always sees a
    Stripe-customer-less account and raises, and every free-tier user hits
    an HTTP 400 on Upgrade. The conditional claim also requires the field to
    be absent: two first checkouts can race on the same ``trial_local`` row,
    but only one may attach a customer.
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
        ConditionExpression="attribute_exists(owner_id) AND attribute_not_exists(stripe_customer_id)",
    )


async def set_pending_checkout(owner_id: str, *, session_id: str) -> None:
    """Record the owner's OPEN Stripe Checkout session (at most one).

    Written and read only by ``billing_service`` workspace Checkout, under the
    checkout claim, so a second click returns the SAME hosted session instead
    of minting a parallel one — completing two open sessions would create two
    charged subscriptions. Never cleared, only overwritten: the reader
    re-checks the session with Stripe and every outcome except "open, ours,
    same plan" (reuse) and "open, foreign" (fail closed) mints a fresh session
    whose write here retires the old id — an open session for another plan is
    expired first. Same existing-row condition as the other billing-row setters.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET pending_checkout_session_id = :s, updated_at = :t",
        ExpressionAttributeValues={
            ":s": session_id,
            ":t": utc_now_iso(),
        },
        ConditionExpression="attribute_exists(owner_id)",
    )


async def try_claim_checkout_slot(owner_id: str, *, ttl_seconds: int = 240) -> str | None:
    """Conditional-write claim serializing the pending-Checkout read/write per owner.

    Without it two concurrent requests for different plans (different Stripe
    idempotency keys) could both read "no open session" and mint two
    completable sessions — completing both double-charges. The caller takes
    it BEFORE reading the pending marker (strongly consistent) and releases it
    on every exit. Returns a claim TOKEN to exactly ONE caller inside the TTL
    window (``None`` if the slot is held). The claim is a timestamp, not a
    flag: a request that dies holding it self-heals after ``ttl_seconds``. No
    phantom rows: guarded on ``attribute_exists(owner_id)``.

    Pass the returned token to :func:`release_checkout_slot` — a late
    release is then fenced on it and can only clear ITS OWN claim, never a
    concurrent request that re-claimed the expired slot in between.
    """
    table = _get_table()
    now = int(time.time())
    token = uuid.uuid4().hex
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET checkout_claim_at = :now, checkout_claim_token = :tok, updated_at = :t",
            ExpressionAttributeValues={
                ":now": now,
                ":tok": token,
                ":cutoff": now - ttl_seconds,
                ":t": utc_now_iso(),
            },
            ConditionExpression=(
                "attribute_exists(owner_id) AND "
                "(attribute_not_exists(checkout_claim_at) OR checkout_claim_at < :cutoff)"
            ),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return token


async def clear_phantom_oauth_connector(owner_id: str, *, expected_user_id: str) -> bool:
    """REMOVE a recorded connector ONLY if it still equals ``expected_user_id``.

    Recovery half of the phantom-connector fix (see
    ``oauth_service.maybe_clear_phantom_connector``): the caller has verified
    via Clerk that the recorded id names no existing user, and CASes the
    removal on that exact value — a concurrent legitimate claim (or any other
    writer) changing the field first fails the condition instead of being
    clobbered. Returns True when this call cleared it, False when the row or
    the expected value was already gone.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE oauth_connector_user_id SET updated_at = :t",
            ExpressionAttributeValues={":old": expected_user_id, ":t": utc_now_iso()},
            ConditionExpression=("attribute_exists(owner_id) AND oauth_connector_user_id = :old"),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def release_checkout_slot(owner_id: str, *, token: str) -> None:
    """Release the checkout-slot claim when the request holding it finishes,
    so the next click doesn't wait out the TTL. No-row / stale-token is a
    silent no-op.

    The REMOVE is ALWAYS fenced on the ``token`` returned by
    :func:`try_claim_checkout_slot`, so a request that hung past the TTL and
    then failed can only clear its OWN claim — never a concurrent create that
    re-claimed the expired slot in the meantime. An unconditional release
    deleted the new holder's claim, letting a third caller mint a second
    completable Checkout session → **two charged subscriptions**. There is no
    token-less path: the provider-switch flow that used to force-clear a dead
    session's claim now relies on the fresh mint's own ``try_claim`` (a stale
    claim is reclaimed; a live one yields a correct, retryable 409).
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE checkout_claim_at, checkout_claim_token SET updated_at = :t",
            ExpressionAttributeValues={":t": utc_now_iso(), ":tok": token},
            ConditionExpression="attribute_exists(owner_id) AND checkout_claim_token = :tok",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise


async def set_plan(owner_id: str, *, plan: str, now_iso: str) -> None:
    """Persist the owner's pricing tier on the billing row.

    ``plan`` is ``"standard"`` (the implicit single $50/mo tier) or
    ``"ultra_pro"`` (the higher tier that unlocks larger companies). The
    attribute is absent on rows created before the field existed — the billing
    response defaults a missing value to ``"standard"``, so a backfill isn't
    required. Same existing-row condition as the other billing-row setters
    (like other billing-row setters): an unconditional update would upsert a
    phantom ``{owner_id, plan}`` row with no stripe_customer_id.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET #p = :p, updated_at = :t",
        ExpressionAttributeNames={"#p": "plan"},
        ExpressionAttributeValues={":p": plan, ":t": now_iso},
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


_TRIAL_LOCAL_DURATION_SECONDS = 30 * 24 * 60 * 60


async def create_trial_local_if_absent(owner_id: str, owner_type: str, *, trial_end: int | None = None) -> bool:
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
        "created_at": now,
        "updated_at": now,
    }
    if trial_end is not None:
        item["trial_end"] = trial_end
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


async def arm_trial_if_unset(workspace_id: str, now_s: int | None = None) -> int | None:
    """Start an unarmed local trial once; losing polls read the winner's expiry."""
    trial_end = (int(time.time()) if now_s is None else now_s) + _TRIAL_LOCAL_DURATION_SECONDS
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": workspace_id},
            UpdateExpression="SET trial_end = :end, updated_at = :now",
            ConditionExpression="subscription_status = :trial AND attribute_not_exists(trial_end)",
            ExpressionAttributeValues={":trial": "trial_local", ":end": trial_end, ":now": utc_now_iso()},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = await get_by_owner_id(workspace_id, consistent_read=True)
        if row and row.get("subscription_status") == "trial_local" and row.get("trial_end") is not None:
            return int(row["trial_end"])
        return None
    return trial_end


async def extend_short_trial(workspace_id: str, row: dict) -> int:
    """Rows armed under the old 3-day window get signup + 30 days (owner ruling 2026-09-22),
    once: the write is conditioned on the old value, so racing polls apply it at most once."""
    current = int(row["trial_end"])
    try:
        target = int(datetime.fromisoformat(row["created_at"]).timestamp()) + _TRIAL_LOCAL_DURATION_SECONDS
    except (KeyError, TypeError, ValueError):
        return current
    if current >= target:
        return current
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": workspace_id},
            UpdateExpression="SET trial_end = :end, updated_at = :now",
            ConditionExpression="subscription_status = :trial AND trial_end = :old",
            ExpressionAttributeValues={":trial": "trial_local", ":end": target, ":old": current, ":now": utc_now_iso()},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = await get_by_owner_id(workspace_id, consistent_read=True) or {}
        return int(row.get("trial_end") or current)
    return target


async def mark_trial_first_agent_response(owner_id: str) -> bool:
    """Stamp ``first_agent_response_at`` on a ``trial_local`` row, once.

    The durable gate for the ``trial_first_agent_response`` product event
    (``product_analytics.schedule_trial_first_agent_response``). Same
    conditional-write idiom as ``create_trial_local_if_absent``: exactly one
    caller wins, across processes and restarts; every later call — and every
    call for a paid or org owner, whose row is not ``trial_local`` — no-ops.

    The stamp doubles as the owner's time-to-first-value. Returns ``True``
    iff THIS call wrote it.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET first_agent_response_at = :now, updated_at = :now",
            ConditionExpression=(
                "attribute_exists(owner_id) AND attribute_not_exists(first_agent_response_at) "
                "AND subscription_status = :trial"
            ),
            ExpressionAttributeValues={":now": utc_now_iso(), ":trial": "trial_local"},
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
) -> dict | None:
    """Persist Stripe subscription identity + status onto the billing account.

    Used on trial signup (Plan 3 §7.1) + every subscription state change
    via the customer.subscription.updated/deleted webhook. Records
    ``subscription_id`` (or ``None`` on cancellation) + ``status`` + the
    optional ``trial_end`` epoch so the rest of the system can read state
    without re-querying Stripe.

    """

    def _mutate(existing: dict) -> None:
        existing["stripe_subscription_id"] = subscription_id
        # Stripe's own status, recorded alongside the shared one so the two
        # providers can be COMBINED rather than overwriting each other (see
        # _effective_status). Without this the shared field is last-writer-wins
        # and an Apple expiry cancels a live Stripe subscription — or vice versa.
        existing["stripe_subscription_status"] = status
        _apply_effective_status(existing, prefer="stripe")
        # Always overwrite trial_end so a cancellation (trial_end=None) actually
        # clears the field (otherwise a stale trial countdown lingers after
        # subscription.deleted).
        if trial_end is not None:
            existing["trial_end"] = trial_end
        else:
            existing.pop("trial_end", None)

    return await _put_with_apple_cas(owner_id, _mutate)


_VALID_PROVIDER_CHOICES = frozenset({"byo_key", "chatgpt_oauth", "openrouter_managed"})


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
        provider_choice: one of ``byo_key``, ``chatgpt_oauth``,
            ``openrouter_managed``.
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

    # Only the billing row: the runtime follows through the caller's
    # hermes_provisioning.reconcile_runtime, which reads this row.
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
    publish_status_changed(owner_id)
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


_ONCE_FLAGS = frozenset({"trial_recap_emailed_at", "deletion_notice_at", "paused_at"})


async def claim_once(owner_id: str, field: str, now: int) -> bool:
    """Set ``field`` to ``now`` only if it was never set. True = this caller won (T19)."""
    assert field in _ONCE_FLAGS
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #f = :n, updated_at = :u",
            ConditionExpression="attribute_exists(owner_id) AND attribute_not_exists(#f)",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":n": int(now), ":u": utc_now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def release_once(owner_id: str, field: str) -> None:
    """Undo :func:`claim_once` after the guarded action failed, so a later pass retries."""
    assert field in _ONCE_FLAGS
    await run_in_thread(
        _get_table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="REMOVE #f",
        ExpressionAttributeNames={"#f": field},
    )


async def extend_trial(owner_id: str, *, trial_end: int) -> None:
    """Move a trial_local row's ``trial_end`` (the one 30-day pause, T19)."""
    await run_in_thread(
        _get_table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET trial_end = :e, updated_at = :u",
        ConditionExpression="subscription_status = :t",
        ExpressionAttributeValues={":e": int(trial_end), ":u": utc_now_iso(), ":t": "trial_local"},
    )


async def set_founding_team(owner_id: str) -> None:
    """Mark the workspace a founding team (two-year price lock at its checkout)."""
    await run_in_thread(
        _get_table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET founding_team = :t, updated_at = :u",
        ExpressionAttributeValues={":t": True, ":u": utc_now_iso()},
        ConditionExpression="attribute_exists(owner_id)",
    )


async def release_front_desk_claim(owner_id: str) -> None:
    """Clear the texting-line flag (the Front desk add-on): a release.

    It drops the claim timestamp and bumps ``front_desk_gen``, which keys the next line request, so a redelivery
    after a crash reuses the same Photon operation until a release happens. A missing billing row has no claim
    to release (a wipe deletes the row first): a no-op.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET front_desk = :f, updated_at = :u REMOVE front_desk_claim_at ADD front_desk_gen :one",
            ExpressionAttributeValues={":f": False, ":u": utc_now_iso(), ":one": 1},
            ConditionExpression="attribute_exists(owner_id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


async def claim_front_desk(owner_id: str, *, stale_before: str | None = None) -> int | None:
    """Single-flight claim before buying a line: the generation claimed under (the request key), or None when
    another delivery holds it.

    ``stale_before`` (an ISO time) also retakes a claim stamped earlier than it, in place: the crash repair
    must not bump ``front_desk_gen``, or the retry would request a second line under a new key.
    """
    now = utc_now_iso()
    condition = "attribute_not_exists(front_desk) OR front_desk = :f"
    values = {":v": True, ":f": False, ":u": now}
    if stale_before:
        condition += (
            " OR (front_desk = :v AND (attribute_not_exists(front_desk_claim_at) OR front_desk_claim_at < :stale))"
        )
        values[":stale"] = stale_before
    try:
        resp = await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET front_desk = :v, front_desk_claim_at = :u, updated_at = :u",
            ExpressionAttributeValues=values,
            ConditionExpression=f"attribute_exists(owner_id) AND ({condition})",
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return int(resp["Attributes"].get("front_desk_gen") or 0)


async def set_nightly_ceo_opted_out(owner_id: str, opted_out: bool) -> None:
    """Set/clear the durable nightly-CEO opt-out flag.

    Written from the Teams routine PATCH hook when the owner pauses/archives
    (opt-out True) or re-activates (opt-out False) the nightly CEO routine.
    Because billing is PRESERVED across a full reprovision (the container +
    Paperclip company are wiped, the billing row is not), this flag is what
    makes the owner's opt-out survive a company wipe — the arm step reads it on
    the fresh company and declines to re-seed.

    Same existing-row condition as the other billing-row setters; a missing row
    (never a paid owner, so never armed) is a silent no-op.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET nightly_ceo_opted_out = :o, updated_at = :t",
            ExpressionAttributeValues={":o": bool(opted_out), ":t": utc_now_iso()},
            ConditionExpression="attribute_exists(owner_id)",
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


async def set_provider_quota_until(owner_id: str, until: int) -> None:
    """Stamp when the owner's own LLM provider plan (e.g. ChatGPT) comes back from a usage
    limit. Display only -- nothing gates on it. No-op without a row."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET provider_quota_until = :until",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":until": int(until)},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def clear_openrouter_capped_day(owner_id: str) -> None:
    """Remove the openrouter-gate's daily-cap marker. No-op without a row."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE openrouter_capped_day",
            ConditionExpression="attribute_exists(owner_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def delete(owner_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"owner_id": owner_id})


# ---------------------------------------------------------------------------
# Company Stripe account — the owner's own business account on our Connect
# platform. One binding per owner; status mirrors Connect `account.updated`.
# Every write is a diff (see _write_changed_fields) so billing/Apple/provider
# fields can never be rolled back by these setters.
# ---------------------------------------------------------------------------

_COMPANY_STRIPE_FIELDS = (
    "company_stripe_account_id",
    # The ONE Stripe Provisioning Project for this company. It lives here and
    # not on a provisioning workflow row because every workflow must land in
    # the SAME Project — credentials are scoped by project, so a second
    # Project splits the company's credentials in two.
    "company_stripe_project_id",
    # A platform-created account Stripe Projects runs on when the main binding
    # can't (`connected` is a v1 Standard account v2 refuses; `api_key` has no
    # platform grant). Listed here so every unbind releases it with the main.
    "company_stripe_projects_account_id",
    "company_stripe_source",
    "company_stripe_details_submitted",
    "company_stripe_charges_enabled",
    "company_stripe_payouts_enabled",
    "company_stripe_connected_at",
    "company_stripe_updated_at",
)


def _is_condition_failure(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


async def _write_company_stripe(owner_id: str, mutate, *, condition: str, values: dict) -> dict | None:
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None
    before = dict(existing)
    after = dict(existing)
    mutate(after)
    after["company_stripe_updated_at"] = utc_now_iso()
    try:
        await _write_changed_fields(owner_id, before, after, condition=condition, values=values)
    except ClientError as e:
        if _is_condition_failure(e):
            return None
        raise
    return after


async def set_company_stripe_account(owner_id: str, *, account_id: str, source: str) -> dict | None:
    """Bind the company account exactly once. None = no row, or already bound."""
    # "api_key" is the owner's OWN restricted key, held backend-side — no
    # Connect relationship exists for that row, so the account.updated
    # webhook never fires for it and status is re-read on demand instead.
    if source not in ("created", "connected", "api_key"):
        raise ValueError("source must be 'created', 'connected' or 'api_key'")
    if not account_id:
        raise ValueError("account_id must be non-empty (it keys a GSI)")

    def mutate(row: dict) -> None:
        row["company_stripe_account_id"] = account_id
        row["company_stripe_source"] = source
        row["company_stripe_details_submitted"] = False
        row["company_stripe_charges_enabled"] = False
        row["company_stripe_payouts_enabled"] = False
        row["company_stripe_connected_at"] = utc_now_iso()

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="attribute_exists(owner_id) AND attribute_not_exists(company_stripe_account_id)",
        values={},
    )


async def set_company_stripe_project_id(owner_id: str, *, project_id: str) -> dict | None:
    """Record the company's Provisioning Project, exactly once.

    ``None`` = no billing row, or another worker got there first. The caller
    re-reads and uses the winner's id; a Project it created and lost the race
    with is inert metadata (no resources, no cost) and is adopted by name on a
    later pass.
    """
    if not project_id:
        raise ValueError("project_id must be non-empty")

    def mutate(row: dict) -> None:
        row["company_stripe_project_id"] = project_id

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="attribute_exists(owner_id) AND attribute_not_exists(company_stripe_project_id)",
        values={},
    )


async def set_company_stripe_projects_account(owner_id: str, *, account_id: str) -> dict | None:
    """Record the services account, exactly once, beside a bound main account.
    None = no row, no main binding, or another worker got there first."""
    if not account_id:
        raise ValueError("account_id must be non-empty")

    def mutate(row: dict) -> None:
        row["company_stripe_projects_account_id"] = account_id

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="attribute_exists(company_stripe_account_id) AND attribute_not_exists(company_stripe_projects_account_id)",
        values={},
    )


async def set_company_stripe_status(
    owner_id: str,
    *,
    account_id: str,
    details_submitted: bool,
    charges_enabled: bool,
    payouts_enabled: bool,
    livemode: bool | None = None,
) -> dict | None:
    """Mirror Connect's account.updated. None = the row is bound to a different account.
    ``livemode`` is only known for api_key rows (from the key prefix); Connect
    rows follow the platform key and leave it unset."""

    def mutate(row: dict) -> None:
        row["company_stripe_details_submitted"] = bool(details_submitted)
        row["company_stripe_charges_enabled"] = bool(charges_enabled)
        row["company_stripe_payouts_enabled"] = bool(payouts_enabled)
        if livemode is not None:
            row["company_stripe_livemode"] = bool(livemode)

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="company_stripe_account_id = :acct",
        values={":acct": account_id},
    )


async def clear_company_stripe_account(owner_id: str, *, account_id: str) -> dict | None:
    """Unbind after deauthorization. Conditioned on the id so a stale event can't clear a rebound row."""

    def mutate(row: dict) -> None:
        for key in _COMPANY_STRIPE_FIELDS:
            row.pop(key, None)

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="company_stripe_account_id = :acct",
        values={":acct": account_id},
    )


async def get_by_company_stripe_account_id(account_id: str) -> dict | None:
    if not account_id:
        return None
    resp = await run_in_thread(
        _get_table().query,
        IndexName="company-stripe-account-index",
        KeyConditionExpression="company_stripe_account_id = :acct",
        ExpressionAttributeValues={":acct": account_id},
        Limit=1,
    )
    items = resp.get("Items") or []
    return items[0] if items else None


async def list_company_stripe_owners() -> list[str]:
    """Owner ids with a bound company Stripe account (the rollup pass input).

    # ponytail: scan is fine at fleet size; GSI when >1k owners.
    """
    table = _get_table()
    owners: list[str] = []
    kwargs: dict = {
        "FilterExpression": Attr("company_stripe_account_id").exists(),
        "ProjectionExpression": "owner_id",
    }
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        owners.extend(item["owner_id"] for item in page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return owners
        kwargs["ExclusiveStartKey"] = last


# --- Capability policy (the P0d slice) -------------------------------------
# `capability_policy` is a map of capability -> bool ("does this capability
# require owner approval?"). An ABSENT key means True — fail-closed — so only
# explicitly-set keys are ever persisted; defaults are filled on read.

CAPABILITY_POLICY_KEYS = ("money_moves", "outreach", "publishing", "provisioning")


def _fill_capability_policy(stored: dict | None) -> dict[str, bool]:
    stored = stored or {}
    return {k: bool(stored.get(k, True)) for k in CAPABILITY_POLICY_KEYS}


async def get_capability_policy(owner_id: str) -> dict[str, bool]:
    row = await get_by_owner_id(owner_id) or {}
    return _fill_capability_policy(row.get("capability_policy"))


async def set_capability_policy(owner_id: str, updates: dict[str, bool]) -> dict[str, bool] | None:
    """Merge ``updates`` over the stored map and write it. Raises ValueError on
    an unknown key; ``None`` = no billing row. Returns the filled map."""
    unknown = set(updates) - set(CAPABILITY_POLICY_KEYS)
    if unknown:
        raise ValueError(f"unknown capability keys: {sorted(unknown)}")
    row = await get_by_owner_id(owner_id)
    if row is None:
        return None
    merged = {**(row.get("capability_policy") or {}), **{k: bool(v) for k, v in updates.items()}}
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET capability_policy = :p",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":p": merged},
        )
    except ClientError as e:
        if _is_condition_failure(e):
            return None
        raise
    return _fill_capability_policy(merged)


# --- Purchase limits (Link Agent Wallet) -----------------------------------
# Structured, never free text. Absent = no limit configured = purchases refused (fail-closed).


def _fill_purchase_limits(stored: dict | None) -> dict:
    stored = stored or {}
    return {
        "perPurchaseCents": int(stored.get("perPurchaseCents") or 0),
        "projectMonthlyCents": int(stored.get("projectMonthlyCents") or 0),
        "merchants": [str(m) for m in stored.get("merchants") or []],
    }


async def get_purchase_limits(owner_id: str) -> dict:
    row = await get_by_owner_id(owner_id) or {}
    return _fill_purchase_limits(row.get("purchase_limits"))


async def set_purchase_limits(owner_id: str, limits: dict) -> dict | None:
    """Replace the limits map. ``None`` = no billing row."""
    filled = _fill_purchase_limits(limits)
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET purchase_limits = :p",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":p": filled},
        )
    except ClientError as e:
        if _is_condition_failure(e):
            return None
        raise
    return filled
