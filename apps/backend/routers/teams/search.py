"""Teams BFF — cross-entity company search.

Surfaces Paperclip's ranked, filtered, cross-entity search
(``GET /api/companies/:companyId/search`` + the bulk ``/search/extract``
variant) so the Teams UI can search across issues / comments / documents /
artifacts / agents / projects instead of the old client-side ``.includes()``
substring filter over the ≤10 already-loaded rows.

SECURITY — company scoping (IDOR): the ``companyId`` path segment is ALWAYS
``ctx.company_id`` (the server-derived company on the resolved
``TeamsContext``), NEVER a client-supplied value. Upstream additionally
``assertCompanyAccess`` + runs a ``company_scope:read`` decision against the
session cookie's actor, so even a forged id would be rejected upstream — but
the id never reaches the wire from the client here.

Every query param is shape-checked at THIS boundary before the upstream call so
a malformed value surfaces as a clean local 422 rather than an opaque upstream
error: ``projectId`` / ``labelId`` / ``assigneeAgentId`` are matched against the
uuid / sentinel forms upstream expects, and ``scope`` / ``sort`` / ``status`` /
``priority`` are checked against the supported enums. The upstream response
object is returned verbatim (it is already an envelope:
``{results, countsByType, …}``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import agents as _agents
from .deps import TeamsContext

router = APIRouter()
_ctx = _agents._ctx

# Mirrors upstream Paperclip constants (packages/shared/src/constants.ts) and
# the search validators (packages/shared/src/{types,validators}/search.ts).
# Kept local so the BFF boundary is self-validating and does not import the
# Node shared package.
_SEARCH_SCOPES = frozenset({"all", "issues", "comments", "documents", "artifacts", "agents", "projects"})
_SEARCH_SORTS = frozenset({"relevance", "updated", "created", "priority"})
_ISSUE_STATUSES = frozenset({"backlog", "todo", "in_progress", "in_review", "done", "blocked", "cancelled"})
_ISSUE_PRIORITIES = frozenset({"critical", "high", "medium", "low"})
_EXTRACT_SCOPES = frozenset({"all", "issues", "comments", "documents"})
_EXTRACT_KINDS = frozenset({"literal", "url"})

# Boundary shape check for the uuid filter params — upstream expects a uuid
# here, so validating locally yields a cleaner 422 than the opaque upstream
# error a malformed value would return.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# ``assigneeAgentId`` also casts to uuid upstream; ``"null"`` filters unassigned.
# (Upstream accepts only uuid/null here — the search parser resolves ``me`` to a
# concrete agent id or an ``assigneeUserId`` before it reaches this param.)
_ASSIGNEE_AGENT_RE = r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|null)$"
# Duration filter (``24h`` / ``7d`` / ``4w`` / ``3m``), mirrors the upstream
# ``UPDATED_WITHIN_RE``.
_UPDATED_WITHIN_RE = r"^[1-9]\d{0,2}(h|d|w|m)$"


def _validate_updated_after(value: Optional[str]) -> Optional[str]:
    """Return ``value`` unchanged, or 422 unless it parses as ISO-8601.

    Upstream casts ``updatedAfter`` with ``new Date(...)`` and its validator
    4xxs on an unparseable value. The global ``PaperclipApiError`` handler
    surfaces ``str(exc)`` for an upstream 4xx, which embeds the internal
    ``/api/companies/{company_id}/search`` path — so a malformed value would
    leak the company-scoped upstream path. Rejecting it at THIS boundary yields
    a clean local 422 and never reaches the proxy, mirroring the other filter
    checks in this file.
    """
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="updatedAfter must be a valid ISO-8601 datetime",
        )
    return value


def _validate_enum_list(
    values: Optional[list[str]],
    allowed: frozenset[str],
    field: str,
) -> Optional[list[str]]:
    """Return the (de-duped) list unchanged, or 422 on the first bad value.

    Upstream's ``parseEnumList`` also accepts comma-separated values inside a
    single param, so each entry is split on commas before validation — a client
    may send either ``?status=todo&status=done`` or ``?status=todo,done``.
    """
    if not values:
        return None
    out: list[str] = []
    for raw in values:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if item not in allowed:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} contains an unsupported value: {item!r}",
                )
            if item not in out:
                out.append(item)
    return out or None


@router.get("/search")
async def company_search(
    ctx: TeamsContext = Depends(_ctx),
    q: Optional[str] = Query(default=None, max_length=200),
    scope: Optional[str] = Query(default=None, max_length=32),
    sort: Optional[str] = Query(default=None, max_length=32),
    limit: Optional[int] = Query(default=None, ge=1, le=50),
    offset: Optional[int] = Query(default=None, ge=0, le=200),
    status: Optional[list[str]] = Query(default=None),
    priority: Optional[list[str]] = Query(default=None),
    assignee_agent_id: Optional[str] = Query(default=None, alias="assigneeAgentId", pattern=_ASSIGNEE_AGENT_RE),
    assignee_user_id: Optional[str] = Query(default=None, alias="assigneeUserId", max_length=200),
    project_id: Optional[str] = Query(default=None, alias="projectId", pattern=_UUID_RE),
    label_id: Optional[str] = Query(default=None, alias="labelId", pattern=_UUID_RE),
    updated_within: Optional[str] = Query(default=None, alias="updatedWithin", pattern=_UPDATED_WITHIN_RE),
    updated_after: Optional[str] = Query(default=None, alias="updatedAfter", max_length=64),
):
    """Ranked cross-entity search over the caller's company.

    Forwards to upstream ``GET /api/companies/{companyId}/search`` with the
    company derived from ``ctx`` (IDOR-safe). Returns the upstream
    ``CompanySearchResponse`` verbatim.
    """
    upstream_params: dict[str, Any] = {}
    if q is not None:
        upstream_params["q"] = q
    if scope is not None:
        if scope not in _SEARCH_SCOPES:
            raise HTTPException(status_code=422, detail="scope must be a supported search scope")
        upstream_params["scope"] = scope
    if sort is not None:
        if sort not in _SEARCH_SORTS:
            raise HTTPException(status_code=422, detail="sort must be relevance, updated, created, or priority")
        upstream_params["sort"] = sort
    if limit is not None:
        upstream_params["limit"] = str(limit)
    if offset is not None:
        upstream_params["offset"] = str(offset)
    statuses = _validate_enum_list(status, _ISSUE_STATUSES, "status")
    if statuses is not None:
        upstream_params["status"] = statuses
    priorities = _validate_enum_list(priority, _ISSUE_PRIORITIES, "priority")
    if priorities is not None:
        upstream_params["priority"] = priorities
    if assignee_agent_id is not None:
        upstream_params["assigneeAgentId"] = assignee_agent_id
    if assignee_user_id is not None:
        # The search parser emits the ``me`` sentinel for the "Assigned to me"
        # filter. Upstream compares ``assigneeUserId`` as a LITERAL user id, so
        # forwarding ``me`` matches nothing — resolve it to the caller's real
        # Paperclip user id (server-derived, IDOR-safe) before forwarding.
        upstream_params["assigneeUserId"] = (
            ctx.paperclip_user_id if assignee_user_id.lower() == "me" else assignee_user_id
        )
    if project_id is not None:
        upstream_params["projectId"] = project_id
    if label_id is not None:
        upstream_params["labelId"] = label_id
    if updated_within is not None:
        upstream_params["updatedWithin"] = updated_within
    if updated_after is not None:
        upstream_params["updatedAfter"] = _validate_updated_after(updated_after)

    return await _agents._admin().search_company(
        session_cookie=ctx.session_cookie,
        company_id=ctx.company_id,
        params=upstream_params or None,
    )


@router.get("/search/extract")
async def company_search_extract(
    ctx: TeamsContext = Depends(_ctx),
    contains: str = Query(max_length=200),
    kind: Optional[str] = Query(default=None, max_length=16),
    scope: Optional[str] = Query(default=None, max_length=32),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    offset: Optional[int] = Query(default=None, ge=0, le=5000),
    matches_per_issue: Optional[int] = Query(default=None, alias="matchesPerIssue", ge=1, le=200),
    status: Optional[list[str]] = Query(default=None),
    updated_within: Optional[str] = Query(default=None, alias="updatedWithin", pattern=_UPDATED_WITHIN_RE),
    updated_after: Optional[str] = Query(default=None, alias="updatedAfter", max_length=64),
):
    """Bulk literal / URL match extraction across the company's issues.

    Forwards to upstream ``GET /api/companies/{companyId}/search/extract``
    (company from ``ctx``, IDOR-safe). ``updatedWithin`` and ``updatedAfter``
    are mutually exclusive upstream, so reject the combination locally.
    """
    # Enforce the two-character minimum against the TRIMMED value. Upstream's
    # extract validator trims ``contains`` before applying its own min-length
    # check, so an untrimmed local floor (e.g. ``"  "`` or ``" a"``) would slip
    # through here, 4xx upstream, and the global ``PaperclipApiError`` handler
    # would surface ``str(exc)`` — leaking the internal company-scoped path.
    if len(contains.strip()) < 2:
        raise HTTPException(
            status_code=422,
            detail="contains must be at least 2 characters",
        )

    if updated_within is not None and updated_after is not None:
        raise HTTPException(
            status_code=422,
            detail="updatedWithin and updatedAfter cannot be used together",
        )

    upstream_params: dict[str, Any] = {"contains": contains}
    if kind is not None:
        if kind not in _EXTRACT_KINDS:
            raise HTTPException(status_code=422, detail="kind must be literal or url")
        upstream_params["kind"] = kind
    if scope is not None:
        if scope not in _EXTRACT_SCOPES:
            raise HTTPException(status_code=422, detail="scope must be all, issues, comments, or documents")
        upstream_params["scope"] = scope
    if limit is not None:
        upstream_params["limit"] = str(limit)
    if offset is not None:
        upstream_params["offset"] = str(offset)
    if matches_per_issue is not None:
        upstream_params["matchesPerIssue"] = str(matches_per_issue)
    statuses = _validate_enum_list(status, _ISSUE_STATUSES, "status")
    if statuses is not None:
        upstream_params["status"] = statuses
    if updated_within is not None:
        upstream_params["updatedWithin"] = updated_within
    if updated_after is not None:
        upstream_params["updatedAfter"] = _validate_updated_after(updated_after)

    return await _agents._admin().search_company_extract(
        session_cookie=ctx.session_cookie,
        company_id=ctx.company_id,
        params=upstream_params,
    )
