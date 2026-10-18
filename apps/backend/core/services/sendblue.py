"""Thin HTTP client for Sendblue (iMessage/SMS lines for text agents).

No business logic here: no DynamoDB, no owner lookups. Settings are read at call time.
"""

import logging
import uuid

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sendblue.com"

# Tests inject an ``httpx.MockTransport`` here.
_transport: httpx.AsyncBaseTransport | None = None


class SendblueError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"sendblue {status}: {code}")
        self.status = status
        self.code = code


def enabled() -> bool:
    return bool(settings.SENDBLUE_API_KEY_ID and settings.SENDBLUE_API_SECRET_KEY and settings.SENDBLUE_WEBHOOK_SECRET)


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = {
        "sb-api-key-id": settings.SENDBLUE_API_KEY_ID,
        "sb-api-secret-key": settings.SENDBLUE_API_SECRET_KEY,
    }
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, transport=_transport) as client:
        resp = await client.request(method, path, json=json, headers=headers)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    failed = not resp.is_success or body.get("status") in ("ERROR", "DECLINED")
    if failed:
        raise SendblueError(resp.status_code, body.get("error") or body.get("error_key") or f"http_{resp.status_code}")
    return body


async def send_text(*, from_number: str, to_number: str, content: str) -> dict:
    # Fictional +1555 numbers are the bench's customers: never reach Sendblue.
    if to_number.startswith("+1555"):
        return {"message_handle": f"dry-{uuid.uuid4().hex}", "status": "QUEUED", "dry_run": True}
    return await _call(
        "POST",
        "/api/send-message",
        {"number": to_number, "from_number": from_number, "content": content},
    )


async def send_typing(*, from_number: str, to_number: str) -> None:
    try:
        await _call("POST", "/api/send-typing-indicator", {"number": to_number, "from_number": from_number})
    except Exception:
        logger.warning("sendblue typing indicator failed", exc_info=True)


async def provision_line() -> str:
    preview = await _call("POST", "/accounts/lines/provision-preview", {})
    confirmed = await _call("POST", "/accounts/lines/provision-confirm", {"preview_token": preview["preview_token"]})
    return confirmed["phone_number"]


async def deprovision_line(phone_number: str) -> None:
    preview = await _call("POST", "/accounts/lines/deprovision-preview", {"phone_number": phone_number})
    await _call("POST", "/accounts/lines/deprovision-confirm", {"preview_token": preview["preview_token"]})
