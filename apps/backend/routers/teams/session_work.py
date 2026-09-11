"""Explicit, per-viewer work references. Paperclip never owns a native session."""

import uuid
from typing import Annotated, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as sessions
from core.services.messaging import ApiProblem, ContractModel
from core.services.paperclip_admin_client import PaperclipApiError
from . import agents, deps

router = APIRouter()
CanonicalId = Annotated[str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")]


class IssueLink(ContractModel):
    kind: Literal["issue"]
    issue_id: CanonicalId


class WorkspaceLink(ContractModel):
    kind: Literal["workspace"]
    project_id: CanonicalId
    workspace_id: CanonicalId


class LinkUpdate(ContractModel):
    link: Annotated[IssueLink | WorkspaceLink, Field(discriminator="kind")] | None
    expected_link_revision: CanonicalId | None


def conflict():
    return ApiProblem("LOCAL_SESSION_CONFLICT", "Linked work changed. Refresh before trying again.")


def result(row, *, state=None, title=None):
    link = row.get("workLink")
    value = {"link": link, "revision": row.get("workLinkRevision"), "state": state or ("linked" if link else "empty")}
    if title is not None:
        value["title"] = title
    return value


async def resolve_work(request, response, auth, link):
    """No reconcile/provisioning. Only the current human's bound cookie may act."""
    try:
        ctx = await deps.resolve_teams_context(
            auth=auth,
            repo=agents._repo(),
            session_factory=agents.get_user_session_cookie_for,
            cookies=request.cookies,
            response=response,
        )
        # Set only after the actual company-scope guard has succeeded.
        request.state.teams_user_id = auth.user_id
        if str(uuid.UUID(ctx.company_id)) != ctx.company_id:
            raise ValueError("company identity")
        if link.get("companyId", ctx.company_id) != ctx.company_id:
            raise ValueError("scope")
        admin = agents._admin()
        if link["kind"] == "issue":
            work = await admin.get_issue(issue_id=link["issueId"], session_cookie=ctx.session_cookie)
            if work.get("id") != link["issueId"] or work.get("companyId") != ctx.company_id:
                raise ValueError("identity")
            title = work.get("title")
        else:
            work = await admin.get_project(project_id=link["projectId"], session_cookie=ctx.session_cookie)
            if work.get("id") != link["projectId"] or work.get("companyId") != ctx.company_id:
                raise ValueError("identity")
            workspace = next((w for w in work.get("workspaces", []) if w.get("id") == link["workspaceId"]), None)
            if workspace is None:
                raise ValueError("workspace")
            project_name, workspace_name = work.get("name"), workspace.get("name")
            if not isinstance(project_name, str) or not isinstance(workspace_name, str):
                raise ValueError("title")
            title = f"{project_name} · {workspace_name}"
        if not isinstance(title, str):
            raise ValueError("title")
        return {**link, "companyId": ctx.company_id}, title[:500]
    except PaperclipApiError as exc:
        if exc.status_code == 401:
            # Preserve established refresh/retry, including its failed-refresh path,
            # without allowing upstream private messages into that handler.
            raise PaperclipApiError("Linked work unavailable.", 401, {}) from None
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="Linked work unavailable.")


@router.get("/rooms/{room_id}/local-sessions/{session_id}/work-link")
async def get_link(
    room_id: str, session_id: str, request: Request, response: Response, auth: AuthContext = Depends(get_current_user)
):
    row, _ = await sessions.authorize_session(auth, room_id, session_id)
    if not row.get("workLink"):
        return result(row)
    title, state = None, "linked"
    try:
        _, title = await resolve_work(request, response, auth, row["workLink"])
    except HTTPException:
        state = "unavailable"
    current, _ = await sessions.authorize_session(auth, room_id, session_id)
    if current.get("workLinkRevision") != row.get("workLinkRevision"):
        raise conflict()
    return result(current, state=state, title=title)


@router.patch("/rooms/{room_id}/local-sessions/{session_id}/work-link")
async def update_link(
    room_id: str,
    session_id: str,
    body: LinkUpdate,
    request: Request,
    response: Response,
    auth: AuthContext = Depends(get_current_user),
):
    row, _ = await sessions.authorize_session(auth, room_id, session_id)
    if row.get("workLinkRevision") != body.expected_link_revision:
        raise conflict()
    link = None
    if body.link is not None:
        link, _ = await resolve_work(request, response, auth, body.link.model_dump(by_alias=True))
    for _ in range(6):
        row, enrollment = await sessions.authorize_session(auth, room_id, session_id)
        if row.get("workLinkRevision") != body.expected_link_revision:
            raise conflict()
        updated = {**row, "workLink": link, "workLinkRevision": str(uuid.uuid4()), "revision": str(uuid.uuid4())}
        try:
            await repo.put_session(
                updated,
                row,
                enrollment,
                extra=repo.room_conditions(
                    resolve_owner_id(auth),
                    row["humanUserId"],
                    {"roomId": room_id, "actorUserId": auth.user_id},
                ),
            )
            return result(updated)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
    raise conflict()
