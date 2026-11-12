"""Thin HTTP client for Photon 2.0 (iMessage lines for text agents).

No business logic here: no DynamoDB, no owner lookups. Settings are read at call time.
"""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.photon.codes/v1"
FIND_USER_MAX_PAGES = 10

# Tests inject an ``httpx.MockTransport`` here, then call ``_reset_client``.
_transport: httpx.AsyncBaseTransport | None = None
# One keep-alive client for every call: a fresh client per request paid a TLS handshake each time.
_client: httpx.AsyncClient | None = None


class PhotonError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"photon {status}: {code}")
        self.status = status
        self.code = str(code)


def enabled() -> bool:
    return bool(settings.PHOTON_API_KEY and settings.PHOTON_PROJECT_ID and settings.PHOTON_WEBHOOK_SECRET)


def _reset_client() -> None:
    global _client
    _client = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE_URL, timeout=20, transport=_transport)
    return _client


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
    resp = await _http().request(method, url, json=json, headers=headers, params=params)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code in ok_statuses:
        return body
    if not resp.is_success:
        status = resp.status_code
        raise PhotonError(status, body.get("code") or ("not_found" if status == 404 else f"http_{status}"))
    return body


async def send_text(*, sender_id: str, space_id: str, text: str, idempotency_key: str) -> dict:
    return await _call(
        "POST",
        "/messages",
        {"senderId": sender_id, "spaceId": space_id, "content": {"type": "text", "format": "plain", "text": text}},
        idempotency_key=idempotency_key,
    )


async def request_dedicated_line(*, idempotency_key: str) -> dict:
    return await _call("POST", "/platforms/imessage/dedicated", idempotency_key=idempotency_key)


async def get_operation(operation_id: str) -> dict:
    """The operation itself. The GET wraps it as ``{operation, resource?}`` (spec: GetOperationResponse),
    unlike the create, cancel and delete calls, which return the Operation at the top level."""
    body = await _call("GET", f"/platforms/operations/{operation_id}")
    op = body.get("operation") or body
    if not op.get("resourceId") and (body.get("resource") or {}).get("resourceId"):
        op = {**op, "resourceId": body["resource"]["resourceId"]}  # the wrapper's resource names it too
    return op


async def cancel_operation(operation_id: str) -> None:
    # 409 = the operation already finished.
    await _call("POST", f"/platforms/operations/{operation_id}/cancel", ok_statuses=(409,))


async def find_line_user(resource_id: str, *, page_token: str | None = None) -> tuple[dict | None, str | None]:
    """The line's own Photon user, and where to look next. ``/users`` filters by platform only, so this walks
    pages from ``page_token``: ``(user, None)`` when found; ``(None, token)`` when ``FIND_USER_MAX_PAGES`` pages
    ran out with more to read, so the caller's next look continues instead of starting over; ``(None, None)``
    when the list ended without it."""
    params: dict = {"platform": "imessage", "pageSize": 100, **({"pageToken": page_token} if page_token else {})}
    for _ in range(FIND_USER_MAX_PAGES):
        page = await _call("GET", "/users", params=params)
        for user in page.get("items", []):
            if (user.get("details") or {}).get("resourceId") == resource_id:
                return {"id": user["id"], "handle": user.get("handle")}, None
        if not page.get("nextPageToken"):
            return None, None
        params = {**params, "pageToken": page["nextPageToken"]}
    return None, params["pageToken"]


async def release_line(resource_id: str) -> dict | None:
    """None once Photon has released the line (200; 404 = already gone). A 202 is an Operation: one look,
    and one not yet ``succeeded`` is returned, so the caller keeps its row for a retry instead of forgetting
    a line that bills until it is released."""
    body = await _call("DELETE", f"/platforms/imessage/dedicated/{resource_id}", ok_statuses=(404,))
    if not body.get("operationId"):
        return None
    op = await get_operation(body["operationId"])
    return None if op.get("state") == "succeeded" else op
