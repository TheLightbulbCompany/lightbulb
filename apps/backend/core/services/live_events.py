"""IAM-only AppSync Events publisher for browser/mobile live projections."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Literal

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services.messaging import (
    ROOM_LIVE_EVENT_TYPES,
    SESSIONS_LIVE_EVENT_TYPES,
    USER_LIVE_EVENT_TYPES,
    LiveEvent,
    make_live_event,
)

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 230 * 1024
# pool=30: a burst waits its turn for a connection instead of failing with PoolTimeout.
_TIMEOUT = httpx.Timeout(5.0, connect=2.0, pool=30.0)
#: Publishes in flight at once, per process. Beyond it a publish waits; the pool is sized to it.
#: ponytail: one bound for every channel; per-workspace fairness if one workspace's burst ever
#: starves the others.
_MAX_INFLIGHT_PUBLISHES = 64
#: How long a publish waits for a slot. Live events are hints the client reconciles from HTTP,
#: so past this it is dropped (metric + warning) rather than stalling the request that fanned
#: it out — the desktop registration ack deadline is 10 s.
_INFLIGHT_WAIT_S = 5.0
_inflight: asyncio.Semaphore | None = None
#: One keep-alive client for every publisher without an injected one: Mac live frames publish
#: several times a second, and a fresh client per POST paid a TLS handshake each time.
_http_client: httpx.AsyncClient | None = None


class LivePublishError(RuntimeError):
    pass


class LivePublishQueueFull(LivePublishError):
    """No publish slot within _INFLIGHT_WAIT_S: terminal, metered once in _post, never retried."""


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


def room_message_created_event(item: dict[str, Any]) -> dict[str, Any]:
    """The ``room.message.created`` event the messaging projector builds for a stored MSG row,
    field for field and in its key order (``_canonical_message`` + ``_fan_out_message`` in
    apps/infra/lambda/messaging-projector/index.py). The projector is a separate bundle, so this
    is a copy: tests/unit/services/test_room_event_on_write_path.py pins the two byte-equal.
    No validation: the row is the one this process just wrote; the projector still validates."""
    fields = (
        "messageId", "roomId", "messageSeq", "roomRevision", "author", "text", "mentions",
        "replyToMessageId", "threadRootMessageId", "createdAt", "runId",
    )  # fmt: skip
    message: dict[str, Any] = {field: item[field] for field in fields}
    author = message["author"]
    message["author"] = {"kind": author["kind"], "id": author["id"], "displayName": author["displayName"]}
    message["mentions"] = [{"kind": m["kind"], "id": m["id"]} for m in message["mentions"]]
    message["addressedAgentIds"] = [
        t["agentId"] for t in item.get("dispatchTargets") or [] if t.get("eventKind") == "user_request"
    ]
    attachments = [
        {"id": a["id"], "fileName": a["fileName"], "contentType": a["contentType"], "size": int(a["size"])}
        for a in item.get("attachments") or []
    ]
    if attachments:
        message["attachments"] = attachments
    session_mentions = item.get("sessionMentions") or []
    if session_mentions:
        labels = item.get("sessionTargetLabels") or {}
        message["sessionMentions"] = list(session_mentions)
        message["sessionTargetLabels"] = {s: labels[s] for s in session_mentions if labels.get(s)}
    if item.get("progress") is True:
        message["progress"] = True
    # Super-agent message attrs (taskRefs, specialist, routedTo, forwardedFrom, kind), as the HTTP read carries them:
    # the live copy merges over it, so leaving them out dropped a Router reply's task cards until a reload.
    # Omitted when absent, so a legacy message's payload is unchanged.
    for attr in ("taskRefs", "specialist", "routedTo", "forwardedFrom", "kind"):
        if item.get(attr) is not None:
            message[attr] = item[attr]
    return {
        "schemaVersion": 1,
        "eventId": message["messageId"],
        "type": "room.message.created",
        "occurredAt": message["createdAt"],
        "roomId": message["roomId"],
        "messageSeq": message["messageSeq"],
        "roomRevision": message["roomRevision"],
        "payload": message,
    }


class LiveEventPublisher:
    def __init__(self, *, http_client: Any | None = None, credentials: Any | None = None) -> None:
        self._http = http_client
        self._credentials = credentials
        #: Guards only the first credential lookup, so a cold burst resolves the chain once.
        self._credentials_lock = asyncio.Lock()

    async def publish_room(self, room_id: str, event: dict[str, Any], *, frames: bool = False) -> bool:
        """``frames``: the room's view-frame sibling channel, ``/ws/<ws>/frames/<room>/<key>``
        (AppSync caps a path at 5 segments, so it cannot hang under the room channel)."""
        room_id = _uuid(room_id, "room id")
        validated = _validated_event(event, ROOM_LIVE_EVENT_TYPES)
        if str(validated.room_id) != room_id:
            raise LivePublishError("room event does not match its channel")
        channel = await self._room_channel(room_id, "frames" if frames else "rooms")
        if channel is None:
            return False
        await self._publish(channel, validated)
        return True

    async def publish_message_created(self, item: dict[str, Any]) -> bool:
        """A just-stored MSG row's ``room.message.created`` event, sent from the write path.

        The bytes are the projector's own (``room_message_created_event`` + its ``_event_json``),
        not a ``LiveEvent`` re-serialization, which would reformat ``occurredAt``: the projector
        sends the same eventId later and the client keeps whichever copy lands first."""
        room_id = _uuid(item["roomId"], "room id")
        room_key = (messaging_repo.room_pk(room_id), "META")
        # The slot first, the key and the row last: a member removed while this waits rotates the
        # key, and an edit or delete lands as a projector event this must not precede with the old
        # body. One strong read of both rows, applying the projector's own "latest" rule.
        async with self._slot({"source": "write_path"}):
            try:
                rows = await messaging_repo.batch_get([room_key, (item["pk"], item["sk"])])
                meta, latest = rows.get(room_key), rows.get((item["pk"], item["sk"]))
                if meta is None or latest is None or int(latest.get("messageRevision", 0)) > 0:
                    return False  # the projector's copy covers a gone room, an edit and a delete
                event = room_message_created_event(latest)
                channel = await self._room_channel(room_id, "rooms", meta)
            except Exception:
                put_metric(
                    "messaging.live.publish",
                    dimensions={"outcome": "error", "status_class": "resolve", "source": "write_path"},
                )
                raise
            await self._send(
                channel, json.dumps(event, separators=(",", ":"), ensure_ascii=False), source="write_path", held=True
            )
        return True

    async def _room_channel(self, room_id: str, family: str, meta: dict | None = None) -> str | None:
        meta = meta or await messaging_repo.get_room(room_id)
        if meta is None:
            return None
        key = _uuid(str(meta.get("deliveryKey", "")), "room delivery key")
        workspace = _uuid(
            await messaging_repo.workspace_id_for_meta(messaging_repo.room_pk(room_id), "META", meta),
            "room workspace id",
        )
        return f"/ws/{workspace}/{family}/{room_id}/{key}"

    async def publish_session_frames(self, workspace_id: str, event: dict[str, Any]) -> None:
        """One screen frame to its shared session's frames channel, ``/ws/<ws>/session-frames/<sid>``:
        one publish for every viewer. Who may subscribe is AppSync's onSubscribe decision."""
        validated = _validated_event(event, frozenset({"workspace.session.screen"}))
        workspace = _uuid(workspace_id, "workspace id")
        session_id = _uuid(str(validated.payload.get("sessionId")), "session id")
        await self._publish(f"/ws/{workspace}/session-frames/{session_id}", validated)

    async def publish_link_frames(self, public_id: str, event: dict[str, Any]) -> None:
        """The same frame to an "Anyone with the link" link's watchers, on ``/pub/<publicId>``. Who
        may subscribe is the `pub` namespace's Lambda authorizer's decision (a live lease)."""
        validated = _validated_event(event, frozenset({"workspace.session.screen"}))
        await self._publish(f"/pub/{_uuid(public_id, 'public id')}", validated)

    async def user_channel(self, owner_id: str, user_id: str) -> str | None:
        mapping = await messaging_repo.get_user_stream(owner_id, user_id)
        if mapping is None or mapping.get("active") is not True:
            return None
        key = _uuid(str(mapping.get("channelKey", "")), "user channel key")
        return await _stream_channel(owner_id, user_id, key, await messaging_repo.get_stream(key))

    async def user_channels(self, owner_id: str, user_ids: list[str]) -> dict[str, str]:
        """``user_channel`` for many users of one owner in two strong batch reads, not 2 per user.

        The same forward and reverse ``active`` checks, read fresh on every call: a removed
        user is absent from the answer on the very next call."""
        owner_pk = messaging_repo.owner_pk(owner_id)
        forward = await messaging_repo.batch_get([(owner_pk, messaging_repo.userstream_sk(u)) for u in user_ids])
        keys: dict[str, str] = {}
        for user in user_ids:
            mapping = forward.get((owner_pk, messaging_repo.userstream_sk(user)))
            if mapping is not None and mapping.get("active") is True:
                keys[user] = str(mapping.get("channelKey", ""))
        reverse = await messaging_repo.batch_get([(messaging_repo.stream_pk(key), "META") for key in keys.values()])
        channels: dict[str, str] = {}
        for user, key in keys.items():
            try:  # one malformed row loses that user only, as it did per publish_user
                key = _uuid(key, "user channel key")
                channel = await _stream_channel(
                    owner_id, user, key, reverse.get((messaging_repo.stream_pk(key), "META"))
                )
            except LivePublishError:
                continue
            if channel is not None:
                channels[user] = channel
        return channels

    async def publish_user(self, owner_id: str, user_id: str, event: dict[str, Any]) -> bool:
        validated = _validated_event(event, USER_LIVE_EVENT_TYPES)
        channel = await self.user_channel(owner_id, user_id)
        if channel is None:
            return False
        await self._publish(channel, validated)
        return True

    async def publish_users(
        self, owner_id: str, user_ids: list[str], event: dict[str, Any]
    ) -> list[bool | BaseException]:
        """``publish_user`` to each user, in ``user_ids`` order, with the event validated and
        encoded once and the channels resolved in one batch (``user_channels``). An entry is
        False for a user with no live channel, and the exception for a failed publish."""
        serialized = _serialized_event(_validated_event(event, USER_LIVE_EVENT_TYPES))
        channels = await self.user_channels(owner_id, user_ids)

        async def one(user: str) -> bool:
            channel = channels.get(user)
            if channel is None:
                return False
            await self._send(channel, serialized)
            return True

        return await asyncio.gather(*(one(user) for user in user_ids), return_exceptions=True)

    async def publish_sessions(self, owner_id: str, event: dict[str, Any]) -> None:
        """One publish on the workspace sessions channel, whatever the member count (it was one
        per member stream: 51 members x 30 registrations = 1,530 publishes, dev 2026-10-08)."""
        validated = _validated_event(event, SESSIONS_LIVE_EVENT_TYPES)
        workspace = _uuid(await messaging_repo.ensure_workspace_id(owner_id), "workspace id")
        await self._publish(f"/ws/{workspace}/sessions", validated)

    async def _publish(self, channel: str, event: LiveEvent) -> None:
        await self._send(channel, _serialized_event(event))

    async def _send(
        self, channel: str, serialized_event: str, *, source: str | None = None, held: bool = False
    ) -> None:
        # ``source`` splits the write path's publishes out of the series; absent, the dimensions
        # are exactly what they were before it existed.
        tag = {"source": source} if source else {}
        if len(serialized_event.encode("utf-8")) > MAX_EVENT_BYTES:
            raise LivePublishError("live event exceeds 230 KiB")
        endpoint = settings.APPSYNC_EVENTS_HTTP_ENDPOINT.strip()
        if not endpoint:
            put_metric("messaging.live.publish", dimensions={"outcome": "unconfigured", **tag})
            return
        body = json.dumps({"channel": channel, "events": [serialized_event]}, separators=(",", ":")).encode()

        last_error: Exception | None = None
        last_status_class = "transport"
        for attempt in range(2):
            try:
                request = await self._signed_request(endpoint, body)
            except Exception as exc:  # no credentials, failed refresh or signing: terminal, no retry
                put_metric("messaging.live.publish", dimensions={"outcome": "error", "status_class": "signing", **tag})
                raise LivePublishError("AppSync live publish could not be signed") from exc
            try:
                response = await (self._post_held if held else self._post)(endpoint, body, dict(request.headers), tag)
            except LivePublishQueueFull:
                raise  # saturation is terminal and already metered once: no retry, no transport sample
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
                    dimensions={
                        "outcome": "error",
                        "status_class": _publish_status_class(response.status_code),
                        **tag,
                    },
                )
                raise LivePublishError(f"AppSync HTTP {response.status_code}")
            try:
                payload = response.json()
            except Exception as exc:
                put_metric("messaging.live.publish", dimensions={"outcome": "error", "status_class": "invalid", **tag})
                raise LivePublishError("invalid AppSync response") from exc
            successful = payload.get("successful") if isinstance(payload, dict) else None
            failed = payload.get("failed") if isinstance(payload, dict) else None
            if not isinstance(successful, list) or not successful or failed != []:
                put_metric("messaging.live.publish", dimensions={"outcome": "error", "status_class": "rejected", **tag})
                raise LivePublishError("AppSync rejected live event")
            put_metric("messaging.live.publish", dimensions={"outcome": "ok", **tag})
            return
        put_metric(
            "messaging.live.publish",
            dimensions={"outcome": "error", "status_class": last_status_class, **tag},
        )
        # IDs and payloads are intentionally absent: message text, tokens, and
        # Authorization headers must never reach logs.
        logger.warning("AppSync live publish failed after retry (%s)", type(last_error).__name__)
        raise LivePublishError("AppSync live publish failed") from last_error

    async def _signed_request(self, endpoint: str, body: bytes) -> AWSRequest:
        """SigV4 on one small body is well under a millisecond of CPU, so it runs on the loop: a
        thread hop per publish queued behind every boto3 call in the default executor. Only the
        blocking parts hop: the first credential lookup, and a refresh (botocore's own lock
        serialises concurrent refreshes)."""
        if self._credentials is None:
            async with self._credentials_lock:
                if self._credentials is None:
                    self._credentials = await asyncio.to_thread(_task_credentials)
        credentials = self._credentials
        refresh_needed = getattr(credentials, "refresh_needed", None)
        if refresh_needed is not None and refresh_needed():
            return await asyncio.to_thread(_sign, credentials, endpoint, body)
        return _sign(credentials, endpoint, body)

    @contextlib.asynccontextmanager
    async def _slot(self, tag: dict[str, str] | None = None):
        global _inflight
        if _inflight is None:
            # Bound to the first event loop that posts, like the client below. Without it a
            # fan-out burst (51 members x 30 session registrations on 2026-10-08) exhausted the
            # connection pool and every publish past it was dropped with PoolTimeout.
            _inflight = asyncio.Semaphore(_MAX_INFLIGHT_PUBLISHES)
        try:
            await asyncio.wait_for(_inflight.acquire(), timeout=_INFLIGHT_WAIT_S)
        except TimeoutError as exc:
            put_metric(
                "messaging.live.publish", dimensions={"outcome": "error", "status_class": "queue_full", **(tag or {})}
            )
            raise LivePublishQueueFull("AppSync live publish queue full") from exc
        try:
            yield
        finally:
            _inflight.release()

    async def _post(self, endpoint: str, body: bytes, headers: dict[str, str], tag: dict[str, str] | None = None):
        async with self._slot(tag):
            return await self._post_held(endpoint, body, headers)

    async def _post_held(self, endpoint: str, body: bytes, headers: dict[str, str], tag: dict[str, str] | None = None):
        global _http_client
        if self._http is not None:
            return await self._http.post(endpoint, content=body, headers=headers)
        if _http_client is None:
            # Bound to the first event loop that posts: fine, the backend and the provisioner
            # Lambda each keep one loop for the process.
            _http_client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=_MAX_INFLIGHT_PUBLISHES, max_keepalive_connections=_MAX_INFLIGHT_PUBLISHES
                ),
            )
        return await _http_client.post(endpoint, content=body, headers=headers)


def _task_credentials() -> Any:
    credentials = get_session().get_credentials()
    if credentials is None:
        raise LivePublishError("AWS task credentials unavailable")
    return credentials


def _sign(credentials: Any, endpoint: str, body: bytes) -> AWSRequest:
    if hasattr(credentials, "get_frozen_credentials"):
        credentials = credentials.get_frozen_credentials()
    request = AWSRequest(method="POST", url=endpoint, data=body, headers={"content-type": "application/json"})
    SigV4Auth(credentials, "appsync", settings.APPSYNC_EVENTS_REGION or settings.AWS_REGION).add_auth(request)
    return request


async def _stream_channel(owner_id: str, user_id: str, key: str, reverse: dict | None) -> str | None:
    """The user channel for ``key`` when its reverse row is active and still names this user."""
    if (
        reverse is None
        or reverse.get("active") is not True
        or reverse.get("ownerId") != owner_id
        or reverse.get("userId") != user_id
    ):
        return None
    workspace = _uuid(
        await messaging_repo.workspace_id_for_meta(messaging_repo.stream_pk(key), "META", reverse),
        "stream workspace id",
    )
    return f"/ws/{workspace}/users/{key}"


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


async def publish_room_activity(
    room_id: str,
    actor: dict[str, str],
    state: Literal["typing", "working", "idle"],
    expires_at: datetime | None = None,
    *,
    skip_user_id: str | None = None,
) -> None:
    """One compact "this room is live" hint on every CURRENT human member's private
    user channel, so a conversation LIST can say "Ann is typing…" / "CEO is
    working…" for rooms it has not opened (typing and run frames ride the room
    channel, which a client subscribes to for the open room only).

    No content rides it. Membership is strong-read now (``list_members`` is
    ConsistentRead) so a removed member gets nothing; mute is client-side (push
    is the only server-side mute); each channel resolves in the member's HOME
    workspace, as the projector's ``_stream_channel`` does. Best-effort: a
    per-member failure is counted, never raised.

    ponytail: one AppSync POST per member per call (typing is throttled to
    1/s per typist per room); batch the channels into one request if a large
    room's fan-out ever shows in latency.
    """
    meta = await messaging_repo.get_room(room_id)
    if meta is None or meta.get("deletedAt"):
        return
    payload: dict[str, Any] = {"roomId": room_id, "actor": actor, "state": state}
    if expires_at is not None:
        payload["expiresAt"] = expires_at.isoformat()
    event = make_live_event("room.activity.changed", payload, room_id=room_id)
    publisher = get_live_event_publisher()
    humans = [
        member
        for member in await messaging_repo.list_members(room_id)
        if member["kind"] == "human" and member["id"] != skip_user_id
    ]
    results = await asyncio.gather(
        *(
            publisher.publish_user(messaging_repo.member_home(member, meta["ownerId"]), member["id"], event)
            for member in humans
        ),
        return_exceptions=True,
    )
    failed = sum(isinstance(result, Exception) for result in results)
    put_metric("messaging.live.activity", dimensions={"outcome": "error" if failed else "ok"})
