"""Send agent-completion push notifications via the Expo Push API.

Best-effort: never raises into the caller (the gateway reader loop). Expo
fronts APNs/FCM, so we hold no APNs key — only an optional EXPO_ACCESS_TOKEN
bearer (injected as a container secret by infra) for send authentication.
"""

import logging
import os

import httpx

from core.repositories import user_repo

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_TIMEOUT_S = 10.0


async def send_push_to_user(user_id: str, title: str, body: str, data: dict | None = None) -> None:
    """Deliver a push to every registered device of *user_id*. No-op if none."""
    row = await user_repo.get(user_id)
    tokens_map = (row or {}).get("push_tokens") or {}
    entries = [(device_id, t["token"]) for device_id, t in tokens_map.items() if isinstance(t, dict) and t.get("token")]
    if not entries:
        return

    messages = [
        {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"} for _, token in entries
    ]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    access_token = os.environ.get("EXPO_ACCESS_TOKEN")
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(EXPO_PUSH_URL, json=messages, headers=headers)
            resp.raise_for_status()
            tickets = resp.json().get("data", [])
    except Exception:
        logger.exception("Expo push send failed for user %s", user_id)
        return

    # Prune tokens Expo reports as permanently invalid.
    for (device_id, _token), ticket in zip(entries, tickets):
        if isinstance(ticket, dict) and ticket.get("status") == "error":
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                try:
                    await user_repo.clear_push_token(user_id, device_id)
                except Exception:
                    logger.exception("Failed to prune dead token %s for user %s", device_id, user_id)
