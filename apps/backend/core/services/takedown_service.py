"""Takedown workflow (admin-initiated).

Under the Isol8-internal scope there is no public DMCA filing form, so the
takedown queue is structurally empty. The supported flow is:

- execute_admin_initiated_takedown(): admin types a reason on the listing
  detail page and grants the takedown in one shot. Writes the takedown row
  AND cascades license revocation + listing status flip + audit metadata.

The lower-level helpers below (file_takedown / execute_full_takedown) remain
as building blocks for forward-compatibility if a public filing form is ever
added; nothing in the live request path calls them today.
"""

import time
import uuid
from typing import Literal

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.services import license_service


class ListingNotFoundError(Exception):
    """Takedown was granted on a listing_id that does not exist."""


class TakedownNotFoundError(Exception):
    """Cascade was invoked with a takedown_id that does not exist (the row
    must be written by file_takedown / execute_admin_initiated_takedown
    before the cascade flips it to 'granted')."""


# Stable sentinel used for `filed_by_email` on admin-initiated takedowns —
# there is no real claimant, so we record an internal marker rather than
# leaving the field blank or reusing the admin's user_id (which already lives
# in `decided_by`).
ADMIN_FILED_BY_EMAIL = "admin@isol8.internal"


def _takedowns_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_TAKEDOWNS_TABLE)


def _purchases_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PURCHASES_TABLE)


def _listings_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)


async def file_takedown(
    *,
    listing_id: str,
    reason: Literal["dmca", "policy", "fraud", "seller-request"],
    claimant_name: str,
    claimant_email: str,
    basis_md: str,
) -> str:
    """Create a pending takedown row. Returns takedown_id.

    Retained for forward-compatibility with a future public filing form; not
    called by any live route today.
    """
    tid = str(uuid.uuid4())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _takedowns_table().put_item(
        Item={
            "listing_id": listing_id,
            "takedown_id": tid,
            "reason": reason,
            "filed_by_name": claimant_name,
            "filed_by_email": claimant_email,
            "basis_md": basis_md,
            "filed_at": now_iso,
            "decision": "pending",
        }
    )
    return tid


async def _cascade_takedown(
    *,
    listing_id: str,
    takedown_id: str,
    decided_by: str,
    now_iso: str,
) -> int:
    """Cascade the side effects of granting a takedown.

    Revokes every purchase's license, flips the listing status to
    `taken_down`, and stamps the takedown row with the granted decision +
    affected-purchases count. Returns the number of revoked purchases.
    """
    # Page through every purchase row — DynamoDB Query caps each page at 1MB
    # (or the explicit Limit), so a single .query() call leaves later buyers
    # with still-valid licenses on a high-volume listing. Loop on
    # LastEvaluatedKey so the cascade revokes ALL purchases.
    items: list[dict] = []
    purchases_table = _purchases_table()
    query_kwargs: dict = {
        "IndexName": "listing-created-index",
        "KeyConditionExpression": "listing_id = :l",
        "ExpressionAttributeValues": {":l": listing_id},
    }
    while True:
        page = purchases_table.query(**query_kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        query_kwargs["ExclusiveStartKey"] = last
    for purchase in items:
        await license_service.revoke(
            purchase_id=purchase["purchase_id"],
            buyer_id=purchase["buyer_id"],
            reason="takedown",
        )

    # Resolve the currently-published version dynamically — hardcoding v=1
    # leaves a v2 listing live after takedown (publish_new_version retires v1
    # and publishes v2; admin "success" but listing remains purchasable).
    # Falls back to the highest-version row if nothing is currently
    # published, so a takedown on a draft/review listing still flips state.
    listings_table = _listings_table()
    rows = listings_table.query(
        KeyConditionExpression="listing_id = :l",
        ExpressionAttributeValues={":l": listing_id},
        ScanIndexForward=False,  # newest version first
    ).get("Items", [])
    if not rows:
        raise ListingNotFoundError(f"listing {listing_id} does not exist")
    target = next((r for r in rows if r.get("status") == "published"), rows[0])
    target_version = int(target["version"])

    try:
        listings_table.update_item(
            Key={"listing_id": listing_id, "version": target_version},
            UpdateExpression="SET #s = :taken, updated_at = :now",
            ConditionExpression="attribute_exists(listing_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":taken": "taken_down", ":now": now_iso},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ListingNotFoundError(f"listing {listing_id} v{target_version} does not exist")
        raise

    # Cascade to any in-flight v(N+1) draft/review row. Without this, taking
    # down v(N) leaves a non-terminal v(N+1) orphaned — publish_new_version
    # would later fail because v(N) is no longer 'published', and the
    # seller's UI would surface a draft on a taken-down listing as if it
    # were still actionable. By the create_new_version invariant there is
    # at most one such row, but the loop is correct for any count and the
    # cascade adds a bounded extra DDB call (typically zero, at most one).
    #
    # Each flip is conditional on the row STILL being in draft/review to
    # avoid clobbering a race (e.g. seller racing a discard_draft against
    # the admin's takedown). target_version is excluded so we don't
    # double-write the v(N) row when the fallback above already targeted
    # a non-terminal highest row.
    for row in rows:
        v = int(row["version"])
        if v == target_version:
            continue
        if row.get("status") not in ("draft", "review"):
            continue
        try:
            listings_table.update_item(
                Key={"listing_id": listing_id, "version": v},
                UpdateExpression="SET #s = :taken, updated_at = :now",
                ConditionExpression="#s IN (:draft, :review)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":taken": "taken_down",
                    ":draft": "draft",
                    ":review": "review",
                    ":now": now_iso,
                },
            )
        except ClientError as e:
            # Race: the seller transitioned the row (e.g. discarded the
            # draft, or it was already taken_down via a second admin
            # action) between our query and this update. Swallow — the
            # row is no longer in a non-terminal state, which is the
            # desired end state.
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise

    # Guard against upsert: a malformed grant request with a fabricated
    # takedown_id would otherwise create a partial row {listing_id,
    # takedown_id, decision:'granted', decided_by, decided_at,
    # affected_purchases} with no claimant fields. The takedown row is
    # written by the caller (file_takedown / execute_admin_initiated_
    # takedown) before this cascade runs, so attribute_exists is the
    # correct invariant.
    try:
        _takedowns_table().update_item(
            Key={"listing_id": listing_id, "takedown_id": takedown_id},
            UpdateExpression=("SET decision = :granted, decided_by = :by, decided_at = :now, affected_purchases = :n"),
            ConditionExpression="attribute_exists(takedown_id)",
            ExpressionAttributeValues={
                ":granted": "granted",
                ":by": decided_by,
                ":now": now_iso,
                ":n": len(items),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise TakedownNotFoundError(f"takedown {takedown_id} for listing {listing_id} does not exist")
        raise
    return len(items)


async def execute_full_takedown(*, listing_id: str, takedown_id: str, decided_by: str) -> None:
    """Admin action: flip listing to taken_down, revoke all licenses.

    Retained for forward-compatibility with a future public filing form; not
    called by any live route today.
    """
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await _cascade_takedown(
        listing_id=listing_id,
        takedown_id=takedown_id,
        decided_by=decided_by,
        now_iso=now_iso,
    )


async def execute_admin_initiated_takedown(
    *,
    listing_id: str,
    reason: Literal["dmca", "policy", "fraud", "seller-request"],
    basis_md: str,
    decided_by: str,
) -> dict:
    """Admin-initiated takedown.

    Writes a takedown row and immediately cascades license revocation +
    listing status flip in one shot. The admin's `user_id` is recorded in
    `decided_by`; `filed_by_email` is the internal admin sentinel so the
    audit-log view can distinguish admin-initiated rows from any future
    publicly filed ones.

    Returns ``{ "takedown_id", "listing_id", "affected_purchases" }``.
    """
    tid = str(uuid.uuid4())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Validate the listing exists BEFORE writing the pending row — otherwise
    # a malformed admin grant on a phantom listing_id leaves the cascade
    # raising ListingNotFoundError but the orphan "pending" row stays in
    # DDB forever, polluting the takedown audit view.
    rows = (
        _listings_table()
        .query(
            KeyConditionExpression="listing_id = :l",
            ExpressionAttributeValues={":l": listing_id},
            Limit=1,
        )
        .get("Items", [])
    )
    if not rows:
        raise ListingNotFoundError(f"listing {listing_id} does not exist")

    # Symmetric with `file_takedown` + `execute_full_takedown`: write a
    # "pending" row first, then let `_cascade_takedown` perform the single
    # granted-stamp + affected-purchases count. Avoids a transient state
    # where the row carries `granted` but no `affected_purchases`, and keeps
    # the cascade path the only place that ever flips decision → granted.
    _takedowns_table().put_item(
        Item={
            "listing_id": listing_id,
            "takedown_id": tid,
            "reason": reason,
            "filed_by_name": "admin",
            "filed_by_email": ADMIN_FILED_BY_EMAIL,
            "basis_md": basis_md,
            "filed_at": now_iso,
            "decision": "pending",
        }
    )

    affected = await _cascade_takedown(
        listing_id=listing_id,
        takedown_id=tid,
        decided_by=decided_by,
        now_iso=now_iso,
    )

    return {
        "takedown_id": tid,
        "listing_id": listing_id,
        "affected_purchases": affected,
    }
