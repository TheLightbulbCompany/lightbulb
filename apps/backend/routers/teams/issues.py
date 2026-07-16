"""Teams BFF — Issues.

Tier 2 mutating resource: ``CreateIssueBody`` and ``PatchIssueBody``
forbid extras, so unknown / smuggled fields are 422'd at the FastAPI
boundary. No adapter synthesis here — issues are pure user data.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

# ``request.form()`` yields Starlette's ``UploadFile`` (the base class FastAPI's
# subclasses); isinstance-check against the base so it matches both.
from starlette.datastructures import UploadFile


from . import agents as _agents
from ._guards import require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import AddIssueCommentBody, CreateIssueBody, PatchIssueBody

router = APIRouter()
_ctx = _agents._ctx
logger = logging.getLogger(__name__)

# Hard cap on attachment uploads at the BFF boundary. Upstream Paperclip
# additionally enforces a per-company ``attachmentMaxBytes`` (default 10 MB,
# ``server/src/attachment-types.ts``), so the EFFECTIVE limit is the lower of
# the two — this cap is the early-reject fence so the backend never buffers a
# multi-hundred-MB body just to forward it and have upstream 422 it.
_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
# Multipart framing (boundary + part headers) adds a little above the raw file
# bytes; allow ~1 MB headroom for the pre-read Content-Length fence.
_ATTACHMENT_REQUEST_MAX_BYTES = _ATTACHMENT_MAX_BYTES + 1024 * 1024
_ATTACHMENT_READ_CHUNK = 1024 * 1024

# Paperclip ``projects.id`` is a Postgres ``uuid`` column, so a malformed
# ``project`` filter makes upstream throw a uuid-cast error → 500 (whose
# body leaks the internal company path). Validate the shape here so a bad
# value is a local 422 before any upstream call — mirrors ``inbox.py``.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


@router.get("/issues")
async def list_issues(
    ctx: TeamsContext = Depends(_ctx),
    q: Optional[str] = Query(default=None, max_length=200),
    status: Optional[str] = Query(default=None, max_length=120),
    project: Optional[str] = Query(default=None, pattern=_UUID_RE),
    # Same uuid-cast guard as ``project`` — upstream casts ``assigneeAgentId``
    # to a Postgres ``uuid``, so a malformed value 500s and leaks the company
    # path. ``"null"`` is the sentinel upstream accepts to filter unassigned.
    assignee: Optional[str] = Query(default=None, pattern=r"^([0-9a-fA-F-]{36}|null)$"),
    sort_field: Optional[str] = Query(default=None, alias="sortField", pattern=r"^(updated)$"),
    sort_dir: Optional[str] = Query(default=None, alias="sortDir", pattern=r"^(asc|desc)$"),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    """List issues in the caller's company.

    Upstream ``GET /api/companies/{co}/issues`` returns a bare JSON
    array. The frontend Tasks panel reads ``data?.issues`` (envelope
    shape) so without this wrap the list silently renders empty.

    Optional query params are forwarded to upstream's already-capable
    issues-list route so the Tasks surface can drive server-side
    search (``q``), status filtering, per-status board columns
    (``status``), assignee/project filters, and sort. Mirrors the
    pass-through style of the sibling ``inbox.py`` listing.
    """
    upstream_params: dict[str, str] = {}
    if q is not None:
        upstream_params["q"] = q
    if status is not None:
        upstream_params["status"] = status
    if project is not None:
        upstream_params["projectId"] = project
    if assignee is not None:
        upstream_params["assigneeAgentId"] = assignee
    if sort_field is not None:
        upstream_params["sortField"] = sort_field
    if sort_dir is not None:
        upstream_params["sortDir"] = sort_dir
    if limit is not None:
        upstream_params["limit"] = str(limit)

    kwargs: dict[str, Any] = {
        "company_id": ctx.company_id,
        "session_cookie": ctx.session_cookie,
    }
    if upstream_params:
        kwargs["params"] = upstream_params

    rows = await _agents._admin().list_issues(**kwargs)
    return envelope_list(rows, "issues")


@router.get("/issues/labels")
async def list_labels(ctx: TeamsContext = Depends(_ctx)):
    """List company labels for the Inbox filter chip dropdown.

    Wraps upstream's bare-array response in ``{labels: [...]}`` envelope so
    the frontend's panel hooks can read ``data?.labels`` like every other
    Teams BFF resource. See ``deps.envelope_list`` for the wrapping pattern.

    Registered before ``/issues/{issue_id}`` so FastAPI's path matcher picks
    the static segment over the parameterized one.
    """
    upstream = await _agents._admin().list_company_labels(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "labels")


# NOTE: any new static `/issues/<word>` routes (e.g. /issues/labels) MUST be
# registered ABOVE this handler — FastAPI's matcher would otherwise treat
# the static word as a value for {issue_id} and 404 with a misleading shape.
@router.get("/issues/{issue_id}")
async def get_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single issue by id."""
    return await _agents._admin().get_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues")
async def create_issue(
    body: CreateIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create an issue."""
    return await _agents._admin().create_issue(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/issues/{issue_id}")
async def patch_issue(
    issue_id: str,
    body: PatchIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch an issue.

    Uses ``exclude_unset=True`` (not ``exclude_none``) so the frontend
    can send ``assignee_agent_id: null`` / ``project_id: null`` to
    unassign — Pydantic only emits keys the caller explicitly provided,
    including those set to ``null``. Dropping ``exclude_none`` was
    needed because the read-only assignee row in IssueProperties was
    replaced with a real picker that needs an "Unassigned" path.
    """
    return await _agents._admin().patch_issue(
        issue_id=issue_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/issues/{issue_id}")
async def delete_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete an issue.

    Maps to upstream ``DELETE /api/issues/{id}``. The admin client's
    ``_delete`` helper treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/archive")
async def archive_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Archive an issue from the inbox.

    Maps to upstream ``POST /api/issues/{id}/inbox-archive``. The Inbox
    UI fades the row out + shows an undo toast for ~8s.
    """
    return await _agents._admin().archive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/unarchive")
async def unarchive_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Restore an archived issue back to the inbox.

    Maps to upstream ``DELETE /api/issues/{id}/inbox-archive``. Drives the
    undo-archive toast.
    """
    return await _agents._admin().unarchive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/monitor/check-now")
async def monitor_check_now(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Trigger an immediate monitor re-check. Maps to upstream
    POST /api/issues/{id}/monitor/check-now.

    An immediate re-check dispatches a run right away server-side inside
    Paperclip, so this is a spend front door. The free-tier $3/day
    cap is checked here, BEFORE the proxy call reaches Paperclip.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().monitor_check_now(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-read")
async def mark_issue_read(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Mark an issue as read for the signed-in user.

    Maps to upstream ``POST /api/issues/{id}/read``.
    """
    return await _agents._admin().mark_issue_read(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-unread")
async def mark_issue_unread(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Mark an issue as unread for the signed-in user.

    Maps to upstream ``DELETE /api/issues/{id}/read``.
    """
    return await _agents._admin().mark_issue_unread(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


async def _enrich_comment_authors(ctx: TeamsContext, payload: Any) -> None:
    """Stamp ``authorName`` / ``authorClerkUserId`` onto human-authored comment
    rows in place.

    Upstream comment rows carry only ``authorUserId`` — a Paperclip principal
    id (Better Auth), which the frontend cannot join against the Clerk-keyed
    ``/members`` roster. Mirror the activity feed's actor enrichment
    (``feed._enrich_actor_names``): principal → Clerk id → Clerk identity,
    each unique author resolved at most once. Best-effort — an unmapped
    principal or failed Clerk lookup ships that row unstamped rather than
    failing the request.
    """
    if isinstance(payload, list):
        comments = payload
    elif isinstance(payload, dict) and isinstance(payload.get("comments"), list):
        comments = payload["comments"]
    else:
        return

    author_ids: set[str] = set()
    for row in comments:
        if not isinstance(row, dict) or row.get("authorAgentId"):
            continue
        author_id = row.get("authorUserId")
        if isinstance(author_id, str) and author_id:
            author_ids.add(author_id)
    if not author_ids:
        return

    principal_to_clerk = await _agents.build_principal_to_clerk(ctx)

    identity_by_author: dict[str, dict] = {}
    for author_id in author_ids:
        clerk_user_id = principal_to_clerk.get(author_id)
        if not clerk_user_id:
            continue
        try:
            member = await _agents._resolve_member_identity(clerk_user_id)
        except Exception:
            logger.exception(
                "issues.comments: clerk lookup failed for clerk_user_id=%s (paperclip principal=%s)",
                clerk_user_id,
                author_id,
            )
            continue
        if member:
            identity_by_author[author_id] = member

    for row in comments:
        if not isinstance(row, dict) or row.get("authorAgentId"):
            continue
        member = identity_by_author.get(row.get("authorUserId"))
        if member:
            row["authorName"] = member.get("name")
            row["authorClerkUserId"] = member.get("userId")


@router.get("/issues/{issue_id}/comments")
async def list_issue_comments(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List comments on an issue, with human authors resolved to names.

    Maps to upstream ``GET /api/issues/{id}/comments``; rows pass through
    verbatim except for the ``_enrich_comment_authors`` stamp.
    """
    payload = await _agents._admin().list_issue_comments(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    await _enrich_comment_authors(ctx, payload)
    return payload


@router.post("/issues/{issue_id}/comments")
async def add_issue_comment(
    issue_id: str,
    body: AddIssueCommentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Post a comment on an issue. Body is whitelisted to ``{body: str}``.

    A comment triggers Friday (or the assigned agent) to run server-side
    inside Paperclip, so this is a spend front door. The free-tier
    $3/day cap is checked here, BEFORE the proxy call reaches Paperclip --
    that's what actually stops the spend, since there is no enforcement
    point downstream of this proxy.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().add_issue_comment(
        issue_id=issue_id,
        body=body.model_dump(by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/children")
async def list_issue_children(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List an issue's descendants (the Sub-issues section).

    Forwards upstream's ``descendantOf`` filter on the issues-list route —
    matches the upstream IssueDetail sub-issues section (all descendants,
    flat). Wrapped ``{issues: [...]}`` like the other list surfaces.
    """
    rows = await _agents._admin().list_issues(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params={"descendantOf": issue_id},
    )
    return envelope_list(rows, "issues")


@router.post("/issues/{issue_id}/children")
async def create_issue_child(
    issue_id: str,
    body: CreateIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a sub-issue under ``issue_id``.

    Reuses ``CreateIssueBody`` (the same whitelist as top-level create);
    routes to upstream's ``/children`` endpoint, which wires the
    parent/child relation server-side.
    """
    return await _agents._admin().create_child_issue(
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/approvals")
async def list_issue_approvals(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the approvals linked to an issue (the issue-detail Approvals section)."""
    rows = await _agents._admin().list_issue_approvals(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "approvals")


@router.get("/issues/{issue_id}/runs")
async def list_issue_runs(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the agent runs for an issue (the issue-detail Runs section)."""
    rows = await _agents._admin().list_issue_runs(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "runs")


@router.get("/issues/{issue_id}/documents")
async def list_issue_documents(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the documents attached to an issue (the issue-detail Documents section)."""
    rows = await _agents._admin().list_issue_documents(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "documents")


# ----------------------------------------------------------------------
# Issue attachments (binary files: images / PDFs / etc.)
# ----------------------------------------------------------------------


@router.get("/issues/{issue_id}/attachments")
async def list_issue_attachments(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the binary attachments on an issue (the issue-detail Attachments section).

    Maps to upstream ``GET /api/issues/{id}/attachments`` (issue-scoped, the
    session cookie's company is asserted upstream). Wrapped ``{attachments:
    [...]}`` like the sibling list surfaces.
    """
    rows = await _agents._admin().list_issue_attachments(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "attachments")


@router.post("/issues/{issue_id}/attachments")
async def upload_issue_attachment(
    issue_id: str,
    request: Request,
    ctx: TeamsContext = Depends(_ctx),
):
    """Upload a binary attachment to an issue.

    Forwards to upstream ``POST /api/companies/{company}/issues/{issue}/
    attachments`` as multipart (field name ``"file"``).

    SECURITY — company scoping: the company segment is taken from
    ``ctx.company_id`` (the server-derived company on the resolved
    ``TeamsContext``), NEVER from a client-supplied param. This route is
    IDOR-sensitive, so the company id comes from the authenticated context
    only; upstream then validates the path issue id against that company
    (``issue.companyId !== companyId`` → 422).

    SECURITY — body size: a huge upload must not OOM the task. We (1) reject by
    ``Content-Length`` BEFORE parsing the multipart, so an honestly-sized
    oversized body never spools, and (2) read the file in bounded chunks and
    abort the moment the cap is exceeded — so memory stays bounded even if the
    header is absent or lies. (We take ``request`` directly rather than a
    ``File(...)`` param so the length fence runs before form parsing.)
    """
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > _ATTACHMENT_REQUEST_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment exceeds the {_ATTACHMENT_MAX_BYTES} byte limit",
        )

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=422, detail="Missing 'file' upload field")

    chunks = bytearray()
    while True:
        chunk = await upload.read(_ATTACHMENT_READ_CHUNK)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > _ATTACHMENT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Attachment exceeds the {_ATTACHMENT_MAX_BYTES} byte limit",
            )

    return await _agents._admin().upload_issue_attachment(
        session_cookie=ctx.session_cookie,
        company_id=ctx.company_id,
        issue_id=issue_id,
        filename=upload.filename,
        content_type=upload.content_type or "application/octet-stream",
        body=bytes(chunks),
    )


@router.get("/attachments/{attachment_id}/content")
async def get_attachment_content(attachment_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Stream an attachment's bytes back to the browser.

    Proxies upstream ``GET /api/attachments/{id}/content``. The upstream route
    resolves the attachment and asserts the session cookie's company owns it,
    so a cross-tenant id is rejected upstream (404/403) — the BFF doesn't
    re-derive ownership here.

    Streamed via ``StreamingResponse`` over the upstream
    ``aiter_bytes()`` so a large file never buffers in backend memory. The
    upstream ``Content-Type`` / ``Content-Disposition`` / ``Content-Length``
    are copied through; ``X-Content-Type-Options: nosniff`` is forwarded if
    present so the browser doesn't MIME-sniff an untrusted upload.
    """
    admin = _agents._admin()

    # Open the upstream stream ONCE. ``open_attachment_stream`` does the GET,
    # raises ``PaperclipApiError`` on a >=400 status (so the error surfaces as
    # an HTTP error here, BEFORE the StreamingResponse body begins — a
    # generator that raises mid-stream can't set a status code), and returns
    # the live response on success.
    upstream = await admin.open_attachment_stream(
        session_cookie=ctx.session_cookie,
        attachment_id=attachment_id,
    )

    headers: dict[str, str] = {}
    # Copy the upstream's content-serving safety headers through verbatim:
    # ``content-disposition`` (upstream forces ``attachment`` for dangerous
    # types) and ``content-security-policy`` (upstream's inline-SVG sandbox) are
    # the inline-render / stored-XSS defenses — forwarding the CSP keeps that
    # defense intact if the allowed-types set is ever widened. ``cache-control``
    # keeps a private attachment out of shared caches.
    for h in (
        "content-disposition",
        "content-length",
        "x-content-type-options",
        "content-security-policy",
        "cache-control",
    ):
        v = upstream.headers.get(h)
        if v:
            headers[h] = v
    media_type = upstream.headers.get("content-type", "application/octet-stream")

    async def _body():
        # Pump the bytes from the already-open upstream response, then release
        # the connection. ``aclose`` in a finally guards against a client
        # disconnect mid-stream leaking the upstream socket.
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(_body(), media_type=media_type, headers=headers)


@router.delete("/issues/{issue_id}/attachments/{attachment_id}")
async def delete_issue_attachment(
    issue_id: str,  # noqa: ARG001 — issue scoping is enforced upstream via the company cookie.
    attachment_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete an attachment.

    Maps to upstream ``DELETE /api/attachments/{id}``. The ``issue_id`` is in
    the path for a RESTful URL shape, but upstream scopes the delete to the
    session cookie's company (``assertCompanyAccess`` on the resolved
    attachment), so a cross-tenant attachment id is rejected upstream rather
    than relying on a BFF-side issue↔attachment ownership check. 404 is treated
    as already-gone (idempotent) by the admin client's ``_delete``.
    """
    return await _agents._admin().delete_attachment(
        session_cookie=ctx.session_cookie,
        attachment_id=attachment_id,
    )
