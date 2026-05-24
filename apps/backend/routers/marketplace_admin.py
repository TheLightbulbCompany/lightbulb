"""Admin moderation endpoints — approve, reject, takedown, preview."""

import io
import json
import tarfile
from typing import Annotated, Literal

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, require_platform_admin
from core.config import settings
from core.services import marketplace_safety, marketplace_search, marketplace_service, takedown_service
from core.services.admin_audit import audit_admin_action
from schemas import marketplace as schemas


# Valid listing statuses surfaced to the admin browse view. Anything outside
# this set is rejected at the router boundary — keeps a typo in the URL bar
# from quietly returning [] (which would look like "no listings exist").
_ALLOWED_STATUSES = {"draft", "review", "published", "retired", "taken_down"}
# Server-side ceiling on the limit query param. Larger values would scan a
# disproportionate slice of the listings table in marketplace_search.search().
_MAX_LISTINGS_LIMIT = 200


router = APIRouter(prefix="/api/v1/admin/marketplace", tags=["marketplace-admin"])

# Cap on tarball size for the in-memory preview extract.
#
# 50 MB is a realistic ceiling for an agent workspace once persona files,
# bundled skills, and a couple of MCPs are included. A tighter cap would
# trip on legitimate listings carrying normal recipe content. Hitting this
# cap no longer 413s the endpoint; instead the preview returns a partial
# response with the file tree intact and ``scan_incomplete=True`` so the
# admin can still see WHAT is in the tarball and decide to reject.
# Approval is disabled client-side when scan_incomplete is set — see
# ModerationActions.
_MAX_PREVIEW_TARBALL_BYTES = 50 * 1024 * 1024


@router.get("/listings")
@audit_admin_action(
    "marketplace.list_listings",
    target_user_id_override="__marketplace__",
    capture_params=["status", "q", "limit"],
)
async def list_listings(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
):
    """List / browse marketplace listings across all statuses.

    Two paths:

    * **No params** — backwards-compatible review queue. Queries the
      ``status-published-index`` GSI directly for ``status="review"`` rows and
      returns up to 50 newest-first. This is the original ``review_queue``
      behavior; any pre-existing caller (the admin review page, scripts, etc.)
      that hits ``GET /admin/marketplace/listings`` with no query string
      gets the same response shape it always did.
    * **``status`` and/or ``q`` provided** — admin browse / search. Delegates
      to :func:`core.services.marketplace_search.search` with
      ``include_unpublished=True`` so drafts, retired, and taken-down rows are
      visible (the helper's public-vs-seller gate would otherwise hide them).
      ``status_in`` is set from the validated ``status`` param when provided
      so the search service narrows the candidate set before scoring; ``q``
      drives the same tokenized scoring used elsewhere.

    ``limit`` is clamped at 200 server-side to avoid DDB scan blowups —
    ``marketplace_search`` does an unfiltered scan and an unbounded ``limit``
    would scan the whole table for every typo'd URL.

    Auth: ``require_platform_admin`` — only platform admins may browse all
    statuses (drafts contain seller-private content).
    """
    # Clamp before passing to search(). The audit decorator captures the
    # raw kwargs (pre-clamp) so abuse attempts like ?limit=10000 stay
    # visible in the audit log — only the actual DDB read is bounded.
    limit = max(1, min(limit, _MAX_LISTINGS_LIMIT))

    # Validate status against the closed set so a typo in the URL bar fails
    # loudly with 422 rather than silently returning an empty list (which an
    # admin could read as "no listings in that state").
    if status is not None and status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_ALLOWED_STATUSES)}, got {status!r}",
        )

    # Backwards-compat fast path: no params means "give me the review queue"
    # via the same GSI query the old endpoint used. Keeps the existing review
    # page working without a code change and avoids paying the search-service
    # scan cost on the default load.
    if status is None and not q:
        table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
        resp = table.query(
            IndexName="status-published-index",
            KeyConditionExpression="#s = :review",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":review": "review"},
            Limit=50,
        )
        return {"items": resp.get("Items", [])}

    # Browse / search path. include_unpublished=True bypasses the helper's
    # public-vs-seller gate so admins see every status; status_in narrows
    # further when the admin picks a specific status pill. When no pill is
    # selected we still pass the full real-status whitelist — otherwise
    # the listings table's slug-reservation rows (``listing_id="slug:..."``,
    # ``version=0``, no status field) leak into search results and the
    # admin clicks through to a malformed detail page.
    items = await marketplace_search.search(
        query=q,
        limit=limit,
        seller_id=None,
        include_unpublished=True,
        status_in=[status] if status else list(_ALLOWED_STATUSES),
    )
    return {"items": items}


@router.post("/listings/{listing_id}/approve")
@audit_admin_action(
    "marketplace.approve",
    target_user_id_override="__marketplace__",
    capture_params=["listing_id", "version", "prev_version"],
)
async def approve(
    listing_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
    version: int = 1,
    prev_version: int | None = None,
):
    """Approve a listing version.

    First publish (version=1): marketplace_service.approve flips status
    review→published.

    Subsequent versions: when version > 1, derive prev_version automatically
    by reading the listing's history and finding the currently-published
    row. publish_new_version atomically flips prev_version's status published→retired
    AND new version's review→published in one TransactWriteItems. This
    keeps the frontend ignorant of the v_prev concept — the admin just
    approves; the backend figures out which version (if any) to retire.

    `prev_version` query param is retained as a manual override for
    edge cases where the admin wants to be explicit (e.g. tooling that
    wants to fail loudly on race conditions).
    """
    # Defense in depth: refuse to approve a listing whose tarball can't
    # be fully scanned. The admin UI hides the Approve button when the
    # preview reports scan_incomplete=true, but a moderator could still
    # POST directly. We re-check BOTH the compressed size (ContentLength)
    # AND the unpacked sum (sum of tar header sizes), because a gzip with
    # a high compression ratio — or an outright gzip bomb — can be small
    # on disk but trip the unpacked cap inside the preview. Without the
    # unpacked check this gate would only catch the easier case.
    table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
    row = table.get_item(Key={"listing_id": listing_id, "version": version}).get("Item") or {}
    s3_prefix = row.get("s3_prefix")
    # ``artifact_uploaded`` is the canonical signal that an artifact is on
    # S3. submit_for_review enforces this before flipping draft → review,
    # so any listing reaching approve should have it True. Missing
    # defaults to True for legacy rows written before the field existed
    # (matches the convention in marketplace_service.submit_for_review).
    artifact_uploaded = row.get("artifact_uploaded", True)
    if s3_prefix and artifact_uploaded:
        cap_mb = _MAX_PREVIEW_TARBALL_BYTES // (1024 * 1024)
        s3 = boto3.client("s3")
        artifact_key = f"{s3_prefix}workspace.tar.gz"

        # ANY failure on the scan path — head_object, get_object, or the
        # tarfile parse — must block approval. The artifact is supposed to
        # exist (artifact_uploaded=True above), so a missing or
        # inaccessible artifact at moderation time means we cannot verify
        # safety. Without a clean scan we can't tell whether the bytes are
        # what the moderator inspected; fail closed.
        try:
            head = s3.head_object(Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET, Key=artifact_key)
        except Exception:
            raise HTTPException(
                status_code=409,
                detail=(
                    "could not verify artifact size on S3 — approval blocked. "
                    "Retry, or reject if the artifact is missing."
                ),
            )
        tarball_bytes = int(head.get("ContentLength", 0))
        if tarball_bytes > _MAX_PREVIEW_TARBALL_BYTES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"artifact is {tarball_bytes // (1024 * 1024)} MB compressed, "
                    f"exceeds {cap_mb} MB scan cap — approval blocked. "
                    "Reject or ask the seller to slim the listing."
                ),
            )
        # Unpacked-size gate. Reading the gzip into memory here costs one
        # bounded download (≤50 MB by the compressed check above) per
        # approve call, which is acceptable for an admin path.
        # ``tarfile.getmembers()`` reads headers only — no file content is
        # loaded — so we don't OOM on a 50 MB tarball with 500 MB of
        # unpacked content.
        try:
            body = s3.get_object(Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET, Key=artifact_key)["Body"].read()
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
                unpacked = sum(m.size for m in tf.getmembers() if m.isfile())
        except tarfile.TarError:
            raise HTTPException(
                status_code=409,
                detail="artifact tarball is unreadable — approval blocked.",
            )
        except Exception:
            raise HTTPException(
                status_code=409,
                detail=("artifact scan failed (could not download or parse the tarball) — approval blocked."),
            )
        if unpacked > _MAX_PREVIEW_TARBALL_BYTES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"artifact unpacks to {unpacked // (1024 * 1024)} MB, "
                    f"exceeds {cap_mb} MB scan cap — approval blocked. "
                    "The preview safety scan was incomplete. Reject or ask "
                    "the seller to slim the listing."
                ),
            )

    try:
        if version > 1 and prev_version is None:
            # Derive prev_version: the currently-published row of this listing.
            table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
            rows = table.query(
                KeyConditionExpression="listing_id = :l",
                ExpressionAttributeValues={":l": listing_id},
            ).get("Items", [])
            published = next((r for r in rows if r.get("status") == "published"), None)
            if published is not None and int(published["version"]) != version:
                prev_version = int(published["version"])

        if prev_version is not None:
            await marketplace_service.publish_new_version(
                listing_id=listing_id,
                prev_version=prev_version,
                new_version=version,
                approved_by=auth.user_id,
            )
            return {"status": "published", "version": version, "retired_version": prev_version}
        return await marketplace_service.approve(listing_id=listing_id, version=version, approved_by=auth.user_id)
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


class RejectBody(BaseModel):
    """Body schema for the admin reject endpoint.

    Notes is a body field, not a query param: the admin UI sends
    ``{ notes }`` in JSON (apps/frontend/src/app/admin/_actions/marketplace.ts).
    Declaring `notes: str` as a plain function arg made FastAPI bind it as
    a query param, so every reject from the UI 422'd.
    """

    notes: str = Field(..., min_length=1, max_length=4096)


@router.post("/listings/{listing_id}/reject")
@audit_admin_action(
    "marketplace.reject",
    target_user_id_override="__marketplace__",
    capture_params=["listing_id", "version", "body"],
)
async def reject(
    listing_id: str,
    body: RejectBody,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
    version: int = 1,
):
    """Reject a listing version with seller-visible notes.

    Flips status review→draft and persists the moderator's notes on the
    row so the seller can see them on their listing detail page and resubmit.
    """
    try:
        return await marketplace_service.reject(
            listing_id=listing_id, version=version, notes=body.notes, rejected_by=auth.user_id
        )
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get(
    "/listings/{listing_id}/preview",
    response_model=schemas.ListingPreviewResponse,
)
@audit_admin_action(
    "marketplace.listing_preview",
    target_user_id_override="__marketplace__",
    capture_params=["listing_id"],
)
async def listing_preview(
    listing_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
    version: int | None = None,
):
    """Preview a listing's contents for moderation review.

    Streams the listing's workspace.tar.gz from S3, extracts in-memory
    (capped at 10 MB), runs the safety scan, and returns a structured
    response that the admin UI renders as a file tree + content viewer +
    safety-flag banner.

    When ``version`` is provided, fetches that exact ``(listing_id, version)``
    row — used by the browse view to inspect a specific version (e.g.,
    the published v1 while a v2 sits in review). When omitted, falls
    back to "prefer review, then published, then highest version" so
    the moderation-queue link (which doesn't know about versions)
    still resolves to the actionable row.
    """
    table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_LISTINGS_TABLE)
    if version is not None:
        # Caller selected an exact version (browse-view link). Honor it.
        item = table.get_item(Key={"listing_id": listing_id, "version": version}).get("Item")
        if not item:
            raise HTTPException(status_code=404, detail="listing not found")
        listing = item
    else:
        # Review-queue link path: pick the most-actionable row across versions.
        rows = table.query(
            KeyConditionExpression="listing_id = :l",
            ExpressionAttributeValues={":l": listing_id},
            ScanIndexForward=False,  # newest version first
        ).get("Items", [])
        if not rows:
            raise HTTPException(status_code=404, detail="listing not found")
        listing = (
            next((r for r in rows if r.get("status") == "review"), None)
            or next((r for r in rows if r.get("status") == "published"), None)
            or rows[0]
        )

    s3 = boto3.client("s3")
    bucket = settings.MARKETPLACE_ARTIFACTS_BUCKET
    s3_prefix = listing.get("s3_prefix", "")
    tar_key = f"{s3_prefix}workspace.tar.gz"

    try:
        head = s3.head_object(Bucket=bucket, Key=tar_key)
    except Exception:
        # Empty / not-yet-uploaded artifact — return an empty preview rather
        # than 500. The admin UI shows "no artifact uploaded yet".
        return schemas.ListingPreviewResponse(
            listing_id=listing_id,
            version=int(listing.get("version", 1)),
            slug=listing.get("slug", ""),
            name=listing.get("name", ""),
            seller_id=listing.get("seller_id", ""),
            format=listing.get("format", "openclaw"),
            status=listing.get("status", "draft"),
            price_cents=int(listing.get("price_cents", 0)),
            tags=list(listing.get("tags", [])),
            manifest=listing.get("manifest_json", {}),
            file_tree=[],
            skill_md_text=None,
            openclaw_summary=None,
            safety_flags=[],
        )
    tarball_bytes = int(head.get("ContentLength", 0))
    listing_format = listing.get("format", "openclaw")
    base_response_kwargs = dict(
        listing_id=listing_id,
        version=int(listing.get("version", 1)),
        slug=listing.get("slug", ""),
        name=listing.get("name", ""),
        seller_id=listing.get("seller_id", ""),
        format=listing_format,
        status=listing.get("status", "draft"),
        price_cents=int(listing.get("price_cents", 0)),
        tags=list(listing.get("tags", [])),
        manifest=listing.get("manifest_json", {}),
        tarball_bytes=tarball_bytes,
    )

    # Tarball download ceiling: even the compressed bytes are over the cap.
    # We can't enumerate members without buffering the whole gzip stream,
    # so degrade to a metadata-only response with scan_incomplete=True.
    # Admin UI disables Approve and surfaces the size so the moderator can
    # ask the seller to slim it down (or reject).
    if tarball_bytes > _MAX_PREVIEW_TARBALL_BYTES:
        cap_mb = _MAX_PREVIEW_TARBALL_BYTES // (1024 * 1024)
        return schemas.ListingPreviewResponse(
            **base_response_kwargs,
            file_tree=[],
            skill_md_text=None,
            openclaw_summary=None,
            safety_flags=[],
            scan_incomplete=True,
            scan_failure_reason=(
                f"tarball is {tarball_bytes // (1024 * 1024)} MB compressed, "
                f"exceeds the {cap_mb} MB scan ceiling. File tree unavailable — "
                "ask the seller to slim the artifact (or reject)."
            ),
            unpacked_bytes=0,
        )

    body = s3.get_object(Bucket=bucket, Key=tar_key)["Body"].read()

    file_tree: list[schemas.FileTreeEntry] = []
    file_dict: dict[str, bytes] = {}
    skill_md_text: str | None = None
    openclaw_config: dict | None = None
    scan_incomplete = False
    scan_failure_reason: str | None = None

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            # Two-pass: first collect the file tree from tar headers (cheap,
            # always succeeds), then extract content under the cap. If the
            # cap is hit during extraction the file tree still tells the
            # moderator WHAT is in the tarball so they can identify the
            # bloat — without that, all they see is a 413 and can't act.
            members = [m for m in tf.getmembers() if m.isfile() and not (m.issym() or m.islnk())]
            for m in members:
                clean_name = m.name[2:] if m.name.startswith("./") else m.name
                file_tree.append(schemas.FileTreeEntry(path=clean_name, size_bytes=m.size))

            total_unpacked = 0
            cap_mb = _MAX_PREVIEW_TARBALL_BYTES // (1024 * 1024)
            for m in members:
                next_total = total_unpacked + m.size
                if next_total > _MAX_PREVIEW_TARBALL_BYTES:
                    # Stop reading content but keep the file_tree we built
                    # above. Safety scan can't trust a partial scan (an
                    # adversary could hide bad content behind padding), so
                    # we surface scan_incomplete and the admin UI gates
                    # Approve on it.
                    scan_incomplete = True
                    scan_failure_reason = (
                        f"unpacked size exceeds {cap_mb} MB cap "
                        f"(stopped after {total_unpacked // (1024 * 1024)} MB). "
                        "Safety scan skipped. Approve disabled — reject or ask the "
                        "seller to slim the artifact."
                    )
                    total_unpacked = next_total
                    break
                f = tf.extractfile(m)
                if f is None:
                    total_unpacked = next_total
                    continue
                clean_name = m.name[2:] if m.name.startswith("./") else m.name
                data = f.read()
                file_dict[clean_name] = data
                total_unpacked = next_total
                if clean_name == "SKILL.md" and skill_md_text is None:
                    try:
                        skill_md_text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        skill_md_text = None
                if clean_name == "openclaw.json" and openclaw_config is None:
                    try:
                        openclaw_config = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        openclaw_config = None

            # Continue accumulating unpacked size for any remaining members
            # (helps the moderator see "the listing is 80 MB total" even
            # though we only scanned the first 50 MB).
            if scan_incomplete:
                remaining_idx = members.index(m) + 1
                for rest in members[remaining_idx:]:
                    total_unpacked += rest.size
    except tarfile.TarError as e:
        raise HTTPException(status_code=422, detail=f"artifact tarball is not readable: {e}")

    file_tree.sort(key=lambda e: e.path)
    # Skip the safety scan entirely when the artifact wasn't fully read.
    # A partial scan is worse than no scan — it gives a false sense of
    # security if the moderator misreads scan_incomplete.
    flags = marketplace_safety.scan(file_dict, format=listing_format) if not scan_incomplete else []

    openclaw_summary: schemas.OpenclawSummary | None = None
    if listing_format == "openclaw" and isinstance(openclaw_config, dict):
        tools = openclaw_config.get("tools") or {}
        providers = list((openclaw_config.get("models") or {}).get("providers") or {})
        cron = openclaw_config.get("cron") or {}
        channels = openclaw_config.get("channels") or {}
        agents_block = openclaw_config.get("agents") or {}
        openclaw_summary = schemas.OpenclawSummary(
            tools_count=len(tools) if isinstance(tools, dict) else 0,
            providers=[str(p) for p in providers],
            cron_count=len(cron.get("jobs") or {}) if isinstance(cron, dict) else 0,
            channels_count=sum(len(v.get("accounts") or {}) for v in channels.values() if isinstance(v, dict))
            if isinstance(channels, dict)
            else 0,
            sub_agent_count=len(agents_block.get("sub_agents") or []) if isinstance(agents_block, dict) else 0,
            raw_config_size_bytes=len(file_dict.get("openclaw.json", b"")),
        )

    return schemas.ListingPreviewResponse(
        **base_response_kwargs,
        file_tree=file_tree,
        skill_md_text=skill_md_text if listing_format == "skillmd" else None,
        openclaw_summary=openclaw_summary,
        safety_flags=[
            schemas.SafetyFlag(
                pattern=f.pattern,
                severity=f.severity,
                file=f.file,
                line=f.line,
                snippet=f.snippet,
            )
            for f in flags
        ],
        scan_incomplete=scan_incomplete,
        scan_failure_reason=scan_failure_reason,
        unpacked_bytes=total_unpacked,
    )


class AdminTakedownBody(BaseModel):
    """Admin-initiated takedown body.

    Under the Isol8-internal scope there's no public DMCA filing form, so
    the takedown is created and granted in a single admin action: the admin
    types the reason on the listing detail page and submits.
    """

    reason: Literal["dmca", "policy", "fraud", "seller-request"]
    basis_md: str = Field(..., min_length=10, max_length=4096)


@router.post("/listings/{listing_id}/takedown")
@audit_admin_action(
    "marketplace.takedown",
    target_user_id_override="__marketplace__",
    capture_params=["listing_id", "body"],
)
async def admin_initiated_takedown(
    listing_id: str,
    body: AdminTakedownBody,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
):
    """Admin-initiated takedown.

    Writes the takedown row + cascades license revocation + flips the
    listing's status to `taken_down`, all in one shot. Returns the new
    takedown_id and the count of affected purchases so the UI can show
    confirmation copy.
    """
    try:
        result = await takedown_service.execute_admin_initiated_takedown(
            listing_id=listing_id,
            reason=body.reason,
            basis_md=body.basis_md,
            decided_by=auth.user_id,
        )
    except takedown_service.ListingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "taken_down", **result}


@router.get("/takedowns")
@audit_admin_action(
    "marketplace.list_takedowns",
    target_user_id_override="__marketplace__",
)
async def list_takedowns(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_platform_admin)],
):
    """Recent takedowns, newest first.

    Audit-log view (no `?status=pending` queue — the queue is structurally
    empty under the Isol8-internal scope). v0 volume is very low (< a few
    hundred rows expected for the foreseeable future), so we page through
    the full scan, sort in Python, and slice the top 50. We deliberately do
    NOT cap the scan with `Limit=N` — DDB scans return items in
    unpredictable order, so a capped scan would return "newest of an
    arbitrary subset" rather than the globally newest rows.

    TODO(scale): when takedown volume grows, replace the scan with a query
    against a GSI partitioned on `decision` and sorted by `decided_at`
    (e.g. `decision-decided_at-index`), and drop this Python-side sort.
    """
    table = boto3.resource("dynamodb").Table(settings.MARKETPLACE_TAKEDOWNS_TABLE)
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last
    # Sort by decided_at desc; rows that somehow lack decided_at sink to the
    # bottom (shouldn't happen for admin-initiated rows, but be defensive).
    items.sort(key=lambda r: r.get("decided_at") or "", reverse=True)
    return {"items": items[:50]}
