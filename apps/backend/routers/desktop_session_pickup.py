"""The author's Mac uploads a pick-up snapshot (paired-device JWT, not Clerk).

Fails closed and says nothing: any authorization failure is a bare 403, and a request body is
never echoed or logged -- manifests name repositories and branches, next to transcript data.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from core.services import session_pickup as service
from core.services.session_pickup import DeviceRefused

router = APIRouter(prefix="/desktop/session-pickup", tags=["desktop"])


async def _session(request: Request, session_id: str) -> tuple[dict, dict]:
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    found = await service.authorize_device(token, session_id) if token else None
    if found is None:
        raise DeviceRefused(403)
    return found


async def _body(request: Request, model: type[BaseModel]):
    # Parsed here, after authorization, so FastAPI's 422 never echoes the rejected input.
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise DeviceRefused(400) from None


def _refused(exc: DeviceRefused) -> Response:
    if exc.body is not None:
        return JSONResponse(exc.body, status_code=exc.status)
    return Response(status_code=exc.status)


@router.post("/{session_id}/uploads")
async def uploads(session_id: str, request: Request):
    try:
        row, _ = await _session(request, session_id)
        return await service.presign_uploads(row, await _body(request, service.UploadsRequest))
    except DeviceRefused as exc:
        return _refused(exc)


@router.post("/{session_id}/commit", status_code=204)
async def commit(session_id: str, request: Request):
    try:
        row, enrollment = await _session(request, session_id)
        await service.commit(row, enrollment, await _body(request, service.CommitRequest))
    except DeviceRefused as exc:
        return _refused(exc)
    return Response(status_code=204)


@router.post("/{session_id}/unavailable", status_code=204)
async def unavailable(session_id: str, request: Request):
    try:
        row, enrollment = await _session(request, session_id)
        body = await _body(request, service.UnavailableRequest)
        await service.mark_unavailable(row, enrollment, body.reason)
    except DeviceRefused as exc:
        return _refused(exc)
    return Response(status_code=204)
