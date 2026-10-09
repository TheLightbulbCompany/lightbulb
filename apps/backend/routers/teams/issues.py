"""Teams BFF — Issues.

Tier 2 mutating resource: ``CreateIssueBody`` and ``PatchIssueBody``
forbid extras, so unknown / smuggled fields are 422'd at the FastAPI
boundary. No adapter synthesis here — issues are pure user data.
"""

from __future__ import annotations

import logging
import re
import zlib
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

# ``request.form()`` yields Starlette's ``UploadFile`` (the base class FastAPI's
# subclasses); isinstance-check against the base so it matches both.
from starlette.datastructures import UploadFile


from . import agents as _agents
from ._guards import require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import (
    AddIssueCommentBody,
    CreateIssueBody,
    CreateIssueLabelBody,
    CreateWorkProductBody,
    ExternalObjectSummariesBody,
    PatchIssueBody,
    PatchWorkProductBody,
    RefreshExternalObjectsBody,
    ResolveIssueRecoveryActionBody,
    UpsertIssueWatchdogBody,
)

router = APIRouter()

# Paperclip ids are UUIDs or LB-12-style identifiers (`_` admitted for test ids). Pinning keeps a
# percent-decoded `?`/`&`/`/` in the path segment from rewriting the fixed
# upstream path (e.g. `<id>%3Fmode=cancel%26` smuggling a query string).
IssueId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]
_ctx = _agents._ctx


async def _company_issue(issue_id: IssueId, ctx: TeamsContext = Depends(_ctx)) -> str:
    """Route dependency: 404 unless ``issue_id`` is in the caller's workspace company.

    See ``_require_company_issue``. Every ``{issue_id}`` route in the Teams BFF
    takes ``CompanyIssueId``; ``test_teams_company_fences`` fails on one that doesn't.
    """
    await _require_company_issue(issue_id, ctx)
    return issue_id


CompanyIssueId = Annotated[str, Depends(_company_issue)]
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
    # Upstream ``participantAgentId`` (assignee, author or commenter) — the
    # agent Dashboard's Recent Tasks. Same uuid-cast guard as ``assignee``.
    participant_agent_id: Optional[str] = Query(default=None, alias="participantAgentId", pattern=_UUID_RE),
    sort_field: Optional[str] = Query(default=None, alias="sortField", pattern=r"^(updated)$"),
    sort_dir: Optional[str] = Query(default=None, alias="sortDir", pattern=r"^(asc|desc)$"),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
    exclude_routine_executions: bool = Query(default=False, alias="excludeRoutineExecutions"),
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

    ``excludeRoutineExecutions`` drops the rows a routine spawns on every
    firing (``originKind == "routine_execution"``). Upstream's dedup index
    only holds a routine's task open while it has a live execution run, so a
    recurring routine accumulates one finished row per tick — enough to bury
    hand-filed tasks. Filtering has to happen upstream, not client-side: the
    list is capped by ``limit``, so a client-side drop would return a handful
    of real tasks out of a page full of routine rows.
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
    if participant_agent_id is not None:
        upstream_params["participantAgentId"] = participant_agent_id
    if sort_field is not None:
        upstream_params["sortField"] = sort_field
    if sort_dir is not None:
        upstream_params["sortDir"] = sort_dir
    if limit is not None:
        upstream_params["limit"] = str(limit)
    if exclude_routine_executions:
        # Upstream reads this as the literal string "true" / "1".
        upstream_params["excludeRoutineExecutions"] = "true"

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


# Editorial palette. Upstream requires a colour on every label; picking one is
# not a decision worth asking the user for, so a label's colour is derived from
# its name — deterministic, so the same name always reads the same.
_LABEL_COLORS = (
    "#2F5D50",  # forest
    "#8C5A3C",  # clay
    "#4A5B8C",  # slate blue
    "#7A6A9B",  # plum
    "#3F6F8C",  # teal
    "#96603E",  # amber-brown
)


def _label_color_for(name: str) -> str:
    return _LABEL_COLORS[zlib.crc32(name.strip().lower().encode("utf-8")) % len(_LABEL_COLORS)]


@router.post("/issues/labels")
async def create_label(body: CreateIssueLabelBody, ctx: TeamsContext = Depends(_ctx)):
    """Create a company label.

    Nothing seeds labels at provisioning, so before this route existed the
    rail's label picker had an empty list it offered no way to fill — labels
    were selectable in principle and unusable in practice.

    Registered above ``/issues/{issue_id}``-style paths for the same
    static-vs-parameterized reason as ``GET /issues/labels``.
    """
    created = await _agents._admin().create_company_label(
        company_id=ctx.company_id,
        body={
            # Already stripped + rejected-if-blank by CreateIssueLabelBody.
            "name": body.name,
            "color": body.color or _label_color_for(body.name),
        },
        session_cookie=ctx.session_cookie,
    )
    return created


# NOTE: any new static `/issues/<word>` routes (e.g. /issues/labels) MUST be
# registered ABOVE this handler — FastAPI's matcher would otherwise treat
# the static word as a value for {issue_id} and 404 with a misleading shape.
@router.get("/issues/{issue_id}")
async def get_issue(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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
    issue_id: CompanyIssueId,
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
    payload = body.model_dump(exclude_unset=True, by_alias=True)
    admin = _agents._admin()
    # Handing a BLOCKED issue to an agent must unblock it in the same PATCH: on its own the
    # new agent is woken, its run is cancelled as blocked, and the issue snaps back to the
    # recovery owner (Paperclip audit 2026-09-24 F6). Real open blockers still 422, and that
    # reason reaches the caller (main.py's PaperclipApiError handler).
    new_parent = payload.get("parentId")
    if new_parent:
        # Paperclip accepts any parent, so a task could become its own ancestor
        # (Aden audit 10-02 #1). Its issue read carries the parent's ancestor chain.
        parent = await admin.get_issue(issue_id=new_parent, session_cookie=ctx.session_cookie) or {}
        chain = {new_parent, parent.get("id"), *(a.get("id") for a in parent.get("ancestors") or [])}
        if issue_id in chain:
            raise HTTPException(status_code=422, detail="A task can't be moved under one of its own sub-tasks.")
    if payload.get("assigneeAgentId") and "status" not in payload:
        current = await admin.get_issue(issue_id=issue_id, session_cookie=ctx.session_cookie)
        if (current or {}).get("status") == "blocked":
            payload["status"] = "todo"
    return await admin.patch_issue(
        issue_id=issue_id,
        body=payload,
        session_cookie=ctx.session_cookie,
    )


@router.delete("/issues/{issue_id}")
async def delete_issue(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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
async def archive_issue(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Archive an issue from the inbox.

    Maps to upstream ``POST /api/issues/{id}/inbox-archive``. The Inbox
    UI fades the row out + shows an undo toast for ~8s.
    """
    return await _agents._admin().archive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/unarchive")
async def unarchive_issue(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Restore an archived issue back to the inbox.

    Maps to upstream ``DELETE /api/issues/{id}/inbox-archive``. Drives the
    undo-archive toast.
    """
    return await _agents._admin().unarchive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/monitor/check-now")
async def monitor_check_now(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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


@router.put("/issues/{issue_id}/watchdog")
async def upsert_issue_watchdog(
    issue_id: CompanyIssueId,
    body: UpsertIssueWatchdogBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Arm (or re-point) the issue's task watchdog: a named agent that reviews
    the issue's subtree when it goes quiet. Maps to upstream
    ``PUT /api/issues/{id}/watchdog``. The issue detail already carries the
    active watchdog as ``issue.watchdog``, so there is no GET here.

    Upstream evaluates the watchdog as soon as it is armed, so on a subtree
    that is already quiet this opens the review task and wakes the agent right
    away: a spend front door like ``monitor/check-now``, gated the same way.
    The DELETE below is deliberately not gated, so a watchdog can always be
    removed.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().upsert_issue_watchdog(
        issue_id=issue_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/issues/{issue_id}/watchdog")
async def delete_issue_watchdog(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Remove the issue's task watchdog. Maps to upstream
    ``DELETE /api/issues/{id}/watchdog``."""
    return await _agents._admin().delete_issue_watchdog(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


async def _require_company_issue(issue_id: str, ctx: TeamsContext) -> None:
    """404 unless ``issue_id`` belongs to the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company — so the session alone does not pin an
    issue to THIS workspace. Same check as ``session_work``'s linked-issue fence.
    """
    issue = await _agents._admin().get_issue(issue_id=issue_id, session_cookie=ctx.session_cookie)
    if not isinstance(issue, dict) or issue.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Issue not found")


@router.post("/issues/{issue_id}/scheduled-retry/retry-now")
async def retry_scheduled_retry_now(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Pull the issue's scheduled retry forward (the ported IssueScheduledRetryCard's
    Retry now). Maps to upstream ``POST /api/issues/{id}/scheduled-retry/retry-now``.

    Promoting a retry starts a run, so like ``monitor/check-now`` this is a spend
    front door: the free-tier cap is checked before the company fence and the call.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().retry_scheduled_retry_now(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/recovery-actions")
async def list_issue_recovery_actions(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """The issue's recovery action (``{active, actions}``), for the ported
    IssueRecoveryActionCard. Maps to upstream ``GET /api/issues/{id}/recovery-actions``."""
    return await _agents._admin().list_issue_recovery_actions(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/recovery-actions/resolve")
async def resolve_issue_recovery_action(
    issue_id: CompanyIssueId,
    body: ResolveIssueRecoveryActionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Record the human's decision on a boarded recovery (Try again / Mark done /
    Send for review / False positive). Maps to upstream
    ``POST /api/issues/{id}/recovery-actions/resolve``; Paperclip's own 4xx reason
    (stale action, board-only outcome) reaches the caller via main.py's
    ``PaperclipApiError`` handler."""
    return await _agents._admin().resolve_issue_recovery_action(
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


# ----------------------------------------------------------------------
# External objects: the GitHub PRs / issues an issue mentions, with live
# status. Upstream gates the whole feature on the instance flag
# ``enableExternalObjects``; while it is off these answer empty, not an error.
# ----------------------------------------------------------------------


@router.get("/issues/{issue_id}/external-objects")
async def list_issue_external_objects(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """The external objects mentioned on an issue. Maps to upstream
    ``GET /api/issues/{id}/external-objects`` (a bare array, wrapped here)."""
    rows = await _agents._admin().list_issue_external_objects(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "externalObjects")


@router.post("/issues/{issue_id}/external-objects/refresh")
async def refresh_issue_external_objects(
    issue_id: CompanyIssueId,
    body: RefreshExternalObjectsBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Re-resolve an issue's external objects now. Maps to upstream
    ``POST /api/issues/{id}/external-objects/refresh``. It calls GitHub but
    wakes no agent, so it is not a spend door and takes no trial gate."""
    return await _agents._admin().refresh_issue_external_objects(
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/external-object-summaries")
async def list_external_object_summaries(body: ExternalObjectSummariesBody, ctx: TeamsContext = Depends(_ctx)):
    """Status rollups for a page of issues, one request per list. Maps to upstream
    ``POST /api/companies/{companyId}/issues/external-object-summaries``.

    The ids are body-supplied, so there is no per-id fence dependency: the
    company is ``ctx.company_id`` (never the caller's), and upstream selects the
    requested ids ``WHERE company_id = :companyId`` and then drops any the
    caller cannot read, so a foreign id simply has no entry in the answer.
    """
    return await _agents._admin().list_external_object_summaries(
        company_id=ctx.company_id,
        issue_ids=body.issue_ids,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-read")
async def mark_issue_read(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Mark an issue as read for the signed-in user.

    Maps to upstream ``POST /api/issues/{id}/read``.
    """
    return await _agents._admin().mark_issue_read(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-unread")
async def mark_issue_unread(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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
async def list_issue_comments(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """List comments on an issue, with human authors resolved to names.

    Maps to upstream ``GET /api/issues/{id}/comments``; rows pass through
    verbatim except for the ``_enrich_comment_authors`` stamp.
    """
    payload = await _agents._admin().list_issue_comments(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    try:
        await _enrich_comment_authors(ctx, payload)
    except Exception:
        # Enrichment must never take down the thread — return upstream verbatim.
        logger.exception("issues.comments: author enrichment failed; returning raw payload")
    return payload


@router.post("/issues/{issue_id}/comments")
async def add_issue_comment(
    issue_id: CompanyIssueId,
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


@router.delete("/issues/{issue_id}/comments/{comment_id}")
async def delete_issue_comment(
    issue_id: CompanyIssueId,
    comment_id: str = Path(..., pattern=_UUID_RE),
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete (tombstone) the caller's own comment — the ported task chat's bubble
    action. Maps to upstream ``DELETE /api/issues/{id}/comments/{commentId}``;
    Paperclip itself 403s a non-author. ``?mode=cancel`` is deliberately not
    forwarded (queued-comment cancel is a separate, unported surface)."""
    return await _agents._admin().delete_issue_comment(
        issue_id=issue_id,
        comment_id=comment_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/live-runs")
async def list_issue_live_runs(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """Queued/running runs on an issue — the ported task chat's live tail source.
    Maps to upstream ``GET /api/issues/{id}/live-runs`` (a bare array)."""
    return await _agents._admin().list_issue_live_runs(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


# Activity ``details`` echo whatever the writer logged. Upstream already blanks
# secret-named keys, but an issue event can still carry adapter overrides,
# usage/cost numbers or provider traces, none of which the BFF exposes.
_ACTIVITY_REDACT_KEY_RE = re.compile(
    r"adapter|runtime[-_]?config|api[-_]?key|token|secret|passw|credential|private[-_]?key|cookie"
    r"|authorization|bearer|jwt|provider[-_]?trace|budget|cost|join[-_]?request|instance[-_]?setting"
    r"|config[-_]?rollback|plugin",
    re.IGNORECASE,
)
# Stock renders at most 20 ledger items; upstream returns the whole log unpaged.
_ISSUE_ACTIVITY_MAX = 200


def _redact_activity(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_activity(v) for k, v in value.items() if not _ACTIVITY_REDACT_KEY_RE.search(k)}
    if isinstance(value, list):
        return [_redact_activity(v) for v in value]
    return value


@router.get("/issues/{issue_id}/activity")
async def list_issue_activity(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """The issue's activity events (the ported ledger's interleaved rows and
    write-denial notices). Maps to upstream ``GET /api/issues/{id}/activity``,
    which takes no query params; the newest ``_ISSUE_ACTIVITY_MAX`` are kept."""
    payload = await _agents._admin().list_issue_activity(issue_id=issue_id, session_cookie=ctx.session_cookie)
    events = payload if isinstance(payload, list) else []
    return [_redact_activity(e) for e in events[:_ISSUE_ACTIVITY_MAX] if isinstance(e, dict)]


@router.get("/issues/{issue_id}/active-run")
async def get_issue_active_run(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """The issue's active run (or ``null``). Maps to upstream
    ``GET /api/issues/{id}/active-run``."""
    return await _agents._admin().get_issue_active_run(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/children")
async def list_issue_children(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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
    issue_id: CompanyIssueId,
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
async def list_issue_approvals(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """List the approvals linked to an issue (the issue-detail Approvals section)."""
    rows = await _agents._admin().list_issue_approvals(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "approvals")


@router.get("/issues/{issue_id}/runs")
async def list_issue_runs(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """List the agent runs for an issue (the issue-detail Runs section)."""
    rows = await _agents._admin().list_issue_runs(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "runs")


@router.get("/issues/{issue_id}/documents")
async def list_issue_documents(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """List the documents attached to an issue (the issue-detail Documents section)."""
    rows = await _agents._admin().list_issue_documents(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "documents")


# ----------------------------------------------------------------------
# Issue work-products (typed deliverables + review lifecycle)
#
# Work-products are an agent's first-class deliverables (preview_url /
# pull_request / artifact / document / …), each with a reviewState the board
# can drive (needs_board_review → approved | changes_requested). They power
# the issue-detail "Output" section + the company Artifacts gallery. Distinct
# from raw attachments (a file) — a work-product is the reviewable thing.
# ----------------------------------------------------------------------


@router.get("/issues/{issue_id}/work-products")
async def list_issue_work_products(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """List the work-products produced for an issue (the issue-detail Output section).

    Maps to upstream ``GET /api/issues/{id}/work-products`` (issue-scoped;
    company asserted upstream via the session cookie). Upstream returns a bare
    array, so it is wrapped ``{workProducts: [...]}`` like the sibling list
    surfaces.
    """
    rows = await _agents._admin().list_issue_work_products(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "workProducts")


@router.post("/issues/{issue_id}/work-products")
async def create_issue_work_product(
    issue_id: CompanyIssueId,
    body: CreateWorkProductBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a work-product on an issue. Body whitelisted by ``CreateWorkProductBody``.

    Maps to upstream ``POST /api/issues/{id}/work-products``. ``exclude_none``
    drops unset optionals so upstream applies its own defaults;
    ``by_alias`` emits the camelCase wire shape (``reviewState`` / ``isPrimary``
    / ``projectId``).
    """
    return await _agents._admin().create_issue_work_product(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )


def _find_work_product(rows: Any, work_product_id: str) -> Optional[dict]:
    """Locate a work-product row by id in an upstream issue-scoped list payload.

    ``list_issue_work_products`` returns upstream's bare array; tolerate a
    ``{workProducts: [...]}`` envelope too so the lookup is robust to shape.
    """
    if isinstance(rows, dict):
        rows = rows.get("workProducts")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == work_product_id:
            return row
    return None


@router.patch("/issues/{issue_id}/work-products/{work_product_id}")
async def patch_work_product(
    issue_id: CompanyIssueId,
    work_product_id: str,
    body: PatchWorkProductBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Record a board review verdict on a work-product — Approve / Request-changes.

    Maps to upstream ``PATCH /api/work-products/{id}``, but GUARDED at the BFF:

    - The body (``PatchWorkProductBody``) is narrowed to the review decision
      only, so this route can never rewrite a deliverable's status/title/url.
    - The transition is forwarded ONLY when the target work-product (a) belongs
      to the ``issue_id`` in the path and (b) is currently ``needs_board_review``.
      Otherwise 404 / 409 — never forwarded.

    Upstream exposes no GET-by-id for a single work-product, so the current row
    is read from the issue-scoped list (``GET /api/issues/{id}/work-products``);
    the path ``issue_id`` both scopes that read and asserts the product is part
    of the viewed issue. Upstream only company-scopes the raw id, so without
    this guard a same-company caller who learned any work_product_id could
    approve / request-changes an unrelated or already-decided deliverable.
    """
    rows = await _agents._admin().list_issue_work_products(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    wp = _find_work_product(rows, work_product_id)
    if wp is None:
        raise HTTPException(status_code=404, detail="Work product not found on this issue")
    if wp.get("reviewState") != "needs_board_review":
        raise HTTPException(
            status_code=409,
            detail="Work product is not awaiting board review",
        )
    return await _agents._admin().update_work_product(
        session_cookie=ctx.session_cookie,
        work_product_id=work_product_id,
        body=body.model_dump(by_alias=True),
    )


@router.post("/issues/{issue_id}/work-products/{work_product_id}/review-document")
async def ensure_work_product_review_document(
    issue_id: CompanyIssueId,
    work_product_id: IssueId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Materialize a Markdown work product's review document — the ported issue
    Output section's expand-to-preview (stock ``issuesApi.ensureWorkProductReviewDocument``).

    Maps to upstream ``POST /api/issues/{id}/work-products/{wpId}/review-document``,
    a fixed issue-scoped path. The company fence pins the issue to THIS workspace;
    upstream then 404s a work product that is not on that issue.
    """
    return await _agents._admin().ensure_work_product_review_document(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        work_product_id=work_product_id,
    )


@router.delete("/issues/{issue_id}/work-products/{work_product_id}")
async def delete_work_product(
    issue_id: CompanyIssueId,
    work_product_id: Annotated[str, Path(pattern=_UUID_RE)],
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete a work-product — the Artifacts gallery's per-artifact delete for
    a ``work_product:<id>`` row.

    Maps to upstream ``DELETE /api/work-products/{id}``, which is NOT
    issue-scoped: upstream only checks the caller can access the parent
    issue's company, and a Paperclip user can belong to several. So the work
    product must be on ``issue_id`` (already fenced to this workspace) or it
    is a 404, never forwarded. 404 upstream is treated as already-gone
    (idempotent) by the admin client's ``_delete``.
    """
    rows = await _agents._admin().list_issue_work_products(session_cookie=ctx.session_cookie, issue_id=issue_id)
    if _find_work_product(rows, work_product_id) is None:
        raise HTTPException(status_code=404, detail="Work product not found on this issue")
    return await _agents._admin().delete_work_product(
        session_cookie=ctx.session_cookie,
        work_product_id=work_product_id,
    )


@router.get("/artifacts")
async def list_company_artifacts(
    ctx: TeamsContext = Depends(_ctx),
    kind: Optional[str] = Query(default=None, pattern=r"^(image|video|text|document|file|all)$"),
    # Same uuid-cast guard as the issues-list ``project`` filter — upstream
    # casts these to a Postgres ``uuid``, so a malformed value 500s and leaks
    # the internal company path. Validate the shape here (local 422) first.
    project: Optional[str] = Query(default=None, pattern=_UUID_RE),
    group_issue_id: Optional[str] = Query(default=None, alias="groupIssueId", pattern=_UUID_RE),
    q: Optional[str] = Query(default=None, max_length=160),
    group_by: Optional[str] = Query(default=None, alias="groupBy", pattern=r"^(none|task|parent_task)$"),
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    cursor: Optional[str] = Query(default=None, max_length=2000),
):
    """The company Artifacts gallery — deliverables across the whole company.

    Maps to upstream ``GET /api/companies/{companyId}/artifacts``. The company
    segment is the server-derived ``ctx.company_id`` (never a client param);
    upstream ``assertCompanyAccess`` scopes it to the session cookie's company.
    Query params are validated at the boundary then forwarded so the gallery
    can drive kind/project filtering, search, task grouping, and pagination.
    Returns the upstream ``{artifacts, groups?, selectedGroup?, nextCursor}``
    object verbatim.
    """
    upstream_params: dict[str, str] = {}
    if kind is not None:
        upstream_params["kind"] = kind
    if project is not None:
        upstream_params["projectId"] = project
    if group_issue_id is not None:
        upstream_params["groupIssueId"] = group_issue_id
    if q is not None:
        upstream_params["q"] = q
    if group_by is not None:
        upstream_params["groupBy"] = group_by
    if limit is not None:
        upstream_params["limit"] = str(limit)
    if cursor is not None:
        upstream_params["cursor"] = cursor

    return await _agents._admin().list_company_artifacts(
        session_cookie=ctx.session_cookie,
        company_id=ctx.company_id,
        params=upstream_params or None,
    )


# ----------------------------------------------------------------------
# Issue attachments (binary files: images / PDFs / etc.)
# ----------------------------------------------------------------------


@router.get("/issues/{issue_id}/attachments")
async def list_issue_attachments(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
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
    issue_id: CompanyIssueId,
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


async def _require_issue_attachment(issue_id: str, attachment_id: str, ctx: TeamsContext) -> None:
    """404 unless ``attachment_id`` is on ``issue_id``.

    Upstream's attachment-by-id routes are NOT issue-scoped: they only check the
    caller can read the attachment's issue, in ANY company they belong to, and
    upstream has no attachment-metadata route. So the attachment must appear in
    the (company-fenced) issue's own list, or the request is never forwarded.
    """
    if not await _attachment_on_issue(issue_id, attachment_id, ctx):
        raise HTTPException(status_code=404, detail="Attachment not found on this issue")


async def _attachment_on_issue(issue_id: str, attachment_id: str, ctx: TeamsContext) -> bool:
    rows = await _agents._admin().list_issue_attachments(session_cookie=ctx.session_cookie, issue_id=issue_id)
    if isinstance(rows, dict):
        rows = rows.get("attachments")
    return any(isinstance(r, dict) and r.get("id") == attachment_id for r in rows or [])


async def _require_readable_attachment(issue_id: str, attachment_id: str, ctx: TeamsContext) -> None:
    """READ-only widening of ``_require_issue_attachment`` (delete stays strict).

    Agents upload to one task and paste the link into a related task's thread
    (the upload helper takes ``--issue-id``), typically parent <-> subtask. So on
    a miss, the attachment may also be on ``issue_id``'s parent or one of its
    DIRECT children — each candidate itself company-checked, anything foreign
    skipped. Bounded: one level up, one level down, children capped at one list
    call. Grandchildren and siblings still 404.
    """
    if await _attachment_on_issue(issue_id, attachment_id, ctx):
        return
    admin = _agents._admin()
    candidates: list[str] = []
    issue = await admin.get_issue(issue_id=issue_id, session_cookie=ctx.session_cookie)
    parent_id = issue.get("parentId") if isinstance(issue, dict) else None
    if parent_id:
        parent = await admin.get_issue(issue_id=parent_id, session_cookie=ctx.session_cookie)
        if isinstance(parent, dict) and parent.get("companyId") == ctx.company_id:
            candidates.append(parent_id)
    rows = await admin.list_issues(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params={"descendantOf": issue_id},
    )
    if isinstance(rows, dict):
        rows = rows.get("issues")
    for row in rows if isinstance(rows, list) else []:
        # descendantOf is the whole subtree; only DIRECT children count.
        if (
            isinstance(row, dict)
            and row.get("parentId") == issue_id
            and row.get("companyId") == ctx.company_id
            and isinstance(row.get("id"), str)
        ):
            candidates.append(row["id"])
    for candidate in candidates:
        if await _attachment_on_issue(candidate, attachment_id, ctx):
            return
    raise HTTPException(status_code=404, detail="Attachment not found on this issue")


def stream_upstream(upstream) -> StreamingResponse:
    """Pipe an already-open upstream content response to the browser.

    Shared by the issue-attachment and case-attachment content routes.
    """
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


@router.get("/issues/{issue_id}/attachments/{attachment_id}/content")
async def get_attachment_content(
    issue_id: CompanyIssueId,
    attachment_id: IssueId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Stream an attachment's bytes back to the browser.

    Proxies upstream ``GET /api/attachments/{id}/content`` once the attachment
    is proven to be on ``issue_id`` or its company-checked parent / a direct
    child (see ``_require_readable_attachment``).

    Streamed via ``StreamingResponse`` over the upstream
    ``aiter_bytes()`` so a large file never buffers in backend memory. The
    upstream ``Content-Type`` / ``Content-Disposition`` / ``Content-Length``
    are copied through; ``X-Content-Type-Options: nosniff`` is forwarded if
    present so the browser doesn't MIME-sniff an untrusted upload.
    """
    await _require_readable_attachment(issue_id, attachment_id, ctx)
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
    return stream_upstream(upstream)


@router.delete("/issues/{issue_id}/attachments/{attachment_id}")
async def delete_issue_attachment(
    issue_id: CompanyIssueId,
    attachment_id: IssueId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete an attachment.

    Maps to upstream ``DELETE /api/attachments/{id}``, which is NOT issue-scoped:
    upstream only checks the caller can access the attachment's company, and a
    Paperclip user can belong to several. So the attachment must be on
    ``issue_id`` (already fenced to this workspace) or it is a 404, never
    forwarded. 404 upstream is treated as already-gone by the admin client.
    """
    await _require_issue_attachment(issue_id, attachment_id, ctx)
    return await _agents._admin().delete_attachment(
        session_cookie=ctx.session_cookie,
        attachment_id=attachment_id,
    )


# ----------------------------------------------------------------------
# Plan-document review — upsert / revisions / restore / annotation threads.
#
# These enrich the issue-detail Documents section into a review surface:
# board edits mint new revisions, historical revisions can be restored, and
# inline annotation THREADS (with comments + a resolve/reopen lifecycle)
# anchor to a text selection in a specific revision.
#
# SECURITY — every route runs ``TeamsContext`` auth (``_ctx``) and forwards the
# session cookie; upstream ``assertIssueReadAllowed`` / mutation guards scope
# the issue to the cookie's company, so a cross-tenant issue id is rejected
# upstream (IDOR-safe). On TOP of that, the document ``key`` is a Postgres
# path segment upstream casts / matches on, so a malformed key is validated
# HERE (local 400) before any upstream call — mirroring the ``project`` /
# ``assignee`` uuid guards above. The mutating bodies are ``extra="forbid"``
# ``_Strict`` schemas, so a smuggled field 422s at the FastAPI boundary.
# ----------------------------------------------------------------------

import re as _re  # noqa: E402 — imported locally to keep this an additive block.

from fastapi.responses import JSONResponse  # noqa: E402

from .schemas import (  # noqa: E402
    AddDocumentAnnotationCommentBody,
    CreateDocumentAnnotationThreadBody,
    UpdateDocumentAnnotationThreadBody,
    UpsertDocumentBody,
)

# Upstream ``issueDocumentKeySchema``: lowercase letters/digits then
# ``[a-z0-9_-]``, max 64. Validate the shape at the boundary so a bad key is a
# local 400 (never forwarded for upstream to cast-error + leak the company path).
_DOCUMENT_KEY_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Thread ids are Postgres uuids upstream — guard them like ``project``.
_THREAD_ID_RE = _re.compile(_UUID_RE)


def _norm_document_key(key: str) -> str:
    """Normalize + validate a document key or raise a local 400.

    Upstream lowercases + trims the key before matching, so we do the same and
    then validate against ``issueDocumentKeySchema``. Returning the normalized
    value keeps the forwarded path byte-identical to what upstream expects.
    """
    normalized = (key or "").strip().lower()
    if not _DOCUMENT_KEY_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid document key")
    return normalized


def _require_thread_id(thread_id: str) -> str:
    """Validate an annotation thread id (uuid) or raise a local 400."""
    if not _THREAD_ID_RE.match(thread_id or ""):
        raise HTTPException(status_code=400, detail="Invalid thread id")
    return thread_id


@router.put("/issues/{issue_id}/documents/{key}")
async def upsert_issue_document(
    issue_id: CompanyIssueId,
    key: str,
    body: UpsertDocumentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create or replace an issue document body (board edit → new revision).

    Maps to upstream ``PUT /api/issues/{id}/documents/{key}``. Upstream answers
    201 on create vs 200 on update; that status is forwarded verbatim so the UI
    can tell "created" from "updated". ``exclude_none`` drops unset optionals so
    upstream applies its own defaults; ``by_alias`` emits the camelCase wire
    shape (``changeSummary`` / ``baseRevisionId``).
    """
    doc_key = _norm_document_key(key)
    status_code, document = await _agents._admin().upsert_issue_document(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )
    return JSONResponse(status_code=status_code, content=document)


@router.get("/issues/{issue_id}/documents/{key}")
async def get_issue_document(
    issue_id: CompanyIssueId,
    key: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Fetch a single issue document (with its body) — the Artifacts viewer.

    Maps to upstream ``GET /api/issues/{issueId}/documents/{key}``. Upstream
    returns the document object (``{id, key, title, format, body, ...}``) so the
    gallery's viewer can render the markdown body in place rather than
    deep-linking into the issue detail. The ``key`` is normalized + validated at
    the boundary (bad key → local 400, never forwarded for upstream to
    cast-error + leak the company path); company scope is the session cookie's
    company (``assertIssueReadAllowed`` upstream), never a client param.
    """
    doc_key = _norm_document_key(key)
    return await _agents._admin().get_issue_document(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
    )


@router.delete("/issues/{issue_id}/documents/{key}")
async def delete_issue_document(
    issue_id: CompanyIssueId,
    key: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete an issue document by key — the Artifacts gallery's per-artifact
    delete for a ``document:<id>`` row.

    Maps to upstream ``DELETE /api/issues/{issueId}/documents/{key}``. The
    ``key`` is normalized + validated at the boundary (bad key → local 400).
    Upstream requires board authentication (the admin session is a board actor)
    and scopes the delete to the session cookie's company, so a cross-tenant
    issue id is rejected upstream rather than relying on a BFF-side check. 404
    is treated as already-gone (idempotent) by the admin client's ``_delete``.
    """
    doc_key = _norm_document_key(key)
    return await _agents._admin().delete_issue_document(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
    )


@router.get("/issues/{issue_id}/documents/{key}/revisions")
async def list_issue_document_revisions(
    issue_id: CompanyIssueId,
    key: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """List a document's revision history (the rev dropdown + diff modal).

    Maps to upstream ``GET /api/issues/{id}/documents/{key}/revisions``.
    Upstream returns a bare array of ``DocumentRevision`` rows, so it is
    wrapped ``{revisions: [...]}`` like the sibling list surfaces.
    """
    doc_key = _norm_document_key(key)
    rows = await _agents._admin().list_issue_document_revisions(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
    )
    return envelope_list(rows, "revisions")


@router.post("/issues/{issue_id}/documents/{key}/revisions/{revision_id}/restore")
async def restore_issue_document_revision(
    issue_id: CompanyIssueId,
    key: str,
    revision_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Restore a historical revision as the new latest revision.

    Maps to upstream ``POST /api/issues/{id}/documents/{key}/revisions/
    {revisionId}/restore``. ``revision_id`` is validated as a uuid at the
    boundary before forwarding.
    """
    doc_key = _norm_document_key(key)
    _require_thread_id(revision_id)  # revision ids are uuids too — reuse the guard.
    return await _agents._admin().restore_issue_document_revision(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        revision_id=revision_id,
    )


@router.get("/issues/{issue_id}/documents/{key}/annotations")
async def list_document_annotations(
    issue_id: CompanyIssueId,
    key: str,
    ctx: TeamsContext = Depends(_ctx),
    status: Optional[str] = Query(default=None, pattern=r"^(open|resolved|all)$"),
    include_comments: bool = Query(default=True, alias="includeComments"),
):
    """List annotation threads for a document (the thread panel + count chip).

    Maps to upstream ``GET /api/issues/{id}/documents/{key}/annotations``.
    ``status`` filters open | resolved | all (upstream defaults to open);
    ``includeComments`` hydrates each thread's comment list. Wrapped
    ``{threads: [...]}`` like the sibling list surfaces.
    """
    doc_key = _norm_document_key(key)
    rows = await _agents._admin().list_document_annotations(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        status=status,
        include_comments=include_comments,
    )
    return envelope_list(rows, "threads")


@router.post("/issues/{issue_id}/documents/{key}/annotations")
async def create_document_annotation(
    issue_id: CompanyIssueId,
    key: str,
    body: CreateDocumentAnnotationThreadBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create an annotation thread anchored to a text selection.

    Maps to upstream ``POST /api/issues/{id}/documents/{key}/annotations``.
    A new thread pings the assigned agent server-side (a review comment is a
    spend front door), so the free-tier $3/day cap is checked HERE, BEFORE the
    proxy call reaches Paperclip — mirroring ``add_issue_comment``.
    """
    doc_key = _norm_document_key(key)
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().create_document_annotation(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )


@router.post("/issues/{issue_id}/documents/{key}/annotations/{thread_id}/comments")
async def add_document_annotation_comment(
    issue_id: CompanyIssueId,
    key: str,
    thread_id: str,
    body: AddDocumentAnnotationCommentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Append a reply comment to an annotation thread.

    Maps to upstream ``POST /api/issues/{id}/documents/{key}/annotations/
    {threadId}/comments``. Like a new thread, a reply pings the assigned agent,
    so the free-tier cap is checked HERE before forwarding.
    """
    doc_key = _norm_document_key(key)
    _require_thread_id(thread_id)
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().add_document_annotation_comment(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        thread_id=thread_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
    )


@router.patch("/issues/{issue_id}/documents/{key}/annotations/{thread_id}")
async def update_document_annotation_thread(
    issue_id: CompanyIssueId,
    key: str,
    thread_id: str,
    body: UpdateDocumentAnnotationThreadBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Resolve or reopen an annotation thread.

    Maps to upstream ``PATCH /api/issues/{id}/documents/{key}/annotations/
    {threadId}``. A status flip does NOT dispatch an agent run, so (unlike
    create/reply) it is not cap-gated.
    """
    doc_key = _norm_document_key(key)
    _require_thread_id(thread_id)
    return await _agents._admin().update_document_annotation_thread(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        key=doc_key,
        thread_id=thread_id,
        body=body.model_dump(by_alias=True),
    )
