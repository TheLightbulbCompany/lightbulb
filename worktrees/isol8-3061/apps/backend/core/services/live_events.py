"""IAM-only AppSync Events publisher for browser/mobile live projections."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services.messaging import (
    ROOM_LIVE_EVENT_TYPES,
    USER_LIVE_EVENT_TYPES,
    LiveEvent,
)

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 230 * 1024
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class LivePublishError(RuntimeError):
    pass


def _publish_status_class(status_code: int) -> str:
    """Failure class for a non-2xx AppSync answer. 429 is split out because it
    is throttling (shed load) rather than an outage."""
    if status_code == 429:
        return "429"
    return "5xx" if status_code >= 500 else "4xx"


def _uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LivePublishError(f"invalid {label}") from exc
    if str(parsed) != str(value).lower():
        raise LivePublishError(f"invalid {label}")
    return str(parsed)


def _validated_event(
    raw_event: dict[str, Any],
    allowed_types: frozenset[str],
) -> LiveEvent:
    try:
        event = LiveEvent.model_validate(raw_event)
    except Exception as exc:
        raise LivePublishError("invalid live event") from exc
    if event.type not in allowed_types:
        raise LivePublishError("live event is not allowed on this channel family")
    return event


def _serialized_event(event: LiveEvent) -> str:
    body = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    if event.type == "room.run.frame":
        payload = body.get("payload")
        frame = payload.get("frame") if isinstance(payload, dict) else None
        if not isinstance(frame, dict):
            raise LivePublishError("invalid room run frame")
        for source, target in (
            ("agent_id", "agentId"),
            ("session_id", "sessionId"),
        ):
            if source not in frame:
                continue
            if target in frame and frame[target] != frame[source]:
                raise LivePublishError("conflicting room run frame fields")
            frame[target] = frame.pop(source)
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class LiveEventPublisher:
    def __init__(self, *, http_client: Any | None = None, credentials: Any | None = None) -> None:
        self._http = http_client
        self._credentials = credentials
        self._signing_lock = asyncio.Lock()

    async def publish_room(self, room_id: str, event: dict[str, Any]) -> bool:
        room_id = _uuid(room_id, "room id")
        validated = _validated_event(event, ROOM_LIVE_EVENT_TYPES)
        if str(validated.room_id) != room_id:
            raise LivePublishError("room event does not match its channel")
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            return False
        key = _uuid(str(meta.get("deliveryKey", "")), "room delivery key")
        workspace = _uuid(
            await messaging_repo.workspace_id_for_meta(messaging_repo.room_pk(room_id), "META", meta),
            "room workspace id",
        )
        await self._publish(f"/ws/{workspace}/rooms/{room_id}/{key}", validated)
        return True

    async def publish_user(self, owner_id: str, user_id: str, event: dict[str, Any]) -> bool:
        validated = _validated_event(event, USER_LIVE_EVENT_TYPES)
        mapping = await messaging_repo.get_user_stream(owner_id, user_id)
        if mapping is None or mapping.get("active") is not True:
            return False
        key = _uuid(str(mapping.get("channelKey", "")), "user channel key")
        reverse = await messaging_repo.get_stream(key)
        if (
            reverse is None
            or reverse.get("active") is not True
            or reverse.get("ownerId") != owner_id
            or reverse.get("userId") != user_id
        ):
            return False
        workspace = _uuid(
            await messaging_repo.workspace_id_for_meta(messaging_repo.stream_pk(key), "META", reverse),
            "stream workspace id",
        )
        await self._publish(f"/ws/{workspace}/users/{key}", validated)
        return True

    async def _publish(self, channel: str, event: LiveEvent) -> None:
        serialized_event = _serialized_event(event)
        if len(serialized_event.encode("utf-8")) > MAX_EVENT_BYTES:
            raise LivePublishError("live event exceeds 230 KiB")
        endpoint = settings.APPSYNC_EVENTS_HTTP_ENDPOINT.strip()
        if not endpoint:
            put_metric("messaging.live.publish", dimensions={"outcome": "unconfigured"})
            return
        body = json.dumps({"channel": channel, "events": [serialized_event]}, separators=(",", ":")).encode()

        last_error: Exception | None = None
        last_status_class = "transport"
        for attempt in range(2):
            async with self._signing_lock:
                request = await asyncio.to_thread(self._signed_request, endpoint, body)
            try:
                response = await self._post(endpoint, body, dict(request.headers))
            except Exception as exc:
                last_error = exc
                last_status_class = "transport"
                if attempt == 0:
                    continue
                break
            if response.status_code == 429 or response.status_code >= 500:
                last_error = LivePublishError(f"AppSync HTTP {response.status_code}")
                last_status_class = _publish_status_class(response.status_code)
                if attempt == 0:
                    continue
                break
            if not 200 <= response.status_code < 300:
                # Not retried, so this is the terminal outcome — it was the one
                # failure path that published nothing at all.
                put_metric(
                    "messaging.live.publish",
                    dimensions={"outcome": "error", "status_class": _publish_status_class(response.status_code)},
                )
                raise LivePublishError(f"AppSync HTTP {response.status_code}")
            try:
                payload = response.json()
            except Exception as exc:
                raise LivePublishError("invalid AppSync response") from exc
            successful = payload.get("successful") if isinstance(payload, dict) else None
            failed = payload.get("failed") if isinstance(payload, dict) else None
            if not isinstance(successful, list) or not successful or failed != []:
                raise LivePublishError("AppSync rejected live event")
            put_metric("messaging.live.publish", dimensions={"outcome": "ok"})
            return
        put_metric(
            "messaging.live.publish",
            dimensions={"outcome": "error", "status_class": last_status_class},
        )
        # IDs and payloads are intentionally absent: message text, tokens, and
        # Authorization headers must never reach logs.
        logger.warning("AppSync live publish failed after retry (%s)", type(last_error).__name__)
        raise LivePublishError("AppSync live publish failed") from last_error

    def _signed_request(self, endpoint: str, body: bytes) -> AWSRequest:
        credentials = self._credentials
        if credentials is None:
            credentials = get_session().get_credentials()
            if credentials is None:
                raise LivePublishError("AWS task credentials unavailable")
            self._credentials = credentials
        if hasattr(credentials, "get_frozen_credentials"):
            credentials = credentials.get_frozen_credentials()
        request = AWSRequest(
            method="POST",
            url=endpoint,
            data=body,
            headers={"content-type": "application/json"},
        )
        SigV4Auth(credentials, "appsync", settings.APPSYNC_EVENTS_REGION or settings.AWS_REGION).add_auth(request)
        return request

    async def _post(self, endpoint: str, body: bytes, headers: dict[str, str]):
        if self._http is not None:
            return await self._http.post(endpoint, content=body, headers=headers)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.post(endpoint, content=body, headers=headers)


_publisher: LiveEventPublisher | None = None


def get_live_event_publisher() -> LiveEventPublisher:
    global _publisher
    if _publisher is None:
        _publisher = LiveEventPublisher()
    return _publisher


async def publish_owner_users(owner_id: str, event: dict[str, Any]) -> None:
    """Publish one private invalidation to every active user stream for an owner."""
    publisher = get_live_event_publisher()
    rows = await messaging_repo.list_active_user_streams(owner_id)
    await asyncio.gather(
        *(publisher.publish_user(owner_id, row["userId"], event) for row in rows),
        return_exceptions=True,
    )
