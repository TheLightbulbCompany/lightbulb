"""Marketplace listings service.

Wraps catalog_service for packaging; adds:
  - listing-level metadata (price, seller, status)
  - state machine (draft -> review -> published -> retired/taken_down)
  - v2 publish via DynamoDB TransactWriteItems for atomicity (atomically
    flips v_new -> 'published' and v_prev -> 'retired' so only one row
    per listing_id is ever 'published')

Single-table design: every version is a row in `listings` keyed by
(listing_id, version). The version-history requirement is satisfied by
the rows themselves — older versions stay in the table with status
'retired'. No separate immutable history table.
"""

import hashlib
import json
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from core.config import settings


def _listings_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)


def _dynamodb_client():
    return boto3.client("dynamodb")


class InvalidStateError(Exception):
    """Listing is not in the state required for the operation."""


class SlugCollisionError(Exception):
    """Another listing already owns this slug."""


class ArtifactNotUploadedError(Exception):
    """Seller called submit_for_review on a draft whose artifact bytes
    were never replaced. create_draft writes a metadata-only artifact
    (artifact_uploaded=False); the seller must call replace_artifact
    via /artifact-from-agent before submit_for_review will accept the
    draft.
    """


async def _upload_artifact_to_s3(
    *,
    listing_id: str,
    version: int,
    artifact_bytes: bytes,
    manifest: dict,
    openclaw_slice: dict | None = None,
) -> tuple[str, str]:
    """Upload tarball + manifest + openclaw-slice to the marketplace bucket.
    Returns (s3_prefix, manifest_sha256).

    ``openclaw_slice`` carries the seller's agent entry + plugins/tools/cron
    sliced from their per-user openclaw.json. Buyers' deploy_from_artifact
    deep-merges it into their own openclaw.json so the new agent
    auto-registers (matching the admin-catalog publish path). Pass ``None``
    or ``{}`` for metadata-only drafts before the seller has uploaded a
    real artifact.
    """
    s3 = boto3.client("s3")
    bucket = settings.MARKETPLACE_ARTIFACTS_BUCKET
    prefix = f"listings/{listing_id}/v{version}/"
    s3.put_object(Bucket=bucket, Key=f"{prefix}workspace.tar.gz", Body=artifact_bytes)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=f"{prefix}manifest.json", Body=manifest_bytes)
    slice_bytes = json.dumps(openclaw_slice or {}, sort_keys=True).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=f"{prefix}openclaw-slice.json", Body=slice_bytes)
    sha = hashlib.sha256(manifest_bytes).hexdigest()
    return prefix, sha


async def create_draft(
    *,
    seller_id: str,
    slug: str,
    name: str,
    description_md: str,
    format: str,
    price_cents: int,
    tags: list[str],
    artifact_bytes: bytes,
    manifest: dict,
    openclaw_slice: dict | None = None,
    category: str | None = None,
) -> dict:
    """Create a new listing in draft state. Slug must be unique.

    Uses a TransactWriteItems with a slug-reservation row to enforce
    uniqueness atomically — a read-then-put has a race window where two
    concurrent create_draft calls for the same slug both pass the
    pre-check and both insert. The reservation row uses listing_id=
    "slug:{slug}" + version=0 (sentinel) so it sits in the same table
    but doesn't appear in any real-listing query (those filter version >= 1).

    ``openclaw_slice`` is optional — metadata-only drafts (created via the
    /listings POST route) pass nothing because the artifact is uploaded in
    a follow-up call. Real artifacts populate it via replace_artifact.
    """
    listing_id = str(uuid.uuid4())
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s3_prefix, sha = await _upload_artifact_to_s3(
        listing_id=listing_id,
        version=1,
        artifact_bytes=artifact_bytes,
        manifest=manifest,
        openclaw_slice=openclaw_slice,
    )

    listings_tbl = settings.MARKETPLACE_LISTINGS_TABLE
    client = _dynamodb_client()
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": listings_tbl,
                        "Item": {
                            "listing_id": {"S": f"slug:{slug}"},
                            "version": {"N": "0"},
                            "slug": {"S": slug},
                            "owner_listing_id": {"S": listing_id},
                            "reserved_at": {"S": now_iso},
                        },
                        "ConditionExpression": "attribute_not_exists(listing_id)",
                    },
                },
                {
                    "Put": {
                        "TableName": listings_tbl,
                        "Item": {
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
                            "manifest_sha256": {"S": sha},
                            "manifest_json": {"S": json.dumps(manifest)},
                            "artifact_format_version": {"S": "v1"},
                            "entitlement_policy": {"S": "perpetual"},
                            "artifact_uploaded": {"BOOL": False},
                            "created_at": {"S": now_iso},
                            "updated_at": {"S": now_iso},
                            # published_at is NULL until publish (resp. submit
                            # populates it for sparse-GSI visibility).
                        },
                    },
                },
            ]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise SlugCollisionError(f"slug '{slug}' is taken")
        raise

    # Return the listing item shape the caller expects (matching the
    # pre-TransactWrite dict literal — manifest is the original dict, not
    # the JSON-serialized form stored in DDB).
    return {
        "listing_id": listing_id,
        "version": 1,
        "slug": slug,
        "name": name,
        "description_md": description_md,
        "format": format,
        "price_cents": price_cents,
        "tags": tags,
        "category": category,
        "seller_id": seller_id,
        "status": "draft",
        "s3_prefix": s3_prefix,
        "manifest_sha256": sha,
        "manifest_json": manifest,
        "artifact_format_version": "v1",
        "entitlement_policy": "perpetual",
        "artifact_uploaded": False,
        "created_at": now_iso,
        "updated_at": now_iso,
        "published_at": None,
    }


async def replace_artifact(
    *,
    listing_id: str,
    seller_id: str,
    artifact_bytes: bytes,
    manifest: dict,
    openclaw_slice: dict | None = None,
    agent_id: str | None = None,
    version: int = 1,
    bundle_mcps: list[str] | None = None,
    bundle_skills: list[str] | None = None,
) -> dict:
    """Replace the artifact for a draft listing.

    Conditional on (seller_id matches the listing's seller) AND
    (status='draft'). Re-uploads to the same S3 prefix so the existing
    version is overwritten — artifacts are mutable until the listing
    first transitions out of draft.

    ``version`` defaults to 1 to match the create flow (newly-created
    listings only ever have v=1). Edit-mode callers pass the rejected
    row's version so a future republish flow that yields a rejected
    v_new=2 row can still be revised in place.

    ``openclaw_slice`` is the seller's agent slice synthesized at export
    time (see ``agent_export.export_agent_from_efs``). Persisted to S3 at
    ``{prefix}openclaw-slice.json`` so deploy_listing can fetch it and
    feed it to ``catalog_service.deploy_from_artifact`` for buyer-side
    config registration.

    Returns:
        Dict with the updated listing fields: listing_id, version,
        manifest_sha256, file_count, bytes.

    Raises:
        InvalidStateError: Listing is not in draft state, or caller is
        not the seller.
    """
    table = _listings_table()
    # Read first to verify ownership + state. We can't use a conditional
    # update for the upload because the S3 write happens before the DDB
    # update; verifying ownership upfront keeps the failure mode tight.
    resp = table.get_item(Key={"listing_id": listing_id, "version": version})
    item = resp.get("Item")
    if not item:
        raise InvalidStateError("listing not found")
    if item.get("seller_id") != seller_id:
        raise InvalidStateError("you are not the seller of this listing")
    if item.get("status") != "draft":
        raise InvalidStateError("artifact can only be replaced while in 'draft' state")

    s3_prefix, sha = await _upload_artifact_to_s3(
        listing_id=listing_id,
        version=version,
        artifact_bytes=artifact_bytes,
        manifest=manifest,
        openclaw_slice=openclaw_slice,
    )
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # agent_id is persisted as a top-level column so the publish-wizard's
    # edit mode can lock the agent picker without parsing manifest_json.
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
    # Persist the bundle selection so edit mode can prefill the BundleStep
    # checkboxes instead of falsely defaulting to "ship nothing" — without
    # this, a seller revising metadata silently strips integrations buyers
    # would have received. Stored as `bundle_mcps` / `bundle_skills` to
    # match the request-body shape; surfaced through marketplace_search's
    # raw-row response so the frontend has them on hand.
    if bundle_mcps is not None:
        set_clause += ", bundle_mcps = :bmcps"
        values[":bmcps"] = bundle_mcps
    if bundle_skills is not None:
        set_clause += ", bundle_skills = :bskills"
        values[":bskills"] = bundle_skills
    table.update_item(
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression=set_clause,
        # Defense-in-depth: re-check seller match + draft state via condition
        # in case state changed between the get and update.
        ConditionExpression="seller_id = :sid AND #s = :draft",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )

    return {
        "listing_id": listing_id,
        "version": version,
        "manifest_sha256": sha,
        "file_count": len(manifest.get("contents", [])) or manifest.get("file_count", 0),
        "bytes": len(artifact_bytes),
    }


async def update_draft(
    *,
    listing_id: str,
    seller_id: str,
    name: str | None = None,
    description_md: str | None = None,
    price_cents: int | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    version: int = 1,
) -> dict:
    """Patch metadata on a draft listing. Slug / format / agent stay locked.

    All keyword args are optional; only those passed are written. Guarded
    on (seller_id matches) AND (status='draft') so a rejected listing can
    be revised (rejection flips status back to 'draft') but a published or
    in-review listing cannot. Returns the updated item.

    Raises:
        InvalidStateError: caller is not the seller, listing isn't in
        draft state, or no fields were provided.
    """
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if description_md is not None:
        updates["description_md"] = description_md
    if price_cents is not None:
        updates["price_cents"] = price_cents
    if tags is not None:
        updates["tags"] = tags
    if category is not None:
        updates["category"] = category
    if not updates:
        raise InvalidStateError("no fields to update")

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # name is a DDB reserved word; route every column through #-aliases so
    # the SET clause is uniform and immune to future reserved-word additions.
    set_clauses: list[str] = []
    expr_names: dict[str, str] = {"#s": "status"}
    expr_values: dict[str, object] = {
        ":sid": seller_id,
        ":draft": "draft",
        ":now": now_iso,
    }
    for i, (col, val) in enumerate(updates.items()):
        name_placeholder = f"#c{i}"
        value_placeholder = f":c{i}"
        expr_names[name_placeholder] = col
        expr_values[value_placeholder] = val
        set_clauses.append(f"{name_placeholder} = {value_placeholder}")
    set_clauses.append("updated_at = :now")

    table = _listings_table()
    try:
        resp = table.update_item(
            Key={"listing_id": listing_id, "version": version},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="seller_id = :sid AND #s = :draft",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise InvalidStateError("listing is not in 'draft' state or you are not the seller")
        raise
    return resp.get("Attributes", {})


async def submit_for_review(*, listing_id: str, seller_id: str, version: int = 1) -> dict:
    """Transition draft -> review. Idempotent: re-submitting from review is rejected.

    ``version`` defaults to 1 to match the create flow. Edit-mode callers
    pass the rejected row's version so re-submitting a v_new=2 row (a
    future republish path) targets the same row instead of v=1.

    Precondition: artifact_uploaded must be True. Listings created via
    create_draft start with artifact_uploaded=False; the seller must
    call replace_artifact via /artifact-from-agent before submit_for_review
    will accept the draft. Without this guard, sellers could submit a
    metadata-only draft with an empty workspace.tar.gz and buyers would
    get an empty install.
    """
    table = _listings_table()

    # Pre-check artifact_uploaded so we can return a specific error.
    # Listings created before this field existed default to True (legacy
    # safety) — the field is only stored for newly-created drafts.
    pre = table.get_item(Key={"listing_id": listing_id, "version": version})
    pre_item = pre.get("Item") or {}
    if pre_item.get("artifact_uploaded") is False:
        raise ArtifactNotUploadedError("upload artifact before submitting for review")

    try:
        # Populate published_at at submit time even though the listing isn't
        # yet "published". status-published-index has published_at as its
        # sort key; DynamoDB's sparse-GSI semantics exclude items whose sort
        # key is missing. Without a value here the moderation queue
        # (status="review") is structurally empty regardless of how many
        # listings are awaiting review. The approve flow overwrites this on
        # the actual publish transition, so the field still tells you when
        # the listing went live (or when it last entered the review pipeline,
        # if rejected).
        # REMOVE every moderation field so a resubmitted listing doesn't
        # carry "rejected because X (by admin_xyz)" into review (and from
        # there, if approved, onto the public storefront — marketplace_search
        # returns raw listing rows, so anything left on the item leaks to
        # buyers). The audit trail of who rejected what lives in the
        # admin_actions table, not on the listing row.
        resp = table.update_item(
            Key={"listing_id": listing_id, "version": version},
            UpdateExpression=(
                "SET #s = :review, updated_at = :now, published_at = :now "
                "REMOVE rejection_notes, rejected_at, rejected_by"
            ),
            ConditionExpression="seller_id = :sid AND #s = :draft",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":sid": seller_id,
                ":draft": "draft",
                ":review": "review",
                ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise InvalidStateError("listing is not in 'draft' state or you are not the seller")
        raise
    return resp.get("Attributes", {})


async def approve(*, listing_id: str, version: int, approved_by: str) -> dict:
    """Admin transition: review -> published. Sets published_at + published_by.

    Used for the FIRST publish of a listing (v=1). For subsequent versions
    that need to atomically retire the previous published version, callers
    use publish_v2 instead.
    """
    table = _listings_table()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        resp = table.update_item(
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
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise InvalidStateError(f"listing v{version} is not in 'review' state")
        raise
    return resp.get("Attributes", {})


async def reject(*, listing_id: str, version: int, notes: str, rejected_by: str) -> dict:
    """Admin transition: review -> draft with rejection notes.

    Conditional on status='review' so the call cannot upsert a fabricated
    listing (DDB UpdateItem upserts by default), nor flip a published or
    taken_down row back to draft via a malformed admin request.
    """
    table = _listings_table()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        table.update_item(
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
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise InvalidStateError(f"listing v{version} is not in 'review' state")
        raise
    return {"status": "draft", "rejection_notes": notes, "rejected_at": now_iso}


async def publish_v2(
    *,
    listing_id: str,
    prev_version: int,
    new_version: int,
    approved_by: str,
) -> None:
    """Atomic flip: prev_version -> 'retired', new_version -> 'published'.

    Both rows live in the listings table (single-table design). The
    TransactWriteItems guarantees no observable state where two versions
    of the same listing_id are simultaneously 'published'.

    Preconditions (caller responsibility):
    - The new_version row exists in the listings table with status='review'
      (write it via create_draft-style insert before calling).
    - The prev_version row currently has status='published'.

    Conditional checks fail loudly if either precondition is violated.
    """
    client = _dynamodb_client()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    listings_tbl = settings.MARKETPLACE_LISTINGS_TABLE
    try:
        client.transact_write_items(
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
                        "UpdateExpression": (
                            "SET #s = :pub, published_at = :now, published_by = :by, updated_at = :now"
                        ),
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":review": {"S": "review"},
                            ":pub": {"S": "published"},
                            ":now": {"S": now_iso},
                            ":by": {"S": approved_by},
                        },
                    },
                },
            ]
        )
    except ClientError as e:
        # TransactionCanceledException fires when either ConditionExpression
        # fails (prev_version not 'published' or new_version not 'review').
        # Surface as InvalidStateError so the route translates it to 409
        # rather than letting a raw ClientError become a 500.
        if e.response["Error"]["Code"] in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            raise InvalidStateError(
                f"publish_v2 conditional check failed for listing {listing_id} "
                f"(prev v{prev_version} or new v{new_version} not in expected state)"
            )
        raise


async def get_by_slug(*, slug: str) -> dict | None:
    """Look up listing by slug.

    Returns the highest-version row that is currently published, falling back
    to the highest-version row of any status if nothing is published yet.

    Why two-tier: during the publish_v2 flow, a v_new row exists in
    'review' state while v_prev is still 'published'. The downstream
    public/deploy/checkout routes all gate on status=='published', so
    returning the highest version unconditionally would 404 the still-live
    v_prev for the duration of moderation. The fallback preserves the
    original behavior for admin/seller paths that need to see the latest
    row regardless of state (rendering "draft", "pending review", etc.).
    """
    table = _listings_table()
    resp = table.query(
        IndexName="slug-version-index",
        # version > 0 excludes the slug-reservation sentinel row written by
        # create_draft (listing_id="slug:…", version=0). Real listings start
        # at version=1.
        KeyConditionExpression="slug = :s AND version > :zero",
        ExpressionAttributeValues={":s": slug, ":zero": 0},
        ScanIndexForward=False,  # newest version first
    )
    items = resp.get("Items", [])
    if not items:
        return None
    for item in items:
        if item.get("status") == "published":
            return item
    return items[0]


async def get_by_id(*, listing_id: str, version: int) -> dict | None:
    """Look up listing by id + version."""
    table = _listings_table()
    resp = table.get_item(Key={"listing_id": listing_id, "version": version})
    return resp.get("Item")
