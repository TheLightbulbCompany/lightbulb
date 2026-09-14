"""FIFO SQS worker that drains canonical room pointers into OpenClaw."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import traceback
import uuid
from decimal import Decimal
from typing import Any

import boto3
import httpx
from boto3.dynamodb.types import TypeDeserializer

from core.config import settings
from core.containers import get_ecs_manager
from core.observability.metrics import gauge, put_metric
from core.repositories import messaging_repo
from core.services import runtime_rpc
from core.services.live_events import get_live_event_publisher
from core.services.messaging import (
    ApiProblem,
    canonical_message,
    human_emails,
    json_body_size,
    make_live_event,
    with_human_handles,
)

logger = logging.getLogger(__name__)

_INGRESS_MAX_BYTES = 1_048_576
_HISTORY_LIMIT = 20
_VISIBILITY_SECONDS = 60
_RETRY_VISIBILITY_SECONDS = 10
_OPERATION_LEASE_SECONDS = 300
_MAX_CONCURRENT_GROUPS = 8
_DESERIALIZER = TypeDeserializer()
# Every value request_wake_silent can return (container_wake.py). The
# allowlist is the point: an unmapped string becomes "unknown" rather than
# opening an unbounded dimension, and a new return value shows up as
# "unknown" instead of silently inflating a real bucket.
_WAKE_OUTCOMES = frozenset(
    {
        "dispatched",
        "blocked",
        "operator_stopped",
        "no_row",
        "error",
        "already_running",
        "in_flight",
        "converting",
    }
)


class WorkRetry(RuntimeError):
    pass


def deserialize_pointer(body: str) -> dict[str, Any]:
    # The Pipe forwards the raw DynamoDB stream record (no input template — an
    # object placeholder renders as Java-map text, not JSON); the outbox
    # pointer is the AttributeValue map at NewImage.payload.M.
    try:
        record = json.loads(body)
        encoded = record["dynamodb"]["NewImage"]["payload"]["M"]
        if not isinstance(encoded, dict):
            raise ValueError
        value = {key: _DESERIALIZER.deserialize(item) for key, item in encoded.items()}
    except Exception as exc:
        raise ValueError("invalid DynamoDB AttributeValue pointer") from exc
    kind = value.get("kind")
    if kind == "room_dispatch":
        if set(value) != {"kind", "roomId", "agentId", "throughSeq"}:
            raise ValueError("invalid room_dispatch pointer")
        try:
            value["roomId"] = str(uuid.UUID(value["roomId"]))
            value["agentId"] = str(uuid.UUID(value["agentId"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid room_dispatch identity") from exc
        through = value["throughSeq"]
        if (
            isinstance(through, bool)
            or not isinstance(through, (int, Decimal))
            or through != int(through)
            or through < 1
        ):
            raise ValueError("invalid throughSeq")
        value["throughSeq"] = int(through)
        return value
    if kind == "runtime_operation":
        if set(value) != {"kind", "ownerId", "operationId"}:
            raise ValueError("invalid runtime_operation pointer")
        if not isinstance(value["ownerId"], str) or not value["ownerId"]:
            raise ValueError("invalid ownerId")
        try:
            value["operationId"] = str(uuid.UUID(value["operationId"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid operationId") from exc
        return value
    raise ValueError("unknown work pointer kind")


def _ingress_status_class(status_code: int) -> str:
    """200/202/409 are the plugin's contractual answers (replayed/accepted/not admitted)."""
    if status_code in (200, 202, 409):
        return str(status_code)
    if status_code >= 500:
        return "5xx"
    return "4xx" if status_code >= 400 else "other"


def _emit_queue_wait(message: dict, kind: str) -> None:
    sent = (message.get("Attributes") or {}).get("SentTimestamp")
    try:
        waited_ms = time.time() * 1000 - int(sent)
    except (TypeError, ValueError):
        return
    put_metric(
        "messaging.work.queue_wait",
        value=max(0.0, waited_ms),
        unit="Milliseconds",
        dimensions={"kind": kind},
    )


def lightbulb_session_key(agent_id: str, room_id: str) -> str:
    return f"agent:{agent_id}:lightbulb:channel:{room_id}"


def _public_message(row: dict[str, Any]) -> dict[str, Any]:
    message = canonical_message(row, original_request=True).model_dump(
        mode="json",
        by_alias=True,
        exclude={"reactions", "reaction_revision", "message_revision", "edited_at", "deleted_at"},
    )
    if not message["attachments"]:
        del message["attachments"]
    return message


async def build_ingress_body(
    *,
    meta: dict[str, Any],
    target_agent_id: str,
    event_kind: str,
    message: dict[str, Any],
    members: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    history = await messaging_repo.list_messages_before(meta["roomId"], int(message["messageSeq"]), _HISTORY_LIMIT)
    body = {
        "version": 1,
        "eventId": message["messageId"],
        "targetAgentId": target_agent_id,
        "eventKind": event_kind,
        "room": {
            "id": meta["roomId"],
            "name": meta.get("name"),
            "kind": meta["kind"],
            "revision": int(meta["revision"]),
        },
        "sender": message["author"],
        "message": _public_message(message),
        "mentions": message.get("mentions") or [],
        "participants": [
            {
                "kind": member["kind"],
                "id": member["id"],
                "displayName": member["displayName"],
                **({"mentionHandle": member["mentionHandle"]} if member.get("mentionHandle") else {}),
                **({"listenMode": member["listenMode"]} if member["kind"] == "agent" else {}),
            }
            for member in members
        ],
        "recentHistory": [_public_message(row) for row in history],
    }
    if sessions is not None:
        # Only once every container runs a plugin that parses it (Task 15); an
        # older plugin rejects unknown top-level fields with 400.
        body["sessions"] = sessions
    while body["recentHistory"] and json_body_size(body) > _INGRESS_MAX_BYTES:
        body["recentHistory"].pop(0)
    if json_body_size(body) > _INGRESS_MAX_BYTES:
        raise ValueError("Lightbulb ingress exceeds 1 MiB")
    return body


class OpenClawWorkWorker:
    def __init__(self, *, sqs_client: Any | None = None, http_client: Any | None = None) -> None:
        self._sqs = sqs_client
        self._http = http_client
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_GROUPS)
        self._active: set[asyncio.Task] = set()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        queue_url = settings.OPENCLAW_WORK_QUEUE_URL.strip()
        if not queue_url:
            logger.info("OpenClaw work queue is unconfigured; worker disabled")
            return
        if self._sqs is None:
            self._sqs = boto3.client("sqs", region_name=settings.AWS_REGION)
        consecutive_failures = 0
        while not self._stop.is_set():
            # The one signal that separates "the queue is quiet" from "the
            # worker died": emitted every iteration, empty receives included.
            gauge("messaging.worker.alive", 1)
            try:
                response = await asyncio.to_thread(
                    self._sqs.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=5,
                    VisibilityTimeout=_VISIBILITY_SECONDS,
                    AttributeNames=["MessageGroupId", "SentTimestamp"],
                )
            except Exception:
                put_metric("messaging.worker.poll", dimensions={"outcome": "error"})
                logger.warning("OpenClaw FIFO poll failed", exc_info=True)
                # A failing receive returns immediately, so an unslept retry
                # spins a hot loop against throttled/unreachable SQS. Waiting on
                # the stop event rather than sleeping keeps the backoff inside
                # main.py's ~20s graceful drain — a bare sleep(30) would burn it.
                # ponytail: plain exponential backoff, no jitter — one worker
                # per environment, so there is no herd to spread.
                consecutive_failures = min(consecutive_failures + 1, 5)  # 2**5 already exceeds the cap
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), min(30, 2**consecutive_failures))
                continue
            consecutive_failures = 0
            messages = response.get("Messages") or []
            if not messages:
                continue
            groups: dict[str, list[dict]] = {}
            for message in messages:
                group = (message.get("Attributes") or {}).get("MessageGroupId")
                if not isinstance(group, str) or not group:
                    group = f"invalid:{message.get('MessageId', '')}"
                groups.setdefault(group, []).append(message)
            tasks = {asyncio.create_task(self._process_group(batch)) for batch in groups.values()}
            self._active.update(tasks)
            for task in tasks:
                task.add_done_callback(self._active.discard)
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)

    async def _process_group(self, messages: list[dict]) -> None:
        async with self._semaphore:
            for message in messages:
                if self._stop.is_set():
                    await self._return_visibility(message)
                    continue
                await self._process_message(message)

    async def _process_message(self, message: dict) -> None:
        completed = False
        kind = "unknown"
        try:
            try:
                pointer = deserialize_pointer(message.get("Body", ""))
                kind = pointer["kind"]
            finally:
                # Emitted for every received message, poisoned pointers
                # included — those are exactly the ones that sat longest.
                _emit_queue_wait(message, kind)
            if pointer["kind"] == "room_dispatch":
                await self._room_dispatch(pointer)
            else:
                await self._runtime_operation(pointer)
            await asyncio.to_thread(
                self._sqs.delete_message,
                QueueUrl=settings.OPENCLAW_WORK_QUEUE_URL,
                ReceiptHandle=message["ReceiptHandle"],
            )
            completed = True
            put_metric("messaging.worker.message", dimensions={"kind": pointer["kind"], "outcome": "ok"})
        except asyncio.CancelledError:
            raise
        except WorkRetry as exc:
            put_metric(
                "messaging.worker.message",
                dimensions={"kind": kind, "outcome": "retry", "error": type(exc).__name__},
            )
            # Never log the SQS body or canonical message text.
            logger.warning("OpenClaw work item will retry (%s)", type(exc).__name__)
            # WorkRetry means "try again soon" (waking container, lease race,
            # ...) — release the message early instead of holding the full
            # 300s visibility, or the rest of the FIFO group queues behind it.
            await self._return_visibility(message, timeout=_RETRY_VISIBILITY_SECONDS)
        except Exception as exc:
            put_metric(
                "messaging.worker.message",
                dimensions={"kind": kind, "outcome": "retry", "error": type(exc).__name__},
            )
            # Never log the SQS body or canonical message text: exception
            # messages (pydantic's especially) echo input values, so log only
            # the type chain and the frames — file/line/function, no locals.
            cause = exc.__cause__ or exc.__context__
            logger.warning(
                "OpenClaw work item will retry (%s%s)\n%s",
                type(exc).__name__,
                f" from {type(cause).__name__}" if cause is not None else "",
                "".join(traceback.format_tb(exc.__traceback__)),
            )
        finally:
            if not completed and self._stop.is_set():
                await self._return_visibility(message)

    async def _return_visibility(self, message: dict, timeout: int = 0) -> None:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    self._sqs.change_message_visibility,
                    QueueUrl=settings.OPENCLAW_WORK_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=timeout,
                )
            )
        except Exception:
            logger.warning("Could not change FIFO visibility", exc_info=True)

    async def _room_dispatch(self, pointer: dict[str, Any]) -> None:
        room_id = pointer["roomId"]
        agent_id = pointer["agentId"]
        through = pointer["throughSeq"]
        cursor = await messaging_repo.get_agent_delivery(room_id, agent_id)
        if cursor is None:
            return
        expected = int(cursor["lastSeq"])
        if through <= expected:
            return
        meta = await messaging_repo.get_room(room_id)
        if meta is None or through > int(meta["lastSeq"]):
            raise WorkRetry("room pointer is ahead of canonical storage")
        rows = await messaging_repo.list_messages_between(room_id, expected + 1, through)
        if not rows or int(rows[-1]["messageSeq"]) != through:
            raise WorkRetry("canonical message range is incomplete")
        for message in rows:
            seq = int(message["messageSeq"])
            if seq <= expected:
                continue
            member = await messaging_repo.get_member(room_id, "agent", agent_id)
            if member is None:
                if not await messaging_repo.advance_agent_delivery(room_id, agent_id, expected, through):
                    latest = await messaging_repo.get_agent_delivery(room_id, agent_id)
                    if latest is not None:
                        raise WorkRetry("agent cursor moved during removal skip")
                return
            target = next(
                (item for item in message.get("dispatchTargets") or [] if item.get("agentId") == agent_id),
                None,
            )
            if target is not None:
                await self._dispatch_message(meta, member, message, target["eventKind"])
            if not await messaging_repo.advance_agent_delivery(room_id, agent_id, expected, seq):
                latest = await messaging_repo.get_agent_delivery(room_id, agent_id)
                if latest is None:
                    return
                actual = int(latest["lastSeq"])
                if actual >= seq:
                    expected = actual
                    continue
                raise WorkRetry("agent cursor compare-and-set failed")
            expected = seq

    async def _dispatch_message(
        self,
        meta: dict[str, Any],
        member: dict[str, Any],
        message: dict[str, Any],
        event_kind: str,
    ) -> None:
        owner_id = meta["ownerId"]
        manager = get_ecs_manager()
        container, ip = await manager.resolve_running_container(owner_id)
        if not container or not ip:
            from core.services.container_wake import request_wake_silent

            wake = await request_wake_silent(owner_id)
            put_metric(
                "messaging.wake.outcome",
                dimensions={"outcome": wake if wake in _WAKE_OUTCOMES else "unknown"},
            )
            raise WorkRetry("runtime is waking")
        token = container.get("gateway_token")
        if not isinstance(token, str) or not token:
            raise WorkRetry("gateway token unavailable")
        room_members = await messaging_repo.list_members(meta["roomId"])
        members = with_human_handles(room_members, await human_emails(room_members))
        body = await build_ingress_body(
            meta=meta, target_agent_id=member["id"], event_kind=event_kind, message=message, members=members
        )
        # The earlier read chose the work; this one is deliberately adjacent to
        # the side effect so queued work cannot outlive membership revocation.
        if await messaging_repo.get_member(meta["roomId"], "agent", member["id"]) is None:
            return
        run_id = messaging_repo.run_id_for(member["id"], message["messageId"])
        workspace_id = await messaging_repo.workspace_id_for_meta(messaging_repo.room_pk(meta["roomId"]), "META", meta)
        body["run"] = {
            "runId": run_id,
            "workspaceId": workspace_id,
            "frameChannel": f"/ws/{workspace_id}/rooms/{meta['roomId']}/{meta['deliveryKey']}",
            "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        }
        created = await messaging_repo.create_run(
            owner_id,
            run_id,
            room_id=meta["roomId"],
            agent_id=member["id"],
            workspace_id=workspace_id,
            trigger_message_id=message["messageId"],
            event_kind=event_kind,
        )
        put_metric(
            "messaging.run.dispatched",
            dimensions={"outcome": "new" if created else "redelivered", "event_kind": event_kind},
        )
        from core.services import local_session_grants
        from core.services.messaging import ApiProblem

        invocation = None
        try:
            invocation = local_session_grants.mint(owner_id, meta["roomId"], member["id"], message["messageId"])
        except (RuntimeError, ApiProblem):
            logger.warning("Local session invocation unavailable")
        url = f"http://{ip}:18789/plugins/lightbulb-channel/inbound/{member['id']}"
        started = time.perf_counter()
        try:
            response = await self._post(url, body, token, invocation)
            put_metric(
                "messaging.ingress.post",
                dimensions={"status_class": _ingress_status_class(response.status_code), "event_kind": event_kind},
            )
            put_metric(
                "messaging.ingress.latency",
                value=(time.perf_counter() - started) * 1000,
                unit="Milliseconds",
                dimensions={"event_kind": event_kind},
            )
            if response.status_code == 409:
                # Permanent synchronous refusal (loop guard, non-member): the ledger
                # closes here because the plugin will never call back for it.
                await messaging_repo.end_run(owner_id, run_id, outcome="not_admitted")
                put_metric("messaging.ambient_gate", dimensions={"outcome": "not_admitted", "event_kind": event_kind})
                return
            if response.status_code not in (200, 202):
                raise WorkRetry(f"plugin refused ingress ({response.status_code})")
            try:
                status = response.json().get("status")
            except (AttributeError, TypeError, ValueError):
                status = "unknown"
            if status not in {"accepted", "replayed"}:
                status = "unknown"
            put_metric("messaging.ingress.ack", dimensions={"status": status, "event_kind": event_kind})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            put_metric(
                "messaging.ingress.post",
                dimensions={
                    "status_class": "timeout" if isinstance(exc, httpx.TimeoutException) else "transport",
                    "event_kind": event_kind,
                },
            )
            # Effect-once on the plugin makes a retry safe; the ledger row is reused.
            raise WorkRetry(f"plugin ingress {type(exc).__name__}") from exc

    async def _post(self, url: str, body: dict[str, Any], token: str, invocation: str | None = None):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if invocation is not None:
            headers["X-Lightbulb-Local-Session-Token"] = invocation
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(content) > _INGRESS_MAX_BYTES:
            raise ValueError("Lightbulb ingress exceeds 1 MiB")
        if self._http is not None:
            return await self._http.post(url, content=content, headers=headers)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            return await client.post(url, content=content, headers=headers)

    async def _runtime_operation(self, pointer: dict[str, Any]) -> None:
        owner_id = pointer["ownerId"]
        operation_id = pointer["operationId"]
        row = await messaging_repo.get_operation(owner_id, operation_id)
        if row is None or row.get("state") in {"succeeded", "failed"}:
            return
        lease = uuid.uuid4().hex
        claimed = await messaging_repo.claim_operation(
            owner_id,
            operation_id,
            lease_token=lease,
            lease_until=int(time.time()) + _OPERATION_LEASE_SECONDS,
        )
        if claimed is None:
            latest = await messaging_repo.get_operation(owner_id, operation_id)
            if latest is None or latest.get("state") in {"succeeded", "failed"}:
                return
            raise WorkRetry("runtime operation lease is still active")
        try:
            result = await runtime_rpc.execute_sync(
                owner_id=owner_id,
                user_id=claimed["requesterUserId"],
                request_id=operation_id,
                method=claimed["method"],
                params=claimed["params"],
            )
        except ApiProblem as exc:
            if exc.error.code == "RUNTIME_UNAVAILABLE" and exc.error.retryable:
                await messaging_repo.release_operation(owner_id, operation_id, lease_token=lease)
                raise WorkRetry("runtime operation is waiting for the runtime") from exc
            completed = await messaging_repo.complete_operation(
                owner_id,
                operation_id,
                lease_token=lease,
                state="failed",
                result=None,
                error={
                    "code": exc.error.code,
                    "message": exc.error.message,
                    "retryable": exc.error.retryable,
                },
            )
        except Exception as exc:
            completed = await messaging_repo.complete_operation(
                owner_id,
                operation_id,
                lease_token=lease,
                state="failed",
                result=None,
                error={"code": "INTERNAL_ERROR", "message": "Runtime operation failed.", "retryable": False},
            )
            logger.warning("runtime operation failed (%s)", type(exc).__name__)
        else:
            completed = await messaging_repo.complete_operation(
                owner_id,
                operation_id,
                lease_token=lease,
                state="succeeded",
                result=result,
                error=None,
            )
        if completed is None:
            raise WorkRetry("runtime operation lease changed before completion")
        event = make_live_event(
            "runtime.operation.changed",
            {
                "operationId": operation_id,
                "state": completed["state"],
                "updatedAt": completed["updatedAt"],
                "result": completed.get("result"),
                "error": completed.get("error"),
            },
            event_id=operation_id,
        )
        await get_live_event_publisher().publish_user(owner_id, completed["requesterUserId"], event)


_worker: OpenClawWorkWorker | None = None


def get_openclaw_work_worker() -> OpenClawWorkWorker:
    global _worker
    if _worker is None or _worker._stop.is_set():
        _worker = OpenClawWorkWorker()
    return _worker
