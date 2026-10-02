"""Teams BFF — Cases (structured work-product records).

Proxies exactly the calls the ported Cases panels make (upstream
``ui/src/api/cases.ts`` @ v2026.916.1-isol8.3): list, detail, the status/label
patch, the event feed, an issue's linked cases, and the case-document review
surface (revisions, restore, annotations) plus attachment bytes. Cases are
agent-authored, so there is no create, link or upload route here.

Upstream gates the whole feature on the instance flag ``enableCases``: while it
is off every call answers 403 "Cases are disabled", which the global
``PaperclipApiError`` handler forwards and the panel renders as "not enabled".

Nothing here wakes an agent, so no route takes ``require_trial_runway``.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .issues import CompanyIssueId, IssueId, _require_thread_id, stream_upstream
from .schemas import (
    AddDocumentAnnotationCommentBody,
    CreateDocumentAnnotationThreadBody,
    PatchCaseBody,
    UpdateDocumentAnnotationThreadBody,
)

router = APIRouter()
_ctx = _agents._ctx

# Upstream ``documentKeySchema`` is ``[A-Za-z0-9_.:-]{1,120}`` (case-sensitive,
# not lowercased like issue document keys). The leading alphanumeric is ours:
# it keeps a ``..`` segment from rewriting the fixed upstream path.
CaseDocKey = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")]


async def _company_case(case_id: IssueId, ctx: TeamsContext = Depends(_ctx)) -> dict:
    """Route dependency: the case, or 404 unless it is in the caller's workspace company.

    ``case_id`` may be the uuid or the ``LB-C7`` identifier, and a Paperclip
    user can belong to several companies, so the session alone does not pin a
    case to THIS workspace. Routes forward the fetched case's own uuid. Every
    ``{case_id}`` route takes ``CompanyCase``; ``test_teams_company_fences``
    fails on one that doesn't.
    """
    case = await _agents._admin().get_case(case_id=case_id, session_cookie=ctx.session_cookie)
    if not isinstance(case, dict) or not case.get("id") or case.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


CompanyCase = Annotated[dict, Depends(_company_case)]


@router.get("/cases")
async def list_cases(
    ctx: TeamsContext = Depends(_ctx),
    types: Optional[list[str]] = Query(default=None, max_length=50),
    status: Optional[str] = Query(default=None, max_length=120),
    statuses: Optional[list[str]] = Query(default=None, max_length=10),
    project_ids: Optional[list[str]] = Query(default=None, alias="projectIds", max_length=100),
    include_no_project: bool = Query(default=False, alias="includeNoProject"),
    label_id: Optional[str] = Query(default=None, alias="labelId", max_length=64),
    parent: Optional[str] = Query(default=None, max_length=64),
    q: Optional[str] = Query(default=None, min_length=1, max_length=200),
    include_ancestors: bool = Query(default=False, alias="includeAncestors"),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
):
    """List the company's cases, wrapped ``{cases: [...]}``.

    Filters pass straight through to upstream ``GET /api/companies/{co}/cases``,
    which validates them itself (a bad status / uuid is its 400, not a 500).
    """
    params: dict = {}
    if types:
        params["types"] = types
    if status is not None:
        params["status"] = status
    if statuses:
        params["statuses"] = statuses
    if project_ids:
        params["projectIds"] = project_ids
    if include_no_project:
        params["includeNoProject"] = "true"
    if label_id is not None:
        params["labelId"] = label_id
    if parent is not None:
        params["parent"] = parent
    if q is not None:
        params["q"] = q
    if include_ancestors:
        params["includeAncestors"] = "true"
    if limit is not None:
        params["limit"] = limit
    rows = await _agents._admin().list_cases(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params=params or None,
    )
    return envelope_list(rows, "cases")


@router.get("/issues/{issue_id}/cases")
async def list_issue_cases(issue_id: CompanyIssueId, ctx: TeamsContext = Depends(_ctx)):
    """The cases linked to an issue (the issue page's Cases rail), wrapped ``{cases: [...]}``."""
    rows = await _agents._admin().list_issue_cases(issue_id=issue_id, session_cookie=ctx.session_cookie)
    return envelope_list(rows, "cases")


@router.get("/cases/{case_id}")
async def get_case(case: CompanyCase):
    """One case with labels, issue links, documents and attachments (the fence's own read)."""
    return case


@router.patch("/cases/{case_id}")
async def patch_case(case: CompanyCase, body: PatchCaseBody, ctx: TeamsContext = Depends(_ctx)):
    """Change a case's status and/or replace its label set. Returns the full detail."""
    return await _agents._admin().patch_case(
        case_id=case["id"],
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.get("/cases/{case_id}/events")
async def list_case_events(
    case: CompanyCase,
    ctx: TeamsContext = Depends(_ctx),
    limit: int = Query(default=100, ge=1, le=500),
):
    """A case's event feed, newest first, wrapped ``{events: [...]}``."""
    rows = await _agents._admin().list_case_events(case_id=case["id"], limit=limit, session_cookie=ctx.session_cookie)
    return envelope_list(rows, "events")


@router.get("/cases/{case_id}/attachments/{attachment_id}/content")
async def get_case_attachment_content(case: CompanyCase, attachment_id: IssueId, ctx: TeamsContext = Depends(_ctx)):
    """Stream a case attachment's bytes.

    Upstream serves them from the company-wide ``/api/assets/{assetId}/content``,
    so the asset id is taken from THIS case's own attachment list, never from
    the client.
    """
    asset_id = next(
        (
            (att.get("asset") or {}).get("id")
            for att in case.get("attachments") or []
            if isinstance(att, dict) and att.get("id") == attachment_id
        ),
        None,
    )
    if not asset_id:
        raise HTTPException(status_code=404, detail="Attachment not found on this case")
    upstream = await _agents._admin().open_asset_stream(asset_id=asset_id, session_cookie=ctx.session_cookie)
    return stream_upstream(upstream)


# ---- Case documents: the same review surface as issue documents ----


@router.get("/cases/{case_id}/documents/{key}/revisions")
async def list_case_document_revisions(case: CompanyCase, key: CaseDocKey, ctx: TeamsContext = Depends(_ctx)):
    """A case document's revision history, ``{revisions: [...]}`` like the issue route."""
    upstream = await _agents._admin().list_case_document_revisions(
        case_id=case["id"], key=key, session_cookie=ctx.session_cookie
    )
    return envelope_list(upstream, "revisions")


@router.post("/cases/{case_id}/documents/{key}/revisions/{revision_id}/restore")
async def restore_case_document_revision(
    case: CompanyCase, key: CaseDocKey, revision_id: str, ctx: TeamsContext = Depends(_ctx)
):
    """Restore a historical revision as the new latest revision."""
    _require_thread_id(revision_id)  # revision ids are uuids too.
    return await _agents._admin().restore_case_document_revision(
        case_id=case["id"], key=key, revision_id=revision_id, session_cookie=ctx.session_cookie
    )


@router.get("/cases/{case_id}/documents/{key}/annotations")
async def list_case_document_annotations(
    case: CompanyCase,
    key: CaseDocKey,
    ctx: TeamsContext = Depends(_ctx),
    status: Optional[str] = Query(default=None, pattern=r"^(open|resolved|all)$"),
    include_comments: bool = Query(default=True, alias="includeComments"),
):
    """Annotation threads on a case document, wrapped ``{threads: [...]}``."""
    rows = await _agents._admin().list_case_document_annotations(
        case_id=case["id"],
        key=key,
        status=status,
        include_comments=include_comments,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "threads")


@router.post("/cases/{case_id}/documents/{key}/annotations")
async def create_case_document_annotation(
    case: CompanyCase,
    key: CaseDocKey,
    body: CreateDocumentAnnotationThreadBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create an annotation thread anchored to a text selection."""
    return await _agents._admin().create_case_document_annotation(
        case_id=case["id"],
        key=key,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/cases/{case_id}/documents/{key}/annotations/{thread_id}/comments")
async def add_case_document_annotation_comment(
    case: CompanyCase,
    key: CaseDocKey,
    thread_id: str,
    body: AddDocumentAnnotationCommentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Reply on an annotation thread."""
    _require_thread_id(thread_id)
    return await _agents._admin().add_case_document_annotation_comment(
        case_id=case["id"],
        key=key,
        thread_id=thread_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/cases/{case_id}/documents/{key}/annotations/{thread_id}")
async def update_case_document_annotation_thread(
    case: CompanyCase,
    key: CaseDocKey,
    thread_id: str,
    body: UpdateDocumentAnnotationThreadBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Resolve or reopen an annotation thread."""
    _require_thread_id(thread_id)
    return await _agents._admin().update_case_document_annotation_thread(
        case_id=case["id"],
        key=key,
        thread_id=thread_id,
        body=body.model_dump(by_alias=True),
        session_cookie=ctx.session_cookie,
    )
