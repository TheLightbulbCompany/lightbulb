"""Marketplace listings public + creator endpoints.

Browse + search are served by an in-process search service backed by a
60s-TTL DDB scan over published listings (see marketplace_search.py).
At v0 scale (Isol8-internal only) the listing count is small enough that
we don't need an external SaaS index.
"""

import json
import logging
from typing import Annotated

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo
from core.services import agent_export, curator, marketplace_search, marketplace_service
from core.services.catalog_service import get_catalog_service
from schemas import marketplace as schemas


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])


# Tier strings that grant openclaw publishing.
_PAID_TIERS = {"starter", "pro", "enterprise"}


def _purchases_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PURCHASES_TABLE)


def _has_valid_entitlement(*, buyer_id: str, listing_id: str) -> bool:
    """True iff caller has a non-revoked marketplace-purchases row for this listing.

    Uses the ``listing-created-index`` GSI (pk=listing_id, sk=created_at) so
    we scope by listing first. Per-listing buyer count is much smaller than
    per-buyer purchase count for power buyers (and is pre-sorted by
    created_at, newest first), so the matching row for *this* buyer is
    reached fast. Pages until we find a match or exhaust — bounded by
    num-buyers-of-this-listing × license_key_revoked-False, which stays
    small in practice.

    The previous implementation queried the buyer_id partition with
    Limit=200, which silently failed open at scale: a buyer with more than
    200 purchases ahead of the matching one (purchase_id is UUID-shaped, so
    effectively random sort order) would be told they had no entitlement.
    """
    table = _purchases_table()
    kwargs: dict = {
        "IndexName": "listing-created-index",
        "KeyConditionExpression": "listing_id = :l",
        "ExpressionAttributeValues": {":l": listing_id},
        "ScanIndexForward": False,  # newest first
    }
    while True:
        resp = table.query(**kwargs)
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


@router.get("/listings/search")
async def search_listings(
    response: Response,
    q: str | None = Query(default=None, description="Whitespace-tokenized query"),
    format: str | None = Query(default=None, pattern="^(openclaw|skillmd)$"),
    limit: int = Query(default=24, ge=1, le=100),
):
    """Browse / search published listings.

    Cached for 60s on the server (matches the search service's snapshot
    TTL) so CloudFront / browser caches don't outpace the underlying
    snapshot.
    """
    response.headers["Cache-Control"] = "public, max-age=60"
    items = await marketplace_search.search(query=q, format=format, limit=limit)
    return {"items": items}


@router.get("/featured-listings")
async def featured_listings(
    response: Response,
    limit: int = Query(default=24, ge=1, le=100),
    format: str = Query(default="openclaw", pattern="^(openclaw|skillmd)$"),
):
    """Curated listings — admin@isol8.co's published agents.

    The chat sidebar's "From the marketplace" gallery hits this. The curator
    publishes through the same /listings flow as any other seller; this
    endpoint just filters the published-listings snapshot down to whoever
    holds the configured curator email in Clerk. Returns an empty list
    when the curator account doesn't exist yet — the sidebar renders that
    as the empty-state.

    ``format`` defaults to ``openclaw`` rather than no-filter so the sidebar
    can never receive a non-agent listing the chat-side ``CatalogAgent``
    deploy flow can't honour (the only other format today is ``skillmd``,
    which was decommissioned in v0 — defending against any reintroduction).

    Lives outside the ``/listings/{slug}`` namespace so a seller can't
    create a listing with slug ``"featured"`` and shadow this route — the
    earlier ``/listings/featured`` placement would have made
    ``GET /listings/featured`` match this static route instead of the
    detail endpoint for that listing.
    """
    response.headers["Cache-Control"] = "public, max-age=60"
    curator_id = await curator.get_curator_user_id()
    if not curator_id:
        return {"items": []}
    items = await marketplace_search.search(query=None, format=format, limit=limit, seller_id=curator_id)
    return {"items": items}


@router.get("/me/installed-templates")
async def my_installed_templates(
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Templates the caller has already deployed into their container.

    Used by the chat sidebar gallery to filter out rows the user has
    already hired (so they don't appear as deployable). Backed by EFS
    sidecar files at ``workspaces/{agent_id}/.template`` written by
    ``catalog_service.deploy_from_artifact`` on every deploy. Replaces
    the legacy ``GET /catalog/deployed`` endpoint that was tied to the
    deleted admin-catalog router.
    """
    catalog_svc = get_catalog_service()
    return {"installed": catalog_svc.list_deployed_for_user(resolve_owner_id(auth))}


@router.get("/listings/{slug}")
async def get_listing(slug: str, response: Response):
    """Public listing detail.

    Returns ``{listing, manifest}``. The manifest is fetched from
    ``<s3_prefix>manifest.json`` so the storefront can render rich fields
    (emoji, vibe, suggested_model, required_skills, …) that aren't on the
    listing row. If the S3 fetch fails for any reason we log and return
    ``manifest: null`` — the storefront still has enough to render from
    the listing row alone.
    """
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    listing = await marketplace_service.get_by_slug(slug=slug)
    # Only published listings are publicly visible. The previous version
    # blacklisted retired/taken_down but allowed draft + review through,
    # leaking unmoderated content if someone guessed a slug. Whitelist
    # status="published" instead — same gate /deploy and /checkout enforce.
    if not listing or listing.get("status") != "published":
        raise HTTPException(status_code=404, detail="listing not found")

    manifest: dict | None = None
    s3_prefix = listing.get("s3_prefix")
    if s3_prefix:
        try:
            s3 = boto3.client("s3")
            obj = s3.get_object(
                Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
                Key=f"{s3_prefix}manifest.json",
            )
            manifest = json.loads(obj["Body"].read().decode("utf-8"))
            if not isinstance(manifest, dict):
                manifest = None
        except (ClientError, json.JSONDecodeError, UnicodeDecodeError):
            # NoSuchKey + transient S3 errors (ClientError), malformed JSON
            # (JSONDecodeError), and non-UTF8 bodies (UnicodeDecodeError) all
            # fall back to manifest=None — the storefront renders from the
            # listing row alone. Other exception types (KeyError, ImportError,
            # AttributeError, …) propagate so programming errors surface as
            # 500 rather than getting silently swallowed.
            logger.exception(
                "get_listing: failed to fetch manifest for slug=%s prefix=%s",
                slug,
                s3_prefix,
            )
            manifest = None

    return {"listing": listing, "manifest": manifest}


@router.post("/listings/{slug}/deploy")
async def deploy_listing(
    slug: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """One-click deploy: land a published listing into the buyer's container.

    Resolves the listing by slug, verifies entitlement (free → anyone signed
    in, paid → caller must hold a non-revoked marketplace-purchases row),
    fetches the artifact tarball + openclaw-slice from S3, and hands them to
    ``catalog_service.deploy_from_artifact`` which extracts files into the
    buyer's EFS at ``workspaces/{agent_uuid}/`` and deep-merges the slice
    into their ``openclaw.json`` so the new agent auto-registers.

    Listings published before slice-synthesis shipped have an empty
    openclaw-slice.json (or no key at all on the older write path); the
    response's ``config_registered`` flag tells the caller which path ran.
    """
    listing = await marketplace_service.get_by_slug(slug=slug)
    if not listing or listing.get("status") != "published":
        raise HTTPException(status_code=404, detail="listing not found")

    price_cents = int(listing.get("price_cents", 0) or 0)
    if price_cents > 0 and not _has_valid_entitlement(
        buyer_id=auth.user_id,
        listing_id=listing["listing_id"],
    ):
        raise HTTPException(
            status_code=403,
            detail="purchase required to deploy this listing",
        )

    # Fetch the artifact tarball + slice from S3. The listing's ``s3_prefix``
    # is written by ``marketplace_service._upload_artifact_to_s3`` and looks
    # like ``listings/{listing_id}/v{version}/``. We deliberately skip
    # reading manifest.json (the manifest stored on the listing row is
    # authoritative for slug/name/version).
    s3_prefix = listing.get("s3_prefix")
    if not s3_prefix:
        raise HTTPException(status_code=500, detail="listing has no artifact prefix")

    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(
            Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
            Key=f"{s3_prefix}workspace.tar.gz",
        )
        tar_bytes = obj["Body"].read()
    except ClientError as exc:
        # Sanitize boto error text out of the user-facing response — raw
        # ClientError messages can leak bucket names / role ARNs / etc.
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.exception(
            "Marketplace deploy: S3 fetch failed for listing %s (code=%s)",
            listing.get("listing_id"),
            error_code,
        )
        if error_code == "NoSuchKey":
            raise HTTPException(
                status_code=500,
                detail="artifact missing for published listing",
            ) from exc
        raise HTTPException(
            status_code=500,
            detail="failed to fetch listing artifact",
        ) from exc

    # Fetch the slice. NoSuchKey is tolerated for legacy listings published
    # before slice-synthesis shipped — those deploys land workspace files
    # without registering the agent in openclaw.json (legacy behavior).
    try:
        slice_obj = s3.get_object(
            Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
            Key=f"{s3_prefix}openclaw-slice.json",
        )
        slice_ = json.loads(slice_obj["Body"].read())
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            logger.info(
                "Marketplace deploy: legacy listing %s has no openclaw-slice; deploying without config registration",
                listing.get("listing_id"),
            )
            slice_ = {}
        else:
            logger.exception(
                "Marketplace deploy: slice fetch failed for listing %s",
                listing.get("listing_id"),
            )
            raise HTTPException(
                status_code=500,
                detail="failed to fetch listing slice",
            ) from exc

    # Manifest carried on the listing row (see _upload_artifact_to_s3 + the
    # listing's manifest_json column). The marketplace manifest doesn't
    # follow the admin catalog's schema — it uses {name, description,
    # format, exported_at, agent_id, file_count} — but
    # deploy_from_artifact only reads ``version`` and ``name`` for the
    # response payload, so passing through what we have is fine.
    manifest = listing.get("manifest_json") or {}
    manifest = dict(manifest)
    manifest.setdefault("version", listing.get("version", 1))
    manifest.setdefault("name", listing.get("name", slug))

    catalog = get_catalog_service()
    owner_id = resolve_owner_id(auth)
    try:
        result = await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=slug,
            manifest=manifest,
            slice_=slice_,
            tar_bytes=tar_bytes,
        )
    except Exception as exc:
        # deploy_from_artifact can fail in many ways (EFS write, container
        # config patch, malformed tar). Log the full traceback server-side
        # but never echo raw exception text back to the client.
        logger.exception(
            "Marketplace deploy: catalog.deploy_from_artifact failed for owner=%s slug=%s",
            owner_id,
            slug,
        )
        raise HTTPException(
            status_code=500,
            detail="deploy failed",
        ) from exc

    return {"agent_uuid": result["agent_id"], **result}


@router.post("/listings")
async def create_listing(
    payload: schemas.ListingCreate,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create a draft listing (requires authenticated seller)."""
    try:
        listing = await marketplace_service.create_draft(
            seller_id=auth.user_id,
            slug=payload.slug,
            name=payload.name,
            description_md=payload.description_md,
            format=payload.format,
            price_cents=payload.price_cents,
            tags=payload.tags,
            artifact_bytes=b"",  # uploaded separately; this is the metadata create
            manifest={"name": payload.name, "description": payload.description_md},
        )
    except marketplace_service.SlugCollisionError:
        raise HTTPException(status_code=409, detail="slug already taken")
    return listing


@router.post("/listings/{listing_id}/submit")
async def submit(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Transition draft -> review."""
    try:
        result = await marketplace_service.submit_for_review(listing_id=listing_id, seller_id=auth.user_id)
    except marketplace_service.ArtifactNotUploadedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return result


# ---------------------------------------------------------------------------
# Artifact upload — Path B (snapshot from EFS); Path A (zip upload) is
# explicitly out of v0 scope per the Isol8-internal reduction.
# ---------------------------------------------------------------------------


@router.post(
    "/listings/{listing_id}/artifact-from-agent",
    response_model=schemas.ArtifactUploadResponse,
)
async def upload_artifact_from_agent(
    listing_id: str,
    payload: schemas.ArtifactFromAgentRequest,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Snapshot one of the seller's existing OpenClaw agents.

    Body: ``{ agent_id }``. Reads ``/mnt/efs/users/{seller}/agents/{agent_id}/``
    directly from EFS (no container interaction), tars the directory, packs
    it as a CatalogPackage, and replaces the listing's artifact.

    Requires the seller to be on a paid tier (free-tier users have no
    container, so no agents on EFS to publish).
    """
    account = await billing_repo.get_by_owner_id(auth.user_id)
    tier = (account or {}).get("tier", "none")
    if tier not in _PAID_TIERS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "publishing OpenClaw agents requires Isol8 Starter, Pro, or Enterprise",
                "current_tier": tier,
                "upgrade_url": "https://isol8.co/pricing",
            },
        )

    try:
        package = agent_export.export_agent_from_efs(seller_id=auth.user_id, agent_id=payload.agent_id)
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except agent_export.AgentNotInConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await marketplace_service.replace_artifact(
            listing_id=listing_id,
            seller_id=auth.user_id,
            artifact_bytes=package.tarball_bytes,
            manifest=package.manifest,
            openclaw_slice=package.openclaw_slice,
        )
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return schemas.ArtifactUploadResponse(
        listing_id=result["listing_id"],
        version=result["version"],
        manifest_sha256=result["manifest_sha256"],
        file_count=len(package.tarball_contents),
        bytes=len(package.tarball_bytes),
    )


# ---------------------------------------------------------------------------
# Seller surfacing — tier gate
# ---------------------------------------------------------------------------


@router.get("/seller-eligibility", response_model=schemas.SellerEligibilityResponse)
async def seller_eligibility(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Surface whether the caller can publish each format.

    Cheap (single billing repo lookup); UI calls it on every mount so a
    user who upgrades mid-session sees the new option immediately.
    """
    account = await billing_repo.get_by_owner_id(auth.user_id)
    tier = (account or {}).get("tier", "none")
    can_openclaw = tier in _PAID_TIERS
    return schemas.SellerEligibilityResponse(
        tier=tier,
        can_sell_skillmd=False,  # skillmd path removed in Isol8-internal v0 reduction
        can_sell_openclaw=can_openclaw,
        reason=None if can_openclaw else "Publishing requires an Isol8 Starter, Pro, or Enterprise subscription.",
    )
