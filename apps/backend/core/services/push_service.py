"""Send agent-completion push notifications via the Expo Push API.

Best-effort: never raises into the caller (the gateway reader loop). Expo
fronts APNs/FCM, so we hold no APNs key. The Expo Push API accepts anonymous
sends by default; we do NOT use an access token. If we later want anti-spoofing,
enable "enhanced push security" on the EAS Dashboard and attach a Bearer here.
"""

import logging

import httpx

from core.observability.metrics import put_metric
from core.repositories import user_repo

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_TIMEOUT_S = 10.0

# Expo's per-ticket error strings → the bounded `error_kind` dimension. Expo
# documents a closed set, but it is their enum, not ours: anything unlisted
# collapses to "other" so a new upstream string can never explode dimension
# cardinality.
_EXPO_ERROR_KINDS = {
    "DeviceNotRegistered": "device_not_registered",
    "InvalidCredentials": "InvalidCredentials",
    "MessageTooBig": "MessageTooBig",
}


async def send_push_to_user(user_id: str, title: str, body: str, data: dict | None = None) -> None:
    """Deliver a push to every registered device of *user_id*. No-op if none.

    Best-effort: the entire body is wrapped so nothing (a DynamoDB fault, an
    HTTP/transport error, a malformed Expo response) can escape into the caller.

    Emits one ``push.send`` per attempted send — `outcome` is ``ok`` when every
    ticket came back clean, ``partial`` when some did, ``error`` when none did
    or the call never completed (including a repository fault before the POST).
    A user with no registered device is not a send and emits nothing.
    """
    # Defaults describe the worst case, so any escape hatch that is not an
    # explicit success still reports as one. `emit` goes false only on the
    # no-registered-device path, which is not a send.
    outcome, error_kind, emit = "error", "other", True
    try:
        row = await user_repo.get(user_id)
        tokens_map = (row or {}).get("push_tokens") or {}
        entries = [
            (device_id, t["token"]) for device_id, t in tokens_map.items() if isinstance(t, dict) and t.get("token")
        ]
        if not entries:
            emit = False
            return

        messages = [
            {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"} for _, token in entries
        ]
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(EXPO_PUSH_URL, json=messages, headers=headers)
            resp.raise_for_status()
            tickets = resp.json().get("data", [])
        outcome, error_kind = "ok", "none"

        # Expo returns one ticket per message. A shorter/absent `data` array
        # means the response was an error envelope (or a partial send), so the
        # zip below would silently skip pruning/error-logging for the unmatched
        # tail (e.g. a DeviceNotRegistered token would never be cleared and keep
        # receiving doomed pushes). Surface it.
        if len(tickets) != len(messages):
            outcome, error_kind = "partial", "ticket_mismatch"
            logger.warning(
                "Expo returned %d tickets for %d messages (user %s) — likely an error "
                "envelope; some dead-token prunes or errors may be unhandled",
                len(tickets),
                len(messages),
                user_id,
            )

        failed = 0
        for (device_id, _token), ticket in zip(entries, tickets):
            if not (isinstance(ticket, dict) and ticket.get("status") == "error"):
                continue
            failed += 1
            error = (ticket.get("details") or {}).get("error")
            # First real ticket error names the kind; a ticket_mismatch is only
            # a placeholder until one does.
            if error_kind in {"none", "ticket_mismatch"}:
                error_kind = _EXPO_ERROR_KINDS.get(error, "other")
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
        if failed:
            outcome = "error" if failed == len(messages) else "partial"
    except httpx.HTTPStatusError:
        outcome, error_kind = "error", "http"
        logger.exception("Push send failed for user %s", user_id)
    except httpx.RequestError:
        outcome, error_kind = "error", "transport"
        logger.exception("Push send failed for user %s", user_id)
    except Exception:
        outcome, error_kind = "error", "other"
        logger.exception("Push send failed for user %s", user_id)
    finally:
        if emit:
            put_metric("push.send", dimensions={"outcome": outcome, "error_kind": error_kind})
