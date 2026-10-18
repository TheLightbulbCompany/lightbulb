"""Thin HTTP client for Photon 2.0 (iMessage lines for text agents).

No business logic here: no DynamoDB, no owner lookups. Settings are read at call time.
"""

import logging
import uuid

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.photon.codes/v1"

# Tests inject an ``httpx.MockTransport`` here.
_transport: httpx.AsyncBaseTransport | None = None


class PhotonError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"photon {status}: {code}")
        self.status = status
        self.code = str(code)


def enabled() -> bool:
    return bool(settings.PHOTON_API_KEY and settings.PHOTON_PROJECT_ID and settings.PHOTON_WEBHOOK_SECRET)


async def _call(
    method: str,
    path: str,
    json: dict | None = None,
    *,
    idempotency_key: str | None = None,
    params: dict | None = None,
    ok_statuses: tuple[int, ...] = (),
) -> dict:
    headers = {"Authorization": f"Bearer {settings.PHOTON_API_KEY}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    url = f"/projects/{settings.PHOTON_PROJECT_ID}{path}"
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, transport=_transport) as client:
        resp = await client.request(method, url, json=json, headers=headers, params=params)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code in ok_statuses:
        return body
    if not resp.is_success:
        if resp.status_code == 404:
            raise PhotonError(404, "not_found")
        raise PhotonError(resp.status_code, body.get("code") or f"http_{resp.status_code}")
    return body


async def send_text(*, sender_id: str, space_id: str, text: str, idempotency_key: str) -> dict:
    # The bench's spaces are fictional ("dry:..."): never reach Photon.
    if space_id.startswith("dry:"):
        return {"id": f"dry-{uuid.uuid4().hex}", "status": "accepted", "dry_run": True}
    return await _call(
        "POST",
        "/messages",
        {"senderId": sender_id, "spaceId": space_id, "content": {"type": "text", "format": "plain", "text": text}},
        idempotency_key=idempotency_key,
    )


async def send_typing(*, sender_id: str, space_id: str, typing: bool = True) -> None:
    try:
        await _call("POST", f"/spaces/{space_id}/typing", {"senderId": sender_id, "typing": typing})
    except Exception:
        logger.warning("photon typing indicator failed", exc_info=True)


async def mark_read(message_id: str) -> None:
    try:
        await _call("POST", f"/messages/{message_id}/read", idempotency_key=f"read-{message_id}")
    except Exception:
        logger.warning("photon mark read failed", exc_info=True)


async def request_dedicated_line(*, idempotency_key: str) -> dict:
    return await _call("POST", "/platforms/imessage/dedicated", idempotency_key=idempotency_key)


async def get_operation(operation_id: str) -> dict:
    return await _call("GET", f"/platforms/operations/{operation_id}")


async def cancel_operation(operation_id: str) -> None:
    # 409 = the operation already finished.
    await _call("POST", f"/platforms/operations/{operation_id}/cancel", ok_statuses=(409,))


async def find_line_user(resource_id: str) -> dict | None:
    token = None
    while True:
        params = {"platform": "imessage", "pageSize": 100}
        if token:
            params["pageToken"] = token
        page = await _call("GET", "/users", params=params)
        for user in page.get("items", []):
            if (user.get("details") or {}).get("resourceId") == resource_id:
                return {"id": user["id"], "handle": user.get("handle")}
        token = page.get("nextPageToken")
        if not token:
            return None


async def release_line(resource_id: str) -> None:
    await _call("DELETE", f"/platforms/imessage/dedicated/{resource_id}", ok_statuses=(404,))
