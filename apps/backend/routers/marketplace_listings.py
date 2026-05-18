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

from core.auth import AuthContext, get_current_user, get_optional_user, resolve_owner_id
from core.config import settings
from core.containers import get_workspace
from core.services import (
    agent_export,
    curator,
    marketplace_search,
    marketplace_service,
    seller_display,
    seller_profile,
)
from core.services.catalog_service import get_catalog_service
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_marketplace,
    delete_paperclip_for_marketplace,
)
from schemas import marketplace as schemas


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])


def _purchases_table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_PURCHASES_TABLE)


def _count_field(field) -> int:
    """Count items in an agent config field that may be array- or object-shaped.

    Real agent configs in this repo (e.g. ``agents/atlas/openclaw.json``)
    store ``skills`` and ``channels`` as plain arrays
    (``"skills": ["foo", "bar"]``). Older / hand-built configs use the
    object form ``{"list": [...]}``. Both must be supported — the previous
    ``(field or {}).get("list", [])`` form raised ``AttributeError`` on
    non-empty arrays because the ``or`` short-circuit doesn't fire.
    """
    if isinstance(field, list):
        return len(field)
    if isinstance(field, dict):
        return len(field.get("list", []) or [])
    return 0


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
    auth: Annotated[AuthContext | None, Depends(get_optional_user)] = None,
    q: str | None = Query(default=None, description="Whitespace-tokenized query"),
    format: str | None = Query(default=None, pattern="^(openclaw|skillmd)$"),
    limit: int = Query(default=24, ge=1, le=100),
    seller_id: str | None = Query(default=None),
    category: str | None = Query(
        default=None,
        description="Optional category filter, compared case-insensitively against listing.category.",
    ),
    status_in: str | None = Query(
        default=None,
        description=(
            "Comma-separated statuses (draft,review,published,retired,taken_down)."
            " Honored only when seller_id matches the calling user."
        ),
    ),
):
    """Browse / search listings.

    Default behavior (anonymous or non-seller caller): returns
    ``status='published'`` only. The chat drawer's My Listings tab calls
    with ``seller_id=<own user_id>`` and an authed token to receive
    its own draft/review/retired rows alongside published ones.

    Cached for 60s on the server (matches the search service's snapshot
    TTL) so CloudFront / browser caches don't outpace the underlying
    snapshot.
    """
    include_unpublished = (
        seller_id is not None and auth is not None and seller_id == auth.user_id and status_in is not None
    )
    if include_unpublished:
        # Seller-scoped reads can include drafts/review/retired rows that
        # must never be cached by a CDN — those are private to the seller.
        response.headers["Cache-Control"] = "private, max-age=0, no-store"
    else:
        response.headers["Cache-Control"] = "public, max-age=60"
    # Push status_in down to the search service so it's applied BEFORE the
    # limit slice — otherwise a seller with many published rows ahead of a
    # smaller status (e.g., a single review) loses the match to the limit
    # cut. See PR #567 review (Codex P2).
    status_list: list[str] | None = None
    if include_unpublished and status_in:
        status_list = [s.strip() for s in status_in.split(",") if s.strip()] or None
    items = await marketplace_search.search(
        query=q,
        format=format,
        limit=limit,
        seller_id=seller_id,
        category=category,
        include_unpublished=include_unpublished,
        status_in=status_list,
    )
    await seller_display.attach_seller_names(items)
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
    await seller_display.attach_seller_names(items)
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


@router.get("/me/agents-summary")
async def my_agents_summary(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Per-agent skill / channel / cron counts from the caller's
    workspace ``openclaw.json``.

    Drives the in-chat marketplace drawer's Publish wizard pick-card
    sublines (Step 1) and the bundled-summary line in the review preview
    (Step 4). Reads EFS directly via the workspace helper — no container
    interaction required, so it returns instantly even when the caller's
    container is stopped or scaled to zero.
    """
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    config = workspace.read_openclaw_config(owner_id) or {}
    agents = (config.get("agents") or {}).get("list", []) or []
    cron_jobs = workspace.read_cron_jobs(owner_id) or []
    return {
        "agents": [
            {
                "agent_id": a.get("id"),
                "name": (a.get("identity") or {}).get("name") or a.get("id"),
                "skill_count": _count_field(a.get("skills")),
                "channel_count": _count_field(a.get("channels")),
                "cron_count": sum(1 for c in cron_jobs if c.get("agent_id") == a.get("id")),
            }
            for a in agents
            if a.get("id")
        ],
    }


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

    await seller_display.attach_seller_name(listing)

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
    # Seller-self-deploy bypass: sellers don't buy their own listings, so
    # they have no marketplace-purchases row pointing at their own
    # listing_id. Without this bypass a seller paying for a $10 listing
    # gets 403'd when they hit "Hire" on their own product — surfaced as
    # an indefinite "Setting up the workspace…" spinner in the storefront
    # (PR #712 follow-up). Free listings already skip the check; paid
    # listings need the explicit identity bypass.
    is_seller = listing.get("seller_id") == auth.user_id
    if (
        price_cents > 0
        and not is_seller
        and not _has_valid_entitlement(
            buyer_id=auth.user_id,
            listing_id=listing["listing_id"],
        )
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

    # Paperclip-first agent mint. Paperclip's create_agent returns an
    # agent UUID that we thread through to catalog_service as
    # new_agent_id, so OpenClaw and Paperclip share the same id. Mirror
    # of the /chat agents.create interception in websocket_chat.py and
    # the /teams create_agent route.
    agent_name = (slice_.get("agent") or {}).get("name") or listing.get("name") or slug
    agent_role = (slice_.get("agent") or {}).get("role") or "general"
    try:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=auth.user_id,
            owner_id=owner_id,
            name=agent_name,
            role=agent_role,
        )
    except Exception as exc:
        logger.exception(
            "Marketplace deploy: Paperclip create failed for owner=%s listing=%s",
            owner_id,
            listing.get("listing_id"),
        )
        raise HTTPException(
            status_code=503,
            detail="paperclip unavailable — retry the install in a moment",
        ) from exc

    new_agent_id = paperclip_agent["id"]

    try:
        result = await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=slug,
            manifest=manifest,
            slice_=slice_,
            tar_bytes=tar_bytes,
            new_agent_id=new_agent_id,
        )
    except Exception as exc:
        # Catalog failed after Paperclip create succeeded — roll back
        # the orphan Paperclip agent. Idempotent + 404-tolerant on the
        # Paperclip side, so a retry doesn't accumulate phantom records.
        logger.exception(
            "Marketplace deploy: catalog.deploy_from_artifact failed for owner=%s slug=%s — rolling back paperclip agent %s",
            owner_id,
            slug,
            new_agent_id,
        )
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
        except Exception:
            # Roll-back is best-effort; the original exception is what
            # the caller needs to see.
            logger.exception(
                "Marketplace deploy: rollback delete_paperclip_for_marketplace failed for agent %s",
                new_agent_id,
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
    """Create a draft listing (requires authenticated seller).

    The caller must have a marketplace seller profile (handle) set —
    otherwise we return 409 ``seller_profile_required`` so the publish
    wizard can route them to the handle picker before retrying.
    """
    profile = await seller_profile.get_profile(auth.user_id)
    if not profile:
        raise HTTPException(status_code=409, detail="seller_profile_required")
    try:
        listing = await marketplace_service.create_draft(
            seller_id=auth.user_id,
            slug=payload.slug,
            name=payload.name,
            description_md=payload.description_md,
            format=payload.format,
            price_cents=payload.price_cents,
            tags=payload.tags,
            category=payload.category,
            artifact_bytes=b"",  # uploaded separately; this is the metadata create
            manifest={"name": payload.name, "description": payload.description_md},
        )
    except marketplace_service.SlugCollisionError:
        raise HTTPException(status_code=409, detail="slug already taken")
    await seller_display.attach_seller_name(listing)
    return listing


@router.patch("/listings/{listing_id}")
async def update_listing(
    listing_id: str,
    payload: schemas.ListingUpdate,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    version: int = Query(default=1, ge=1),
):
    """Patch metadata on a draft listing the caller owns.

    Slug and format are immutable post-creation — drop them from the
    payload schema rather than silently ignoring. Listings in 'review',
    'published', 'retired', or 'taken_down' state reject the call with
    409 (caller must wait for the moderation decision, or open a new
    version via republish).

    ``version`` defaults to 1 (today the only version that exists on the
    listings table) but accepts the listing's actual version so a future
    republish flow that produces a rejected v_new=2 row can be revised in
    place.
    """
    try:
        result = await marketplace_service.update_draft(
            listing_id=listing_id,
            seller_id=auth.user_id,
            name=payload.name,
            description_md=payload.description_md,
            price_cents=payload.price_cents,
            tags=payload.tags,
            category=payload.category,
            version=version,
        )
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await seller_display.attach_seller_name(result)
    return result


@router.post("/listings/{listing_id}/submit")
async def submit(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    version: int = Query(default=1, ge=1),
):
    """Transition draft -> review. ``version`` lets edit-mode resubmit a
    rejected v_new row instead of always targeting v=1."""
    try:
        result = await marketplace_service.submit_for_review(
            listing_id=listing_id, seller_id=auth.user_id, version=version
        )
    except marketplace_service.ArtifactNotUploadedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await seller_display.attach_seller_name(result)
    return result


# ---------------------------------------------------------------------------
# Artifact dry-run preview — drives the publish wizard's Bundle + Review
# steps so the seller sees exactly what will ship before submit.
# ---------------------------------------------------------------------------


@router.get(
    "/me/agents/{agent_id}/artifact-preview",
    response_model=schemas.ArtifactPreviewResponse,
)
async def my_agent_artifact_preview(
    agent_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Dry-run preview of the artifact the seller would publish for ``agent_id``.

    Snapshots the agent dir + scans (no S3, no DDB writes) so the wizard
    can render MCP / skill checkboxes with bucket tags, list persona files
    that will ship, and surface safety_flags inline. Friday-guarded the
    same way as the actual publish endpoint.
    """
    owner_id = resolve_owner_id(auth)

    if await agent_export.is_reserved_friday_agent(
        owner_id=owner_id,
        agent_id=agent_id,
    ):
        raise HTTPException(
            status_code=400,
            detail=agent_export.FRIDAY_PUBLISH_ERROR_MSG,
        )

    try:
        preview = agent_export.enumerate_for_preview(
            seller_id=owner_id,
            agent_id=agent_id,
        )
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except agent_export.AgentNotInConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # needs_config_patch flips True iff anything we'd merge into the
    # buyer's openclaw.json is present: an MCP exists at all OR a skill
    # in entries/bundled bucket is referenced. The wizard surfaces this
    # as a yellow warning on the Review step.
    needs_config_patch = bool(preview["available_mcps"]) or any(
        s["bucket"] in ("entries", "bundled") for s in preview["available_skills"]
    )

    return schemas.ArtifactPreviewResponse(
        agent_id=preview["agent_id"],
        available_mcps=preview["available_mcps"],
        available_skills=[
            schemas.AvailableSkill(name=s["name"], bucket=s["bucket"]) for s in preview["available_skills"]
        ],
        persona_files=preview["persona_files"],
        safety_flags=[schemas.SafetyFlag(**f) for f in preview["safety_flags"]],
        needs_config_patch=needs_config_patch,
    )


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
    version: int = Query(default=1, ge=1),
):
    """Snapshot one of the seller's existing OpenClaw agents.

    Body: ``{ agent_id }``. Reads ``/mnt/efs/users/{seller}/agents/{agent_id}/``
    directly from EFS (no container interaction), tars the directory, packs
    it as a CatalogPackage, and replaces the listing's artifact.

    Authentication is the only gate. The natural floor is "user has a
    provisioned container with a registered agent" — ``agent_export``
    surfaces ``AgentNotInConfigError`` (→ 400) when the seller has no
    openclaw.json or the agent_id isn't in their agents.list, so a user
    without a container can't get past this endpoint regardless. Tier-based
    publishing gates were retired in the post-2026-04-26 flat-fee cutover.

    EFS workspace is keyed by owner_id (= org_id for org members), so
    the export must use the resolved id. The listing row's seller_id
    below stays as auth.user_id — that's the author/attribution field.
    """
    owner_id = resolve_owner_id(auth)

    # Friday-publish guard: the per-owner CEO agent is reserved (its
    # IDENTITY/USER files are owner-keyed and Paperclip wires it into the
    # CEO slot). Run BEFORE the snapshot so we don't tar Friday's dir.
    if await agent_export.is_reserved_friday_agent(
        owner_id=owner_id,
        agent_id=payload.agent_id,
    ):
        raise HTTPException(
            status_code=400,
            detail=agent_export.FRIDAY_PUBLISH_ERROR_MSG,
        )

    selected_mcps = payload.bundle.mcps if payload.bundle else None
    selected_skills = payload.bundle.skills if payload.bundle else None

    try:
        package = agent_export.export_agent_from_efs(
            seller_id=owner_id,
            agent_id=payload.agent_id,
            selected_mcps=selected_mcps,
            selected_skills=selected_skills,
        )
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
            agent_id=payload.agent_id,
            version=version,
            bundle_mcps=selected_mcps,
            bundle_skills=selected_skills,
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
