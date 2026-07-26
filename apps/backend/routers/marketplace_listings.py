"""Marketplace listings public + creator endpoints.

Browse + search are served by an in-process search service backed by a
60s-TTL DDB scan over published listings (see marketplace_search.py).
At v0 scale (Isol8-internal only) the listing count is small enough that
we don't need an external SaaS index.
"""

import asyncio
import json
import logging
from typing import Annotated

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from core.auth import (
    AuthContext,
    get_current_user,
    get_optional_user,
    require_org_admin,
    require_platform_admin,
    resolve_owner_id,
)
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.containers.ecs_manager import ULTRA_CPU, ULTRA_MEMORY_MIB
from core.repositories import billing_repo, marketplace_repo
from core.services import (
    agent_export,
    company_deploy_service,
    curator,
    marketplace_search,
    marketplace_service,
    seller_display,
    seller_profile,
)
from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS
from core.services.agent_routine_provisioning import create_agent_routines
from core.services.catalog_service import MissingCredentialsError, get_catalog_service, warn_rows
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_marketplace,
    delete_paperclip_for_marketplace,
)
from core.services.paperclip_meta import derive_from_listing, read_from_agent_entry
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    resolve_company_id_for_owner,
)
from schemas import marketplace as schemas


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])


def _count_field(field) -> int:
    """Count items in an agent config field that may be array- or object-shaped.

    Real agent configs (e.g. a published listing's ``openclaw.json`` slice)
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


async def _recreate_routines_for_buyer(
    *,
    owner_id: str,
    new_agent_id: str,
    manifest_routines: list,
) -> None:
    """Recreate the listing's captured routines on the buyer's side, DORMANT.

    Each manifest ``routines`` entry is ``{title, description, cron, timezone}``
    captured at publish (see ``agent_export.capture_agent_routines``). We remap
    the assignee to the buyer's freshly-minted ``new_agent_id`` and create each
    via ``create_agent_routines`` — which always attaches an ``enabled: False``
    schedule trigger. Nothing fires until the buyer flips it on.

    Called INSIDE the deploy's rollback-wrapped ``try``: a failure here
    propagates and triggers the existing ``delete_paperclip_for_marketplace``
    rollback, keeping deploys all-or-nothing.

    No-op when the listing carries no routines (legacy listings, or agents that
    had none). Resolving the buyer's company is required — if it's missing the
    buyer has no Paperclip company yet, which shouldn't happen post-mint, so we
    let the (rollback-wrapped) failure surface rather than silently drop.
    """
    if not manifest_routines:
        return
    specs = [
        {
            "title": r["title"],
            "description": r.get("description", ""),
            "cron": r["cron"],
        }
        for r in manifest_routines
        if isinstance(r, dict) and r.get("title") and r.get("cron")
    ]
    if not specs:
        return

    import httpx

    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id is None:
            raise RuntimeError(f"no paperclip company for owner {owner_id!r} — cannot recreate routines")
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        await create_agent_routines(
            admin=admin,
            session_cookie=cookie,
            company_id=company_id,
            agent_id=new_agent_id,  # remap: assigneeAgentId → buyer's minted agent
            routines=specs,
        )
    finally:
        await http.aclose()


_COMPACT_SUMMARY_MAX = 200


def _summary_of(listing: dict) -> str:
    """First prose line of ``description_md`` — skips headings and blanks.

    One line is enough for an agent to judge fit; the full body is a
    listing-detail fetch away for anyone who wants it.
    """
    for line in (listing.get("description_md") or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_COMPACT_SUMMARY_MAX]
    return ""


def _compact_items(items: list[dict]) -> list[dict]:
    """Agent-shaped projection: enough to judge fit, nothing else.

    Deliberately excludes ``manifest_json``, ``s3_prefix``, ``manifest_sha256``,
    safety flags and seller fields. ``manifest_json`` alone is the bulk of the
    ~50KB the default view returns for 8 listings — far more than an agent
    deciding "who could do content work?" can afford to read.

    Carries ``listing_id`` as well as ``slug``. The UUID is the canonical
    identity — **slugs are NOT unique** (see
    ``marketplace_service.get_published_by_id``) — so every downstream action
    (deploy, checkout) must address the listing by ``listing_id``. ``slug``
    stays because it is what an agent names in prose and what a human
    recognizes.

    Coerces null/absent ``tags`` and ``price_cents``: DynamoDB rows carry
    explicit nulls, and older listings predate several of these fields.
    """
    return [
        {
            "listing_id": item.get("listing_id", ""),
            "slug": item.get("slug", ""),
            "name": item.get("name", ""),
            "summary": _summary_of(item),
            "tags": item.get("tags") or [],
            "price_cents": item.get("price_cents") or 0,
        }
        for item in items
    ]


def _resolve_recommended(slugs: list[str], published: list[dict]) -> list[dict]:
    """Compact rows for ``slugs``, in the order asked, unknown ones dropped.

    Dropping rather than raising is deliberate: a model naming a listing that
    does not exist should produce no card, never a failed tool call that costs
    another model turn to recover from. Non-published rows are dropped for the
    same reason — the deploy route would 404 on them anyway.

    Deduplicates, so naming the same agent twice does not render two cards.
    """
    by_slug = {row.get("slug"): row for row in published if row.get("status") == "published"}
    seen: set[str] = set()
    ordered: list[dict] = []
    for slug in slugs:
        if slug in by_slug and slug not in seen:
            seen.add(slug)
            ordered.append(by_slug[slug])
    return _compact_items(ordered)


@router.get("/u/{owner_id}/recommend")
async def recommend(
    owner_id: str,
    request: Request,
    slugs: str = Query(description="Comma-separated listing slugs from marketplace_search."),
    issue_id: str | None = Query(
        default=None,
        description="Paperclip issue id — set only on headless runs, where nobody is watching chat.",
    ),
):
    """Resolve slugs for a hire recommendation. Container-authenticated.

    Read-only with respect to the roster. When ``issue_id`` is present this
    additionally files a PENDING Paperclip ``hire_agent`` approval on that
    issue, which still changes nothing until a human approves it in Teams.

    Owner is in the PATH and authorized by the per-container ``hook_token``,
    matching ``hooks_proxy`` and ``agent_builder``. Naming another owner does
    not grant access to them: the token compare is against THAT owner's row.
    """
    from routers.agent_builder import _require_hook_token

    await _require_hook_token(owner_id, request)

    wanted = [s.strip() for s in slugs.split(",") if s.strip()]
    published = await marketplace_search.search(query=None, limit=100)
    listings = _resolve_recommended(wanted, published)

    # ``issue_id`` is accepted and currently IGNORED — the headless path is not
    # switched on yet. ``marketplace_hire.create_hire_approval`` is complete and
    # correct, but two pieces still have to land with it (see its docstring):
    # the pre-create + pending_approval step that makes ``payload.agentId``
    # deterministic, and the post-approve artifact deploy in
    # ``routers/teams/approvals.py::approve``. Filing an approval without them
    # would leave a roster entry with no workspace behind it.
    #
    # A headless recommendation therefore degrades to "named in Friday's reply
    # and her issue comment", which still reaches the owner.
    return {"listings": listings, "approval_id": None}


@router.get("/listings/search")
async def search_listings(
    response: Response,
    auth: Annotated[AuthContext | None, Depends(get_optional_user)] = None,
    q: str | None = Query(default=None, description="Whitespace-tokenized query"),
    format: str | None = Query(default=None, pattern="^(openclaw|skillmd|company)$"),
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
    view: str | None = Query(
        default=None,
        pattern="^compact$",
        description=(
            "compact = agent-shaped projection (slug, name, summary, tags, price_cents)."
            " Omit for the full row the web surfaces consume."
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
    # cut.
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
    if view == "compact":
        # Agent-facing: skip the seller-name hydration entirely — it is an
        # extra round trip whose output the compact projection drops anyway.
        return {"items": _compact_items(items)}
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
    sidecar files at ``workspace-{agent_id}/.template`` written by
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


@router.get("/listings/{listing_id}")
async def get_listing(listing_id: str, response: Response):
    """Public listing detail.

    Returns ``{listing, manifest}``. The manifest is fetched from
    ``<s3_prefix>manifest.json`` so the storefront can render rich fields
    (emoji, vibe, suggested_model, required_skills, …) that aren't on the
    listing row. If the S3 fetch fails for any reason we log and return
    ``manifest: null`` — the storefront still has enough to render from
    the listing row alone.
    """
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
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
                "get_listing: failed to fetch manifest for listing_id=%s prefix=%s",
                listing_id,
                s3_prefix,
            )
            manifest = None

    return {"listing": listing, "manifest": manifest}


def _fetch_company_tar(*, s3_prefix: str, listing_id) -> bytes:
    """Fetch a company listing's ``workspace.tar.gz`` from S3.

    Mirrors the agent-deploy path's tarball fetch (same bucket + key shape,
    same sanitized ``ClientError`` -> 500 mapping so raw boto error text never
    reaches the caller). Factored out because the company branch returns 202
    before the agent path's inline fetch would run — the agent path itself is
    left byte-identical.
    """
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(
            Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
            Key=f"{s3_prefix}workspace.tar.gz",
        )
        return obj["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.exception(
            "Company deploy: S3 fetch failed for listing %s (code=%s)",
            listing_id,
            error_code,
        )
        if error_code == "NoSuchKey":
            raise HTTPException(status_code=500, detail="artifact missing for published listing") from exc
        raise HTTPException(status_code=500, detail="failed to fetch listing artifact") from exc


@router.post("/listings/{listing_id}/deploy")
async def deploy_listing(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    payload: schemas.DeployListingRequest | None = None,
):
    """One-click deploy: land a published listing into the buyer's container.

    Resolves the listing by listing_id, verifies entitlement (free → anyone
    signed in, paid → caller must hold a non-revoked marketplace-purchases
    row), fetches the artifact tarball + openclaw-slice from S3, and hands
    them to ``catalog_service.deploy_from_artifact`` which extracts files into
    the buyer's EFS at ``workspace-{agent_uuid}/`` and deep-merges the slice
    into their ``openclaw.json`` so the new agent auto-registers.

    Listings published before slice-synthesis shipped have an empty
    openclaw-slice.json (or no key at all on the older write path); the
    response's ``config_registered`` flag tells the caller which path ran.
    """
    require_org_admin(auth)
    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
    if not listing or listing.get("status") != "published":
        raise HTTPException(status_code=404, detail="listing not found")

    if listing.get("format") == "company":
        # COMPANY_MARKETPLACE_LAUNCH_GATE: company deploys are platform-admin-only
        # until the dev E2E validates the deploy engine. Enforced server-side, not
        # merely in the UI. Remove every COMPANY_MARKETPLACE_LAUNCH_GATE marker to
        # open the marketplace (deploy + Ultra upgrade) to buyers together.
        require_platform_admin(auth)

        owner_id = resolve_owner_id(auth)
        # min_plan is "standard" iff agent_count <= 10 (see company_package).
        # A company needing Ultra (min_plan != standard, or — belt-and-
        # suspenders — a raw agent_count over the standard cap should a manifest
        # disagree) may only be deployed by an ultra_pro owner. A standard-plan
        # owner still 409s; an ultra_pro owner passes and gets the large box
        # (the provision path sizes their container by plan).
        company = (listing.get("manifest_json") or {}).get("company") or {}
        agent_count = int(company.get("agent_count", 0) or 0)
        needs_ultra = company.get("min_plan") != "standard" or agent_count > COMPANY_STANDARD_PLAN_MAX_AGENTS
        if needs_ultra:
            account = await billing_repo.get_by_owner_id(owner_id)
            if (account or {}).get("plan") != "ultra_pro":
                raise HTTPException(status_code=409, detail="Requires Ultra Pro")

            # Size reconcile BEFORE dispatch. The Ultra upgrade webhook's
            # up-resize (standard -> 2 vCPU / 8 GB) is best-effort/swallowed;
            # if it failed transiently the owner is plan=ultra_pro but their
            # container is still the standard 1 vCPU / 4 GB box. Seeding a
            # >10-agent company onto that box OOMs. A resize restarts the
            # container, so we CANNOT resize mid-deploy — instead we compare
            # the running box's ACTUAL size against Ultra and, on a mismatch,
            # KICK the resize and refuse this deploy; the owner retries once it
            # is up. Only runs when needs_ultra — a standard box is always
            # adequate for a standard company.
            #
            # The desired size is the Ultra constants INLINE, not
            # _size_for_owner: the plan gate just above already confirmed
            # ultra_pro, and _size_for_owner is fail-open (a transient billing
            # read error there returns the STANDARD size, which would falsely
            # match the still-standard box and dispatch the big company onto
            # the 2 GB box — the exact OOM this guards). current_container_size
            # reads the real running size and does not touch billing.
            desired_cpu, desired_memory = ULTRA_CPU, ULTRA_MEMORY_MIB
            ecs = get_ecs_manager()
            if await ecs.current_container_size(owner_id) != (desired_cpu, desired_memory):
                await ecs.resize_user_container(owner_id, new_cpu=desired_cpu, new_memory=desired_memory)
                raise HTTPException(
                    status_code=409,
                    detail=("Your container is resizing to Ultra Pro — please retry the deploy in about a minute."),
                )

        s3_prefix = listing.get("s3_prefix")
        if not s3_prefix:
            raise HTTPException(status_code=500, detail="listing has no artifact prefix")
        tar_bytes = _fetch_company_tar(s3_prefix=s3_prefix, listing_id=listing.get("listing_id"))

        # Fire-and-forget: instantiate the whole package off the request path and
        # return 202 immediately. There is no job row — progress and the final
        # roster are observed in Teams (the Paperclip roster is the source of
        # truth). On failure the background task best-effort rolls back exactly
        # the agents it created this run (see company_deploy_service).
        asyncio.create_task(
            company_deploy_service.run_company_deploy(owner_id=owner_id, listing=listing, tar_bytes=tar_bytes)
        )
        return JSONResponse(status_code=202, content={"started": True, "agent_count": agent_count})

    price_cents = int(listing.get("price_cents", 0) or 0)
    # Seller-self-deploy bypass: sellers don't buy their own listings, so
    # they have no marketplace-purchases row pointing at their own
    # listing_id. Without this bypass a seller paying for a $10 listing
    # gets 403'd when they hit "Hire" on their own product — surfaced as
    # an indefinite "Setting up the workspace…" spinner in the storefront.
    # Free listings already skip the check; paid listings need the explicit
    # identity bypass.
    is_seller = listing.get("seller_id") == auth.user_id
    if (
        price_cents > 0
        and not is_seller
        and not await marketplace_repo.has_unrevoked_purchase(
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
    manifest.setdefault("name", listing.get("name", listing.get("slug")))

    catalog = get_catalog_service()
    owner_id = resolve_owner_id(auth)

    # Paperclip-first agent mint. Paperclip's create_agent returns an
    # agent UUID that we thread through to catalog_service as
    # new_agent_id, so OpenClaw and Paperclip share the same id. Mirror
    # of the /chat agents.create interception in websocket_chat.py and
    # the /teams create_agent route.
    #
    # Build PaperclipMeta from the slice. If the slice's agent entry has
    # ``params.paperclip``, use those values (seller explicitly tagged
    # the agent at publish time). Otherwise — typical for listings
    # published before the meta-roundtrip feature shipped — derive
    # title + capabilities from the listing fields so the buyer's
    # Paperclip profile is at least non-empty.
    slice_agent = slice_.get("agent") or {}
    has_paperclip_block = isinstance(slice_agent.get("params"), dict) and isinstance(
        slice_agent["params"].get("paperclip"), dict
    )
    if has_paperclip_block:
        meta = read_from_agent_entry(slice_agent)
    else:
        meta = derive_from_listing(
            name=listing.get("name"),
            description_md=listing.get("description_md"),
        )

    agent_name = slice_agent.get("name") or listing.get("name") or listing.get("slug")
    # Org placement: the buyer optionally picks who the new hire reports to
    # (its manager in the Paperclip org chart) at Hire time. None leaves it
    # unparented — one-click deploys with no body land here unchanged.
    reports_to = payload.reports_to if payload else None
    try:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=auth.user_id,
            owner_id=owner_id,
            name=agent_name,
            # meta.role drives — keeps the precedence rule consistent
            # with the chat path (see resolve_create_agent_fields).
            role=None,
            meta=meta,
            reports_to=reports_to,
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

    # Everything after the Paperclip mint must be wrapped so any
    # exception triggers the orphan-Paperclip rollback. That includes
    # container resolution (DDB/ECS can throw transient errors), the
    # 503-on-unreachable-container short-circuit, and the catalog deploy
    # itself. Without this envelope a transient DDB outage on
    # ``resolve_running_container`` leaves a phantom Paperclip agent on
    # every failed deploy.
    try:
        ecs = get_ecs_manager()
        container, container_ip = await ecs.resolve_running_container(owner_id)
        if container is None or container_ip is None:
            raise HTTPException(
                status_code=503,
                detail="container starting — retry in a moment",
            )
        container_token = container["gateway_token"]

        result = await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=listing.get("slug"),
            manifest=manifest,
            slice_=slice_,
            tar_bytes=tar_bytes,
            new_agent_id=new_agent_id,
            container_ip=container_ip,
            container_token=container_token,
        )

        # Register the deployed agent's operator device into the running
        # container's paired.json + bind its authToken to the gateway token, so
        # its first relayed connect isn't rejected NOT_PAIRED. catalog deploy
        # only RPC-registers the agent (agents.create/update); without this a
        # marketplace agent — which Paperclip wakes on events and runs through
        # the operator relay — can't pass the device-auth handshake until the
        # next reprovision. Mirrors the /teams and /chat create paths.
        # ``paperclip_agent`` (not the slice) carries adapterConfig.devicePrivateKeyPem.
        await ensure_operator_device_paired(
            owner_id=owner_id,
            paperclip_agent=paperclip_agent,
            gateway_token=container_token,
        )

        # Recreate the listing's captured routines (schedules) on the buyer,
        # remapped to the freshly-minted agent and DORMANT (enabled:false). A
        # failure here propagates into the rollback below — keeping the deploy
        # all-or-nothing — so it sits inside this same try.
        await _recreate_routines_for_buyer(
            owner_id=owner_id,
            new_agent_id=new_agent_id,
            manifest_routines=manifest.get("routines") or [],
        )
    except Exception as exc:
        # Catalog or container-resolution failed after Paperclip create
        # succeeded — roll back the orphan Paperclip agent. Idempotent
        # + 404-tolerant on the Paperclip side, so a retry doesn't
        # accumulate phantom records.
        logger.exception(
            "Marketplace deploy: post-Paperclip step failed for owner=%s listing=%s — rolling back paperclip agent %s",
            owner_id,
            listing.get("listing_id"),
            new_agent_id,
        )
        # Unwind the container/EFS side effects (openclaw.json agent entry +
        # on-EFS dirs) before deleting the Paperclip agent, so a failed Hire
        # leaves no orphan in the buyer's container — the half-installed
        # ``name=<uuid>`` ghost the incomplete rollback used to strand.
        # Self-contained best-effort; never raises.
        await catalog.unwind_partial_deploy(owner_id=owner_id, new_agent_id=new_agent_id)
        try:
            await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
        except Exception:
            # Roll-back is best-effort; the original exception is what
            # the caller needs to see.
            logger.exception(
                "Marketplace deploy: rollback delete_paperclip_for_marketplace failed for agent %s",
                new_agent_id,
            )
        # Preserve the original HTTP status — the 503 short-circuit
        # already carries the right code; only unexpected errors get
        # mapped to 500.
        if isinstance(exc, HTTPException):
            raise
        # Buyer hasn't connected a provider the agent hard-depends on. The
        # gate runs first inside deploy_from_artifact, so the orphan
        # Paperclip agent was already rolled back above. Surface the
        # provider list so the UI can route to the integrations panel.
        if isinstance(exc, MissingCredentialsError):
            raise HTTPException(
                status_code=409,
                detail={"error": "missing_credentials", "missing": exc.missing},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail="deploy failed",
        ) from exc

    # Surface the honest "these steps will skip" warnings at hire time: byo /
    # unavailable requirements the platform can't inject or verify. These never
    # block the deploy (the hard gate already ran inside deploy_from_artifact);
    # they let the buyer-facing UI tell the truth about what won't run.
    return {"agent_uuid": result["agent_id"], "warnings": warn_rows(manifest), **result}


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
    require_org_admin(auth)
    profile = await seller_profile.get_profile(auth.user_id)
    if not profile:
        raise HTTPException(status_code=409, detail="seller_profile_required")
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
    require_org_admin(auth)
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
    rejected v_new row instead of always targeting v=1.

    Requirements are disclosed (manifest + hire-time warnings), never gated:
    the publisher built the agent, an admin reviews before it goes live, and
    the buyer sees the ledger before hiring — so a publish is never blocked on
    the scanner's classification."""
    require_org_admin(auth)
    try:
        result = await marketplace_service.submit_for_review(
            listing_id=listing_id,
            seller_id=auth.user_id,
            version=version,
        )
    except marketplace_service.ArtifactNotUploadedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await seller_display.attach_seller_name(result)
    return result


@router.post("/listings/{listing_id}/republish", response_model=schemas.Listing)
async def republish(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create a new draft version cloning the currently-published row.

    Returns the new draft row at version=max(version)+1. The wizard then
    drives it through PATCH/artifact-from-agent/submit at that version.

    Guards: no-live-version -> 409; existing draft/review -> 409 naming
    the offending version; non-owner -> 403. Ownership is checked before
    the in-flight state inside the service so a non-owner probing a
    listing they don't own can't learn whether a republish is already
    underway (see create_new_version docstring).
    """
    require_org_admin(auth)
    try:
        row = await marketplace_service.create_new_version(
            listing_id=listing_id,
            seller_id=auth.user_id,
        )
    except marketplace_service.NoPublishedVersionError:
        raise HTTPException(status_code=409, detail="no live version to republish")
    except marketplace_service.RepublishAlreadyInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await seller_display.attach_seller_name(row)
    return row


@router.delete("/listings/{listing_id}", status_code=204)
async def discard_listing_version(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    version: int = Query(..., ge=2),
):
    """Discard a non-terminal draft version (v>=2).

    Used when the seller starts a republish then changes their mind, or
    when a v(N+1) sits as a rejected draft they no longer want to revise.
    The live v(N) row is untouched.
    """
    require_org_admin(auth)
    try:
        await marketplace_service.discard_draft(
            listing_id=listing_id,
            seller_id=auth.user_id,
            version=version,
        )
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return None


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

    # Resolve a legacy slug → live workspace UUID BEFORE the Friday guard, so a
    # slug like "friday" (which resolves to the CEO's workspace) can't slip past
    # the guard and expose Friday's owner-keyed persona files.
    try:
        resolved_agent_id = agent_export.resolve_workspace_agent_id(owner_id, agent_id)
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if await agent_export.is_reserved_friday_agent(
        owner_id=owner_id,
        agent_id=resolved_agent_id,
    ):
        raise HTTPException(
            status_code=400,
            detail=agent_export.FRIDAY_PUBLISH_ERROR_MSG,
        )

    try:
        preview = agent_export.enumerate_for_preview(
            seller_id=owner_id,
            agent_id=resolved_agent_id,
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
        auto_ship_skills=preview["auto_ship_skills"],
        persona_files=preview["persona_files"],
        safety_flags=[schemas.SafetyFlag(**f) for f in preview["safety_flags"]],
        requirements=[schemas.RequirementRow(**r) for r in preview.get("requirements", [])],
        scan_incomplete=preview.get("scan_incomplete", False),
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
    without a container can't get past this endpoint regardless.

    EFS workspace is keyed by owner_id (= org_id for org members), so
    the export must use the resolved id. The listing row's seller_id
    below stays as auth.user_id — that's the author/attribution field.
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)

    # Resolve a legacy slug → live workspace UUID up front so the Friday guard,
    # routine capture, export, and persisted row all key off the same resolved
    # id. This MUST precede the Friday guard: a slug like "friday" resolves to the
    # CEO's workspace, and the guard must see that resolved id or it would slip
    # past and leak Friday's owner-keyed persona files.
    try:
        resolved_agent_id = agent_export.resolve_workspace_agent_id(owner_id, payload.agent_id)
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Friday-publish guard: the per-owner CEO agent is reserved (its
    # IDENTITY/USER files are owner-keyed and Paperclip wires it into the
    # CEO slot). Run BEFORE the snapshot so we don't tar Friday's dir.
    if await agent_export.is_reserved_friday_agent(
        owner_id=owner_id,
        agent_id=resolved_agent_id,
    ):
        raise HTTPException(
            status_code=400,
            detail=agent_export.FRIDAY_PUBLISH_ERROR_MSG,
        )

    # Publish-dedupe: one agent = one listing. If this seller already has a
    # published listing for this agent under a DIFFERENT listing_id, they're
    # re-publishing the agent through the fresh-publish wizard (which minted a
    # new draft) rather than versioning the live listing. Block before the
    # expensive snapshot and hand the frontend the live listing_id so it can
    # redirect into republish-under-the-same-id. A legit republish targets the
    # same listing_id (create_new_version), so it never trips this.
    existing = await marketplace_service.find_active_listing_for_agent(
        seller_id=auth.user_id,
        agent_id=resolved_agent_id,
    )
    if existing and existing.get("listing_id") != listing_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "agent_already_published",
                "existing_listing_id": existing["listing_id"],
            },
        )

    selected_mcps = payload.bundle.mcps if payload.bundle else None
    selected_skills = payload.bundle.skills if payload.bundle else None

    try:
        package = agent_export.export_agent_from_efs(
            seller_id=owner_id,
            agent_id=resolved_agent_id,
            selected_mcps=selected_mcps,
            selected_skills=selected_skills,
            enable_acp=payload.enable_acp,
            requires_desktop=payload.requires_desktop,
        )
    except agent_export.InvalidAgentIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except agent_export.AgentNotInConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except agent_export.CredentialLeakError as e:
        # A credential survived the slice scrub — block the upload with a clear
        # 4xx naming the leaking field paths (never the secret) so the seller can
        # fix it, rather than a generic 500.
        raise HTTPException(status_code=400, detail=str(e))

    # Capture the agent's Paperclip routines (schedules) into the manifest. The
    # cron lives on the routine's schedule trigger, so this fetches each
    # routine's detail and strips it to {title, description, cron, timezone}.
    # Best-effort: a Paperclip-side failure logs and yields [] — it must never
    # block the publish. The deploy side recreates these DORMANT on the buyer.
    package.manifest["routines"] = await agent_export.capture_agent_routines(
        owner_id=owner_id,
        agent_id=resolved_agent_id,
    )

    try:
        result = await marketplace_service.replace_artifact(
            listing_id=listing_id,
            seller_id=auth.user_id,
            artifact_bytes=package.tarball_bytes,
            manifest=package.manifest,
            openclaw_slice=package.openclaw_slice,
            agent_id=resolved_agent_id,
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
