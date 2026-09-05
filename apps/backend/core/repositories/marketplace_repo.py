"""Marketplace repository -- DynamoDB operations for the marketplace tables
(listings, purchases, payout-accounts, takedowns, sellers).

Unlike the prefix-convention repos, marketplace tables are addressed by
full-name settings (``MARKETPLACE_*_TABLE`` env vars), so the factories build
``boto3.resource("dynamodb")`` directly instead of going through
``core.dynamodb.get_table``.

Timestamps use the marketplace's pinned second-resolution format
``%Y-%m-%dT%H:%M:%SZ`` -- NOT ``core.dynamodb.utc_now_iso`` (microseconds +
UTC offset): the refund window parses ``created_at`` back with
``time.strptime`` on the exact format. Callers supply ``now_iso`` so one
stamp can span multiple writes (e.g. the takedown cascade).

Conditional-write failure policy: functions whose failure is a swallow/no-op
at the call site return ``bool`` and absorb ONLY
``ConditionalCheckFailedException`` (or the transact-cancel pair); functions
whose failure maps to a typed domain error let ``ClientError`` propagate so
services keep their existing exception mapping.
"""

import time

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread


def _listings_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)


def _purchases_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PURCHASES_TABLE)


def _payout_accounts_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PAYOUT_ACCOUNTS_TABLE)


def _takedowns_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_TAKEDOWNS_TABLE)


def _sellers_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_SELLERS_TABLE)


def _ddb_client():
    return boto3.client("dynamodb")


# ---------------------------------------------------------------------------
# Listings (PK listing_id S, SK version N; GSIs seller-created-index,
# status-published-index)
# ---------------------------------------------------------------------------


async def get_listing(listing_id: str, version: int) -> dict | None:
    """Look up one listing row by (listing_id, version)."""
    table = _listings_table()
    resp = await run_in_thread(table.get_item, Key={"listing_id": listing_id, "version": version})
    return resp.get("Item")


async def get_versions_page(
    listing_id: str,
    *,
    newest_first: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """SINGLE-page query over a listing's version rows.

    Deliberately not paginated: a listing accumulates one row per republish,
    so the 1MB page cap is never the binding constraint for pick-a-row
    callers. Callers that must see every row (max-version math) use
    :func:`list_all_versions` instead.
    """
    table = _listings_table()
    kwargs: dict = {
        "KeyConditionExpression": "listing_id = :l",
        "ExpressionAttributeValues": {":l": listing_id},
    }
    if newest_first:
        kwargs["ScanIndexForward"] = False  # newest version first
    if limit is not None:
        kwargs["Limit"] = limit
    resp = await run_in_thread(table.query, **kwargs)
    return resp.get("Items", [])


async def list_all_versions(listing_id: str) -> list[dict]:
    """Every non-sentinel version row for a listing, across pages.

    ``version > 0`` excludes the legacy v=0 slug-reservation sentinel (a row
    with no status/seller_id that would noise up every state check). The
    pagination loop matters here because callers compute max(version)+1 --
    a truncated view would mint a duplicate version.
    """
    table = _listings_table()
    rows: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": "listing_id = :l AND version > :z",
            "ExpressionAttributeValues": {":l": listing_id, ":z": 0},
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        resp = await run_in_thread(table.query, **kwargs)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if last_key is None:
            break
    return rows


async def list_listings_by_seller(seller_id: str) -> list[dict]:
    """All of a seller's listing rows via seller-created-index, paginated --
    a prolific seller's later listings must not be silently skipped (the
    payout flush iterates purchases per listing)."""
    table = _listings_table()
    items: list[dict] = []
    kwargs: dict = {
        "IndexName": "seller-created-index",
        "KeyConditionExpression": "seller_id = :s",
        "ExpressionAttributeValues": {":s": seller_id},
    }
    while True:
        page = await run_in_thread(table.query, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


async def list_published_listings_by_seller_agent(seller_id: str, agent_id: str) -> list[dict]:
    """The seller's PUBLISHED rows for one agent, via seller-created-index
    with a filter. Paginated: the filter runs after the 1MB page read, so a
    match can sit on any page."""
    table = _listings_table()
    rows: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs = {
            "IndexName": "seller-created-index",
            "KeyConditionExpression": "seller_id = :sid",
            "FilterExpression": "agent_id = :aid AND #s = :pub",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":sid": seller_id,
                ":aid": agent_id,
                ":pub": "published",
            },
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        resp = await run_in_thread(table.query, **kwargs)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if last_key is None:
            break
    return rows


async def get_review_queue_page(*, limit: int = 50) -> list[dict]:
    """Single page of the moderation queue via status-published-index.

    ``published_at`` is the GSI sort key; ``mark_review`` populates it at
    submit time precisely so review rows appear here -- DynamoDB's
    sparse-GSI semantics exclude items whose sort key is missing.
    """
    table = _listings_table()
    resp = await run_in_thread(
        table.query,
        IndexName="status-published-index",
        KeyConditionExpression="#s = :review",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":review": "review"},
        Limit=limit,
    )
    return resp.get("Items", [])


async def scan_all_listings() -> list[dict]:
    """Unfiltered paginated scan of the listings table.

    No FilterExpression on purpose: the in-Python status gate in
    ``marketplace_search.search()`` is the load-bearing public-vs-seller
    split, and a DDB-side status filter would silently drop
    draft/review/retired/taken_down rows before ``include_unpublished=True``
    could ever surface them.
    """
    table = _listings_table()
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        resp = await run_in_thread(table.scan, **scan_kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last
    return items


async def put_draft_v1(
    *,
    listing_id: str,
    slug: str,
    name: str,
    description_md: str,
    format: str,
    price_cents: int,
    tags: list[str],
    category: str | None,
    seller_id: str,
    s3_prefix: str,
    manifest_sha256: str,
    manifest_json_str: str,
    now_iso: str,
) -> None:
    """Write the v=1 draft row.

    Kept on the low-level client (typed AttributeValues) so the write shape
    stays byte-identical with the transactional writers on this table. The
    row is written as a single put_item keyed on the UUID listing_id -- no
    sentinel row, no transaction (slugs are not unique).
    """
    client = _ddb_client()
    await run_in_thread(
        client.put_item,
        TableName=settings.MARKETPLACE_LISTINGS_TABLE,
        Item={
            "listing_id": {"S": listing_id},
            "version": {"N": "1"},
            "slug": {"S": slug},
            "name": {"S": name},
            "description_md": {"S": description_md},
            "format": {"S": format},
            "price_cents": {"N": str(price_cents)},
            "tags": {"L": [{"S": t} for t in tags]},
            **({"category": {"S": category}} if category else {}),
            "seller_id": {"S": seller_id},
            "status": {"S": "draft"},
            "s3_prefix": {"S": s3_prefix},
            "manifest_sha256": {"S": manifest_sha256},
            "manifest_json": {"S": manifest_json_str},
            "artifact_format_version": {"S": "v1"},
            "entitlement_policy": {"S": "perpetual"},
            "artifact_uploaded": {"BOOL": False},
            "created_at": {"S": now_iso},
            "updated_at": {"S": now_iso},
        },
    )


async def put_version_row_if_absent(new_row: dict) -> None:
    """Conditional insert of a cloned v(N+1) row.

    The condition closes the read-then-write race: if two callers both scan
    the same row set and compute the same new version, only the first
    put_item succeeds. ClientError propagates -- the service maps
    ConditionalCheckFailedException to its republish-race error.
    """
    table = _listings_table()
    await run_in_thread(
        table.put_item,
        Item=new_row,
        ConditionExpression="attribute_not_exists(listing_id)",
    )


async def update_artifact_fields(
    *,
    listing_id: str,
    version: int,
    seller_id: str,
    sha: str,
    manifest: dict,
    s3_prefix: str,
    now_iso: str,
    agent_id: str | None = None,
    bundle_mcps: list[str] | None = None,
    bundle_skills: list[str] | None = None,
) -> None:
    """Persist artifact metadata onto a draft row after the S3 re-upload.

    ``agent_id`` is a top-level column so the publish-wizard's edit mode can
    lock the agent picker without parsing manifest_json. ``bundle_mcps`` /
    ``bundle_skills`` persist the seller's bundle selection so edit mode can
    prefill the checkboxes instead of falsely defaulting to "ship nothing".
    The condition re-checks seller match + draft state in case state changed
    between the caller's read and this update; ClientError propagates.
    """
    set_clause = (
        "SET manifest_sha256 = :sha, manifest_json = :mj,"
        "    s3_prefix = :prefix, artifact_uploaded = :uploaded,"
        "    updated_at = :now"
    )
    values: dict = {
        ":sha": sha,
        ":mj": manifest,
        ":prefix": s3_prefix,
        ":uploaded": True,
        ":sid": seller_id,
        ":draft": "draft",
        ":now": now_iso,
    }
    if agent_id is not None:
        set_clause += ", agent_id = :aid"
        values[":aid"] = agent_id
    if bundle_mcps is not None:
        set_clause += ", bundle_mcps = :bmcps"
        values[":bmcps"] = bundle_mcps
    if bundle_skills is not None:
        set_clause += ", bundle_skills = :bskills"
        values[":bskills"] = bundle_skills
    table = _listings_table()
    await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression=set_clause,
        ConditionExpression="seller_id = :sid AND #s = :draft",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )


async def patch_draft_metadata(
    *,
    listing_id: str,
    version: int,
    seller_id: str,
    updates: dict,
    now_iso: str,
) -> dict:
    """Patch metadata columns on a draft/review row, forcing status back to
    draft (no-op on a draft row; demotes a review row out of the moderation
    queue so moderators never approve a mid-mutation snapshot).

    ``name`` is a DDB reserved word; every column is routed through
    #-aliases so the SET clause is uniform and immune to future
    reserved-word additions. The IN-list condition keeps the gate tight:
    published / retired / taken_down rows can't be mutated via this path.
    ClientError propagates -- the service maps the conditional failure.
    """
    set_clauses: list[str] = []
    expr_names: dict[str, str] = {"#s": "status"}
    expr_values: dict[str, object] = {
        ":sid": seller_id,
        ":draft": "draft",
        ":review": "review",
        ":now": now_iso,
    }
    for i, (col, val) in enumerate(updates.items()):
        name_placeholder = f"#c{i}"
        value_placeholder = f":c{i}"
        expr_names[name_placeholder] = col
        expr_values[value_placeholder] = val
        set_clauses.append(f"{name_placeholder} = {value_placeholder}")
    set_clauses.append("#s = :draft")
    set_clauses.append("updated_at = :now")

    table = _listings_table()
    resp = await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ConditionExpression="seller_id = :sid AND #s IN (:draft, :review)",
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})


async def delete_draft_row(*, listing_id: str, version: int) -> None:
    """Delete a row iff it is still in draft state. ClientError propagates --
    a conditional failure means the row transitioned between the caller's
    read and this delete (e.g. a parallel submit won), which the service
    maps to its state-conflict error."""
    table = _listings_table()
    await run_in_thread(
        table.delete_item,
        Key={"listing_id": listing_id, "version": version},
        ConditionExpression="#s = :draft",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":draft": "draft"},
    )


async def mark_review(*, listing_id: str, version: int, seller_id: str, now_iso: str) -> dict:
    """Transition draft -> review.

    ``published_at`` is populated at submit time even though the listing
    isn't yet "published": status-published-index has published_at as its
    sort key, and DynamoDB's sparse-GSI semantics exclude items whose sort
    key is missing -- without a value here the moderation queue is
    structurally empty. The approve flow overwrites it on the actual publish.

    The REMOVE strips every moderation field so a resubmitted listing
    doesn't carry "rejected because X (by admin_xyz)" into review (and from
    there, if approved, onto the public storefront -- marketplace_search
    returns raw listing rows). The audit trail lives in admin_actions.

    ClientError propagates -- the service maps the conditional failure.
    """
    table = _listings_table()
    resp = await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression=(
            "SET #s = :review, updated_at = :now, published_at = :now REMOVE rejection_notes, rejected_at, rejected_by"
        ),
        ConditionExpression="seller_id = :sid AND #s = :draft",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":sid": seller_id,
            ":draft": "draft",
            ":review": "review",
            ":now": now_iso,
        },
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})


async def mark_published(*, listing_id: str, version: int, approved_by: str, now_iso: str) -> dict:
    """Transition review -> published (first publish; version flips that must
    also retire a previous version go through :func:`transact_publish_flip`).
    ClientError propagates."""
    table = _listings_table()
    resp = await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression=("SET #s = :pub, published_at = :now, published_by = :by, updated_at = :now"),
        ConditionExpression="#s = :review",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":review": "review",
            ":pub": "published",
            ":now": now_iso,
            ":by": approved_by,
        },
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})


async def mark_rejected(*, listing_id: str, version: int, notes: str, rejected_by: str, now_iso: str) -> None:
    """Transition review -> draft with rejection notes.

    Conditional on status='review' so the call cannot upsert a fabricated
    listing (DDB UpdateItem upserts by default), nor flip a published or
    taken_down row back to draft via a malformed admin request.
    ClientError propagates.
    """
    table = _listings_table()
    await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression=(
            "SET #s = :draft, rejection_notes = :notes, rejected_by = :by,    rejected_at = :now, updated_at = :now"
        ),
        ConditionExpression="#s = :review",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":draft": "draft",
            ":review": "review",
            ":notes": notes,
            ":by": rejected_by,
            ":now": now_iso,
        },
    )


async def transact_publish_flip(
    *,
    listing_id: str,
    prev_version: int,
    new_version: int,
    approved_by: str,
    now_iso: str,
) -> None:
    """Atomic flip: prev_version -> 'retired', new_version -> 'published'.

    ONE TransactWriteItems call so there is no observable state where two
    versions of the same listing_id are simultaneously 'published'. Each
    leg's condition asserts its precondition (prev currently 'published',
    new currently 'review') and fails the WHOLE transaction if violated.
    ClientError (incl. TransactionCanceledException) propagates -- the
    service maps it to its InvalidStateError.

    install_count is a per-listing LIFETIME counter that lives on whichever
    version row is currently 'published' (the only row the seller dashboard
    reads). Deploys bump the published row, so a naive flip would strand the
    prior version's count on the now-retired row and reset the visible count
    to 0. We carry it forward: the new published row inherits the retiring
    row's count and keeps accumulating.
    # ponytail: a deploy that bumps prev between this read and the flip loses
    # that one increment; install counts are already best-effort, so this is
    # acceptable. TransactWriteItems can't copy an attribute across items, so
    # a pre-read is the only way to carry it inside the atomic flip.
    """
    client = _ddb_client()
    listings_tbl = settings.MARKETPLACE_LISTINGS_TABLE
    # Pre-read the retiring row's install_count on the SAME low-level client as
    # the flip below, so the whole operation is one client (and one mock
    # surface). Low-level get_item returns DynamoDB-JSON, hence the {"N": ...}.
    prev_resp = await run_in_thread(
        client.get_item,
        TableName=listings_tbl,
        Key={"listing_id": {"S": listing_id}, "version": {"N": str(prev_version)}},
        ProjectionExpression="install_count",
    )
    carried_installs = int((prev_resp.get("Item") or {}).get("install_count", {}).get("N", "0") or 0)
    new_update_expr = "SET #s = :pub, published_at = :now, published_by = :by, updated_at = :now"
    new_values = {
        ":review": {"S": "review"},
        ":pub": {"S": "published"},
        ":now": {"S": now_iso},
        ":by": {"S": approved_by},
    }
    if carried_installs:
        new_update_expr += ", install_count = if_not_exists(install_count, :zero) + :carried"
        new_values[":zero"] = {"N": "0"}
        new_values[":carried"] = {"N": str(carried_installs)}
    await run_in_thread(
        client.transact_write_items,
        TransactItems=[
            {
                "Update": {
                    "TableName": listings_tbl,
                    "Key": {
                        "listing_id": {"S": listing_id},
                        "version": {"N": str(prev_version)},
                    },
                    "ConditionExpression": "#s = :pub",
                    "UpdateExpression": "SET #s = :retired, updated_at = :now",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":pub": {"S": "published"},
                        ":retired": {"S": "retired"},
                        ":now": {"S": now_iso},
                    },
                },
            },
            {
                "Update": {
                    "TableName": listings_tbl,
                    "Key": {
                        "listing_id": {"S": listing_id},
                        "version": {"N": str(new_version)},
                    },
                    "ConditionExpression": "#s = :review",
                    "UpdateExpression": new_update_expr,
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": new_values,
                },
            },
        ],
    )


async def increment_listing_install_count(*, listing_id: str, version: int) -> None:
    """Atomic install-counter bump on a listing row after a successful deploy.

    The seller dashboard reads ``install_count`` off the listing row (the
    purchase-row counter tracks license-key installs, a different surface).
    ADD-style if_not_exists so legacy rows without the column start at 0;
    conditional on the row existing so the bump can never upsert a phantom
    listing. Callers treat this as best-effort — a counter must never fail
    a committed deploy — so ClientError propagates for them to swallow.
    """
    table = _listings_table()
    await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression="SET install_count = if_not_exists(install_count, :zero) + :one",
        ConditionExpression="attribute_exists(listing_id)",
        ExpressionAttributeValues={":zero": 0, ":one": 1},
    )


async def mark_taken_down(*, listing_id: str, version: int, now_iso: str) -> None:
    """Flip a row to taken_down. Conditional on attribute_exists so the
    update can't upsert a fake listing for a nonexistent id. ClientError
    propagates -- the service maps the conditional failure to its
    not-found error."""
    table = _listings_table()
    await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression="SET #s = :taken, updated_at = :now",
        ConditionExpression="attribute_exists(listing_id)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":taken": "taken_down", ":now": now_iso},
    )


async def mark_taken_down_if_nonterminal(*, listing_id: str, version: int, now_iso: str) -> bool:
    """Flip a draft/review row to taken_down.

    Returns False when the row already left the non-terminal state between
    the caller's read and this write (seller discarded the draft, or a
    second admin action won) -- that is the desired end state, so callers
    treat False as done rather than an error.
    """
    table = _listings_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"listing_id": listing_id, "version": version},
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
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# Purchases (PK buyer_id, SK purchase_id; GSIs listing-created-index,
# payment-intent-index, license-key-index)
# ---------------------------------------------------------------------------


async def get_purchase(buyer_id: str, purchase_id: str, *, consistent: bool = False) -> dict | None:
    """consistent=True forces a strongly-consistent base-table read — the
    payout flush re-reads right before moving money, where the default
    eventually-consistent get_item could still miss a just-stamped
    seller_transfer_id. Other callers keep the cheaper default."""
    table = _purchases_table()
    resp = await run_in_thread(
        table.get_item,
        Key={"buyer_id": buyer_id, "purchase_id": purchase_id},
        ConsistentRead=consistent,
    )
    return resp.get("Item")


async def list_purchases_by_buyer(buyer_id: str) -> list[dict]:
    """Every purchase row for a buyer, across pages. The table's sort key is
    purchase_id (UUID), so callers wanting newest-first must sort by
    created_at themselves -- ScanIndexForward would give reverse-lexical
    UUID order."""
    table = _purchases_table()
    raws: list[dict] = []
    kwargs: dict = {
        "KeyConditionExpression": "buyer_id = :b",
        "ExpressionAttributeValues": {":b": buyer_id},
    }
    while True:
        page = await run_in_thread(table.query, **kwargs)
        raws.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return raws


async def list_purchases_by_listing(listing_id: str) -> list[dict]:
    """Every purchase row for a listing via listing-created-index, across
    pages -- the takedown cascade and payout flush must see ALL buyers; a
    single 1MB page would leave later buyers with still-valid licenses (or
    sellers unpaid) on a high-volume listing."""
    table = _purchases_table()
    items: list[dict] = []
    query_kwargs: dict = {
        "IndexName": "listing-created-index",
        "KeyConditionExpression": "listing_id = :l",
        "ExpressionAttributeValues": {":l": listing_id},
    }
    while True:
        page = await run_in_thread(table.query, **query_kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        query_kwargs["ExclusiveStartKey"] = last
    return items


async def find_purchase_by_payment_intent(payment_intent_id: str) -> dict | None:
    """Single Query via the payment-intent GSI, not a full-table scan."""
    table = _purchases_table()
    resp = await run_in_thread(
        table.query,
        IndexName="payment-intent-index",
        KeyConditionExpression="stripe_payment_intent_id = :pi",
        ExpressionAttributeValues={":pi": payment_intent_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


async def find_purchase_by_license_key(license_key: str) -> dict | None:
    table = _purchases_table()
    resp = await run_in_thread(
        table.query,
        IndexName="license-key-index",
        KeyConditionExpression="license_key = :k",
        ExpressionAttributeValues={":k": license_key},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


async def has_unrevoked_purchase(*, buyer_id: str, listing_id: str) -> bool:
    """True iff the buyer holds a non-revoked purchase row for this listing.

    Scopes by listing (listing-created-index, newest first) rather than by
    buyer: per-listing buyer count is much smaller than a power buyer's
    purchase count, so the matching row is reached fast. Pages until a match
    or exhaustion -- a capped buyer-partition query silently failed open at
    scale (a buyer with more purchases than the cap ahead of the matching
    one, in effectively random UUID sort order, was told they had no
    entitlement).
    """
    table = _purchases_table()
    kwargs: dict = {
        "IndexName": "listing-created-index",
        "KeyConditionExpression": "listing_id = :l",
        "ExpressionAttributeValues": {":l": listing_id},
        "ScanIndexForward": False,  # newest first
    }
    while True:
        resp = await run_in_thread(table.query, **kwargs)
        for item in resp.get("Items", []):
            if item.get("buyer_id") != buyer_id:
                continue
            if item.get("license_key_revoked"):
                continue
            return True
        last = resp.get("LastEvaluatedKey")
        if not last:
            return False
        kwargs["ExclusiveStartKey"] = last


async def set_license_revoked(*, buyer_id: str, purchase_id: str, reason: str) -> None:
    """Mark license_key_revoked + reason on a purchase row. The revocation
    timestamp is an epoch int (unlike the row's ISO created_at) -- it feeds
    no string sort, only display."""
    table = _purchases_table()
    await run_in_thread(
        table.update_item,
        Key={"buyer_id": buyer_id, "purchase_id": purchase_id},
        UpdateExpression=(
            "SET license_key_revoked = :true,     license_key_revoked_reason = :r,     license_key_revoked_at = :now"
        ),
        ExpressionAttributeValues={
            ":true": True,
            ":r": reason,
            ":now": int(time.time()),
        },
    )


async def append_install(*, buyer_id: str, purchase_id: str, source_ip: str) -> None:
    """Append the install IP+timestamp to the purchase's install_log and bump
    install_count."""
    table = _purchases_table()
    await run_in_thread(
        table.update_item,
        Key={"buyer_id": buyer_id, "purchase_id": purchase_id},
        UpdateExpression=(
            "SET install_log = list_append("
            "      if_not_exists(install_log, :empty), :entry"
            "    ), "
            "    install_count = if_not_exists(install_count, :zero) + :one, "
            "    last_install_at = :now"
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":entry": [{"ip": source_ip, "ts": int(time.time())}],
            ":zero": 0,
            ":one": 1,
            ":now": int(time.time()),
        },
    )


async def set_seller_transfer_if_absent(*, buyer_id: str, purchase_id: str, transfer_id: str, now_iso: str) -> bool:
    """Stamp the payout transfer id on a purchase row -- set-once.

    listing-created-index is eventually consistent, so a replayed or
    concurrent flush can re-read the same purchase before a prior run's
    stamp is visible. This conditional write is the source of truth for
    "this run transferred it": returns False when another run got there
    first (the Stripe Transfer was idempotent, so no money moved twice) and
    the caller must not count the amount toward its held-balance decrement.
    """
    table = _purchases_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"buyer_id": buyer_id, "purchase_id": purchase_id},
            UpdateExpression="SET seller_transfer_id = :t, transferred_at = :now",
            ConditionExpression="attribute_not_exists(seller_transfer_id)",
            ExpressionAttributeValues={":t": transfer_id, ":now": now_iso},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def transact_record_purchase_and_credit_seller(
    *,
    purchase_id: str,
    buyer_id: str,
    seller_id: str,
    listing_id: str,
    version: int,
    amount_cents: int,
    seller_share_cents: int | None = None,
    payment_intent_id: str,
    session_id: str,
    transfer_group: str,
    license_key: str,
    now_iso: str,
) -> bool:
    """Atomic fulfillment: the purchase-row Put AND the seller held-balance
    credit happen in a single TransactWriteItems, so partial-failure replay
    can never leave the buyer with a license but the seller's accounting
    missing the credit (or vice versa).

    Idempotency: the Put is conditional on attribute_not_exists(purchase_id)
    (purchase_id is deterministic from the Stripe checkout session -- one
    buy = one session). On a replay the conditional fails, the ENTIRE
    transaction cancels, and neither the purchase nor the balance is written
    a second time -- returns False so the caller treats it as a no-op.

    seller_share_cents is the post-platform-fee amount credited to the
    seller's held balance (and later transferred); amount_cents remains what
    the buyer paid. None (legacy callers/tests) means no fee: share = amount.
    """
    if seller_share_cents is None:
        seller_share_cents = amount_cents
    client = _ddb_client()
    try:
        await run_in_thread(
            client.transact_write_items,
            TransactItems=[
                {
                    "Put": {
                        "TableName": settings.MARKETPLACE_PURCHASES_TABLE,
                        "Item": {
                            "buyer_id": {"S": buyer_id},
                            "purchase_id": {"S": purchase_id},
                            "listing_id": {"S": listing_id},
                            "seller_id": {"S": seller_id},
                            "listing_version_at_purchase": {"N": str(version)},
                            "entitlement_version_floor": {"N": str(version)},
                            "price_paid_cents": {"N": str(amount_cents)},
                            "seller_share_cents": {"N": str(seller_share_cents)},
                            "stripe_payment_intent_id": {"S": payment_intent_id},
                            "stripe_checkout_session_id": {"S": session_id},
                            "stripe_transfer_group": {"S": transfer_group},
                            "license_key": {"S": license_key},
                            "license_key_revoked": {"BOOL": False},
                            "status": {"S": "paid"},
                            "install_count": {"N": "0"},
                            "created_at": {"S": now_iso},
                        },
                        "ConditionExpression": "attribute_not_exists(purchase_id)",
                    },
                },
                {
                    "Update": {
                        "TableName": settings.MARKETPLACE_PAYOUT_ACCOUNTS_TABLE,
                        "Key": {"seller_id": {"S": seller_id}},
                        "UpdateExpression": (
                            "SET balance_held_cents = if_not_exists(balance_held_cents, :zero) + :amt, "
                            "    last_balance_update_at = :now"
                        ),
                        "ExpressionAttributeValues": {
                            ":zero": {"N": "0"},
                            ":amt": {"N": str(seller_share_cents)},
                            ":now": {"S": now_iso},
                        },
                    },
                },
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# Payout accounts (PK seller_id)
# ---------------------------------------------------------------------------


async def get_payout_account(seller_id: str, *, consistent: bool = False) -> dict | None:
    """consistent=True: the payout flush reads this right after the
    fulfillment transaction credited balance_held_cents — an eventually
    consistent read can miss that credit and skip the flush entirely."""
    table = _payout_accounts_table()
    resp = await run_in_thread(table.get_item, Key={"seller_id": seller_id}, ConsistentRead=consistent)
    return resp.get("Item")


async def set_connect_account_if_absent(*, seller_id: str, account_id: str) -> bool:
    """Set-once write of the Stripe Connect account id (+ onboarding_status
    'started').

    update_item, not put_item: a concurrent checkout webhook may have
    written balance_held_cents between the caller's read and this write,
    and a blind put would clobber those fields back to stale values. The
    condition tolerates a re-write of the SAME account id; returns False
    when a parallel onboarding wrote a different id first -- the caller
    re-reads and uses the winner (we lose this account_id rather than
    overwriting the first).
    """
    table = _payout_accounts_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"seller_id": seller_id},
            UpdateExpression=("SET stripe_connect_account_id = :aid, onboarding_status = :started"),
            ConditionExpression=("attribute_not_exists(stripe_connect_account_id) OR stripe_connect_account_id = :aid"),
            ExpressionAttributeValues={":aid": account_id, ":started": "started"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def mark_payout_accounted(*, buyer_id: str, purchase_id: str) -> bool:
    """CLAIM a purchase's payout bookkeeping. Returns False when another run
    already claimed it.

    The flush settles per purchase: transfer stamp → this claim →
    apply_flush_totals. The claim is a set-once conditional because two
    overlapping flushes can both observe a row stamped-but-unaccounted and
    apply_flush_totals is a non-idempotent decrement — without ownership one
    purchase's share would be booked twice, consuming held credit belonging
    to another sale.
    """
    table = _purchases_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"buyer_id": buyer_id, "purchase_id": purchase_id},
            UpdateExpression="SET payout_accounted = :t",
            ConditionExpression="attribute_not_exists(payout_accounted)",
            ExpressionAttributeValues={":t": True},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


async def decrement_held_balance(*, seller_id: str, amount_cents: int) -> bool:
    """Decrement the held balance (pre-payout refund reconciliation).

    Returns False (no write) when the balance is already short of the
    amount -- likely a concurrent flush+refund race or a manual ops
    correction; the row is left alone rather than going negative.
    """
    table = _payout_accounts_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"seller_id": seller_id},
            UpdateExpression="SET balance_held_cents = balance_held_cents - :amt",
            ConditionExpression="balance_held_cents >= :amt",
            ExpressionAttributeValues={":amt": amount_cents},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


async def apply_flush_totals(*, seller_id: str, transferred_cents: int) -> None:
    """Post-flush bookkeeping: decrement (not zero) the held balance by the
    transferred amount -- a new purchase arriving mid-flush keeps its credit
    for the next flush -- and stamp onboarding completed + lifetime
    earnings.

    Conditional on the balance covering the decrement: a refund debit landing
    between this flush's read and write can leave the balance short of the
    transferred total, and an unconditional subtract would drive it negative
    (rendered as a negative dollar figure on the seller dashboard). A blind
    "set to zero" fallback is equally wrong — it could stomp a concurrent NEW
    SALE's credit (transact_record_purchase_and_credit_seller updates the same
    field unordered), silently discarding seller-owed money. So on conditional
    failure: consistent-read the current balance and clamp the decrement to
    what's actually there, retrying a couple of times (each retry re-reads, so
    a mid-race credit is respected). If contention persists the balance is
    left alone — a rare, slightly-inflated display — and the lifetime/
    onboarding stamps still land."""
    table = _payout_accounts_table()
    decrement = transferred_cents
    for _ in range(3):
        try:
            await run_in_thread(
                table.update_item,
                Key={"seller_id": seller_id},
                UpdateExpression=(
                    "SET balance_held_cents = balance_held_cents - :d, "
                    "    onboarding_status = :done, "
                    "    lifetime_earned_cents = if_not_exists(lifetime_earned_cents, :zero) + :t"
                ),
                ConditionExpression="balance_held_cents >= :d",
                ExpressionAttributeValues={
                    ":zero": 0,
                    ":done": "completed",
                    ":t": transferred_cents,
                    ":d": decrement,
                },
            )
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            resp = await run_in_thread(table.get_item, Key={"seller_id": seller_id}, ConsistentRead=True)
            current = int((resp.get("Item") or {}).get("balance_held_cents", 0))
            decrement = min(decrement, max(current, 0))
            if decrement <= 0:
                break
    # Contention exhausted (or nothing left to decrement): stamp the
    # non-balance fields only.
    await run_in_thread(
        table.update_item,
        Key={"seller_id": seller_id},
        UpdateExpression=(
            "SET onboarding_status = :done, "
            "    lifetime_earned_cents = if_not_exists(lifetime_earned_cents, :zero) + :t"
        ),
        ExpressionAttributeValues={":zero": 0, ":done": "completed", ":t": transferred_cents},
    )


# ---------------------------------------------------------------------------
# Takedowns (PK listing_id, SK takedown_id)
# ---------------------------------------------------------------------------


async def put_takedown(
    *,
    listing_id: str,
    takedown_id: str,
    reason: str,
    filed_by_name: str,
    filed_by_email: str,
    basis_md: str,
    now_iso: str,
) -> None:
    """Write a 'pending' takedown row.

    The grant stamp (decision/decided_by/decided_at/affected_purchases) is
    applied later by :func:`mark_takedown_granted` in a single update, so
    this put deliberately omits decision metadata -- avoiding a transient
    state where the row carries 'granted' but no affected_purchases.
    """
    table = _takedowns_table()
    await run_in_thread(
        table.put_item,
        Item={
            "listing_id": listing_id,
            "takedown_id": takedown_id,
            "reason": reason,
            "filed_by_name": filed_by_name,
            "filed_by_email": filed_by_email,
            "basis_md": basis_md,
            "filed_at": now_iso,
            "decision": "pending",
        },
    )


async def mark_takedown_granted(
    *,
    listing_id: str,
    takedown_id: str,
    decided_by: str,
    affected_purchases: int,
    now_iso: str,
) -> None:
    """Stamp the granted decision onto an existing takedown row.

    Guarded with attribute_exists so a malformed grant on a fabricated
    takedown_id can't upsert a partial row with no claimant fields (the row
    must have been written by :func:`put_takedown` first). ClientError
    propagates -- the service maps the conditional failure to its
    not-found error.
    """
    table = _takedowns_table()
    await run_in_thread(
        table.update_item,
        Key={"listing_id": listing_id, "takedown_id": takedown_id},
        UpdateExpression=("SET decision = :granted, decided_by = :by, decided_at = :now, affected_purchases = :n"),
        ConditionExpression="attribute_exists(takedown_id)",
        ExpressionAttributeValues={
            ":granted": "granted",
            ":by": decided_by,
            ":now": now_iso,
            ":n": affected_purchases,
        },
    )


async def scan_all_takedowns() -> list[dict]:
    """Full paginated scan of the takedowns table -- deliberately no Limit:
    DDB scans return items in unpredictable order, so a capped scan would
    yield "newest of an arbitrary subset" rather than the globally newest
    rows the audit view sorts for."""
    table = _takedowns_table()
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        resp = await run_in_thread(table.scan, **scan_kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last
    return items


# ---------------------------------------------------------------------------
# Sellers (PK seller_id; GSI handle-index)
# ---------------------------------------------------------------------------


async def get_seller_profile(seller_id: str) -> dict | None:
    """Profile row by seller_id, or None.

    A missing table (e.g. early after a CDK deploy) is benign -- surfaced
    as "no profile" so the rest of the app keeps working.
    """
    table = _sellers_table()
    try:
        resp = await run_in_thread(table.get_item, Key={"seller_id": seller_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return resp.get("Item")


async def find_seller_by_handle(handle: str) -> dict | None:
    """Profile row by handle via the handle-index GSI, or None. Same
    missing-table tolerance as :func:`get_seller_profile`."""
    table = _sellers_table()
    try:
        resp = await run_in_thread(
            table.query,
            IndexName="handle-index",
            KeyConditionExpression="handle = :h",
            ExpressionAttributeValues={":h": handle},
            Limit=1,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    items = resp.get("Items") or []
    return items[0] if items else None


async def put_seller_profile(item: dict) -> None:
    table = _sellers_table()
    await run_in_thread(table.put_item, Item=item)
