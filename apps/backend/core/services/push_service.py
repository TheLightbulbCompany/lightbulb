"""Send agent-completion push notifications via the Expo Push API.

Best-effort: never raises into the caller (the gateway reader loop). Expo
fronts APNs/FCM, so we hold no APNs key. The Expo Push API accepts anonymous
sends by default; we do NOT use an access token. If we later want anti-spoofing,
enable "enhanced push security" on the EAS Dashboard and attach a Bearer here.
"""

import logging

import httpx

from core.repositories import user_repo

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_TIMEOUT_S = 10.0


async def send_push_to_user(user_id: str, title: str, body: str, data: dict | None = None) -> None:
    """Deliver a push to every registered device of *user_id*. No-op if none.

    Best-effort: the entire body is wrapped so nothing (a DynamoDB fault, an
    HTTP/transport error, a malformed Expo response) can escape into the caller.
    """
    try:
        row = await user_repo.get(user_id)
        tokens_map = (row or {}).get("push_tokens") or {}
        entries = [
            (device_id, t["token"]) for device_id, t in tokens_map.items() if isinstance(t, dict) and t.get("token")
        ]
        if not entries:
            return

        messages = [
            {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"} for _, token in entries
        ]
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(EXPO_PUSH_URL, json=messages, headers=headers)
            resp.raise_for_status()
            tickets = resp.json().get("data", [])

        # Expo returns one ticket per message. A shorter/absent `data` array
        # means the response was an error envelope (or a partial send), so the
        # zip below would silently skip pruning/error-logging for the unmatched
        # tail (e.g. a DeviceNotRegistered token would never be cleared and keep
        # receiving doomed pushes). Surface it.
        if len(tickets) != len(messages):
            logger.warning(
                "Expo returned %d tickets for %d messages (user %s) — likely an error "
                "envelope; some dead-token prunes or errors may be unhandled",
                len(tickets),
                len(messages),
                user_id,
            )

        for (device_id, _token), ticket in zip(entries, tickets):
            if not (isinstance(ticket, dict) and ticket.get("status") == "error"):
                continue
            error = (ticket.get("details") or {}).get("error")
            if error == "DeviceNotRegistered":
                # Prune tokens Expo reports as permanently invalid. Guard each
                # prune so one bad token can't abort the rest of the loop.
                try:
                    await user_repo.clear_push_token(user_id, device_id)
                except Exception:
                    logger.exception("Failed to prune dead token %s for user %s", device_id, user_id)
            else:
                # Surface systemic failures (MessageTooBig, InvalidCredentials, …).
                logger.warning("Expo push error for user %s device %s: %s", user_id, device_id, error)
    except Exception:
        logger.exception("Push send failed for user %s", user_id)
        return
