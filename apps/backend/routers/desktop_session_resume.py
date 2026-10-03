"""The owner's Mac says it would not start a resume (paired-device JWT, not Clerk).

Fails closed and says nothing: an authorization failure is a bare 403, and the body is never echoed.
"""

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from core.services import session_resume as service

router = APIRouter(prefix="/desktop/session-resume", tags=["desktop"])


@router.post("/{session_id}/failed", status_code=204)
async def failed(session_id: str, request: Request):
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    row = await service.authorize_device(token, session_id) if token else None
    if row is None:
        return Response(status_code=403)
    try:
        body = service.ResumeFailed.model_validate(await request.json())
    except (ValueError, ValidationError):
        return Response(status_code=400)
    await service.mark_failed(row, body)
    return Response(status_code=204)
