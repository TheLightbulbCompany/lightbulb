"""Processes canonical room pointers from the FIFO work queue."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import traceback
import uuid
from decimal import Decimal
from typing import Any

import httpx
from boto3.dynamodb.types import TypeDeserializer

from core.config import settings
from core.observability.metrics import put_metric
from core.services import runtime_transport
from core.repositories import container_repo, messaging_repo
from core.services import runtime_rpc
from core.services.live_events import get_live_event_publisher
from core.services.messaging import (
    INGRESS_REFUSED,
    STEER_LOST,
    ApiProblem,
    canonical_message,
    human_emails,
    json_body_size,
    is_feed_room,
    make_live_event,
    room_agent,
    with_human_handles,
)

logger = logging.getLogger(__name__)

_INGRESS_MAX_BYTES = 1_048_576
#: recentHistory's size. The bridge runs a room turn on the agent's own Hermes transcript and
#: records the part of this window that transcript does not hold yet (all of it on a room's first
#: turn), so this caps how many messages one turn can catch up on.
_HISTORY_LIMIT = 50
# Routing v2 §4.1: what a steer into an AMBIENT run adds after "{name}: {text}".
AMBIENT_STEER_SUFFIX = (
    " — said since you were asked. If this already answers or settles what you were going to say,"
    " reply exactly [SILENT]. If it adds to it, answer only what is still open. If it is unrelated, carry on."
)
_RETRY_VISIBILITY_SECONDS = 10
#: F4 (predeploy review, M6-R37): hoisted to runtime_transport (the shared home
#: container_rpc.call also reads) so a fleet-wide budget change has one source. Kept as a
#: module attribute here so agent_mail_delivery's existing import keeps working unchanged.
_AGENTCORE_INGRESS_TIMEOUT_S = runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S
_OPERATION_LEASE_SECONDS = 300
_DESERIALIZER = TypeDeserializer()


async def _fallback_ceo_id(owner_id: str) -> str | None:
    """The #general CEO's stable id (same resolution as messaging.ensure_default_room),
    for the deterministic single-responder Jev fallback below."""
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.user_provision import ceo_agent_id

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    return ceo_agent_id(company) if company else None


def _last_speaker(candidates: list[str], message: dict, history: list[dict]) -> str | None:
    """The candidate already in this conversation: whoever spoke last in the trigger's thread,
    else in the room. ``history`` ends before the trigger, so every consumer reads the same one."""
    root = message.get("threadRootMessageId")
    thread = [row for row in history if root and root in (row.get("messageId"), row.get("threadRootMessageId"))]
    for rows in (thread, history):
        for row in reversed(rows):
            author = row.get("author") or {}
            if author.get("kind") == "agent" and author.get("id") in candidates:
                return author["id"]
    return None


async def _fallback_single(owner_id: str, candidates: list[str], message: dict, history: list[dict]) -> str:
    """Jev down/undecided with >=2 room_event candidates (spec #2942): pick one deterministically
    instead of dispatching all of them -- whoever the person was already talking with, else the
    CEO if it is a candidate, else the lowest id (prod 2026-09-27: a whole day of Jev 403s sent
    every plain message to the same agent, whoever the conversation was with)."""
    spoke = _last_speaker(candidates, message, history)
    if spoke:
        return spoke
    ceo_id = await _fallback_ceo_id(owner_id)
    return ceo_id if ceo_id in candidates else min(candidates)


class IngressTooLarge(ValueError):
    """The final serialized ingress exceeds the bridge's 1 MiB limit."""


class WorkRetry(RuntimeError):
    def __init__(self, message: str = "", *, visibility: int | None = None) -> None:
        super().__init__(message)
        self.visibility = visibility


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
    if kind == "room_follow_up":
        if set(value) != {"kind", "roomId", "agentId", "followUpId"}:
            raise ValueError("invalid room_follow_up pointer")
        try:
            for key in ("roomId", "agentId", "followUpId"):
                value[key] = str(uuid.UUID(value[key]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid room_follow_up identity") from exc
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
    if kind == "network_wake":
        if set(value) != {"kind", "ownerId"}:
            raise ValueError("invalid network_wake pointer")
        if not isinstance(value["ownerId"], str) or not value["ownerId"]:
            raise ValueError("invalid ownerId")
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


def _public_message(row: dict[str, Any]) -> dict[str, Any]:
    # The runtime ingress contract is separate from (and pinned tighter than) the
    # client-facing HTTP/live-event message. addressedAgentIds (and inReplyTo, on agent
    # replies) ride only when set: they are the `to` edges Jev reads off this same history
    # (routing v2 §5); the bridge ignores them.
    message = canonical_message(row, original_request=True).model_dump(
        mode="json",
        by_alias=True,
        exclude={
            "reactions",
            "reaction_revision",
            "message_revision",
            "edited_at",
            "deleted_at",
        },
    )
    for key in ("attachments", "addressedAgentIds"):
        if not message[key]:
            del message[key]
    return message


async def room_history(meta: dict[str, Any], before_seq: int, *, root: str | None = None) -> list[dict[str, Any]]:
    """The room's last messages before ``before_seq`` (public shape), plus the whole thread
    ``root`` when it began before that window. Room turns and voice runs share it."""
    history = await messaging_repo.list_messages_before(meta["roomId"], before_seq, _HISTORY_LIMIT)
    if root and not any(row.get("messageId") == root for row in history):
        # The whole thread, even when it began before the window. A root inside the contiguous
        # window already carries every later reply, so only then is the read skipped.
        merged = {
            int(row["messageSeq"]): row for row in await messaging_repo.list_thread(meta["roomId"], root, before_seq)
        }
        merged.update((int(row["messageSeq"]), row) for row in history)
        history = [merged[seq] for seq in sorted(merged)]
    return [_public_message(row) for row in history]


def ingress_participants(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": member["kind"],
            "id": member["id"],
            "displayName": member["displayName"],
            **({"mentionHandle": member["mentionHandle"]} if member.get("mentionHandle") else {}),
            **({"listenMode": member["listenMode"]} if member["kind"] == "agent" else {}),
        }
        for member in members
    ]


async def build_ingress_body(
    *,
    meta: dict[str, Any],
    target_agent_id: str,
    event_kind: str,
    message: dict[str, Any],
    members: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
    history_through_seq: int | None = None,
    allow_oversize: bool = False,
) -> dict[str, Any]:
    # A follow-up re-runs an old trigger: its history runs through the room's latest message, so the
    # turn sees the request itself and the agent's own "I'll report back" reply after it.
    before = int(message["messageSeq"]) if history_through_seq is None else history_through_seq + 1
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
        "participants": ingress_participants(members),
        "recentHistory": await room_history(meta, before, root=message.get("threadRootMessageId")),
    }
    if sessions:
        # Only once every container runs a plugin that parses it; an older
        # plugin rejects unknown top-level fields with 400.
        body["sessions"] = sessions
    if message.get("sessionMentions"):
        # A session chip is a reference (owner ruling 2026-10-02): the agent reads the session with
        # lightbulb_local_sessions, so it needs the exact id even for an offline or ended session
        # the bounded live summary leaves out. Resolved under the writer's own visibility rule.
        from core.services import local_sessions

        referenced = await local_sessions.referenced_sessions(
            meta.get("ownerId") or "", members, message["sessionMentions"]
        )
        if referenced:
            body["referencedSessions"] = referenced
    while body["recentHistory"] and json_body_size(body) > _INGRESS_MAX_BYTES:
        body["recentHistory"].pop(0)
    if json_body_size(body) > _INGRESS_MAX_BYTES:
        if not allow_oversize:
            raise ValueError("Lightbulb ingress exceeds 1 MiB")
        # Internal signal only: the caller removes it before routing or invoke.
        body["_oversized"] = True
    return body


async def publish_delivery_changed(room_id: str, agent_id: str, seq: int) -> None:
    from core.services.messaging import _publish_room_event, make_live_event

    await _publish_room_event(
        room_id,
        make_live_event(
            "room.delivery.changed",
            {"roomId": room_id, "agentId": agent_id, "lastDeliveredSeq": seq},
            room_id=room_id,
        ),
    )


async def steer_grant(owner_id: str, run: dict[str, Any], steerer_id: str | None) -> str | None:
    """The grant a steer of `run` must carry: None when the steerer is the person the run acts for
    (its trigger's human author, as its grant was minted), else a grant naming nobody, which the
    bridge swaps in before the steer's text reaches the agent, so the steerer never borrows that
    person's Claude Code sessions or the rooms they can read. Only ever lowers. Raises when it
    cannot mint one: the caller must then not steer. Shared by every steer path (the worker's
    follow-up steer, the client's explicit steer); voice runs lower in voice_calls.steer_voice_run."""
    from core.services import local_session_grants

    try:
        trigger = await messaging_repo.get_message_by_id(run["roomId"], run.get("triggerMessageId") or "")
    except Exception:
        logger.warning("steer: trigger lookup failed; lowering the run's grant", exc_info=True)
        trigger = None
    author = (trigger or {}).get("author") or {}
    requester = author.get("id") if author.get("kind") == "human" else None
    if steerer_id is not None and steerer_id == requester:
        return None
    return local_session_grants.mint(owner_id, run["roomId"], run["agentId"], run["triggerMessageId"], None)


class OpenClawWorkWorker:
    def __init__(self, *, sqs_client: Any | None = None, http_client: Any | None = None) -> None:
        self._sqs = sqs_client
        self._http = http_client

    async def _process_message(self, message: dict) -> bool:
        """One message; True when completed (deleted). scripts/work_lambda.py reads it."""
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
            elif pointer["kind"] == "room_follow_up":
                await self._room_follow_up(pointer)
            elif pointer["kind"] == "network_wake":
                await self._network_wake(pointer)
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
            # visibility, or the rest of the FIFO group queues behind it.
            await self._return_visibility(message, timeout=exc.visibility or _RETRY_VISIBILITY_SECONDS)
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
        return completed

    async def _return_visibility(self, message: dict, timeout: int = 0) -> bool:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    self._sqs.change_message_visibility,
                    QueueUrl=settings.OPENCLAW_WORK_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=timeout,
                )
            )
            return True
        except Exception:
            logger.warning("Could not change FIFO visibility", exc_info=True)
            return False

    async def _room_dispatch(self, pointer: dict[str, Any]) -> None:
        room_id = pointer["roomId"]
        agent_id = pointer["agentId"]
        through = pointer["throughSeq"]
        cursor = await messaging_repo.get_agent_delivery(room_id, agent_id)
        if cursor is None:
            # The feed's agents are authors, never seated, so a seat never made their cursor: the
            # first wake starts it just before itself (FIFO per room+agent keeps the order).
            meta = await messaging_repo.get_room(room_id)
            if not (meta and is_feed_room(meta) and await room_agent(room_id, agent_id, meta)):
                return
            await messaging_repo.ensure_agent_delivery(room_id, agent_id, through - 1)
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
            member = await room_agent(room_id, agent_id, meta)
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
            # Queued before an archive or delete: skip, advance.
            targeted = target is not None and not meta.get("archivedAt") and not meta.get("deletedAt")
            delivered = await self._dispatch_message(meta, member, message, target["eventKind"]) if targeted else False
            if not await messaging_repo.advance_agent_delivery(room_id, agent_id, expected, seq, accepted=delivered):
                latest = await messaging_repo.get_agent_delivery(room_id, agent_id)
                if latest is None:
                    return
                actual = int(latest["lastSeq"])
                if actual >= seq:
                    expected = actual
                    continue
                raise WorkRetry("agent cursor compare-and-set failed")
            expected = seq
            if delivered:
                # The runtime accepted a turn on this message: "Delivered".
                await publish_delivery_changed(room_id, agent_id, seq)

    async def _room_follow_up(self, pointer: dict[str, Any]) -> None:
        """Wake the agent whose async delegation finished, in the thread that started it.
        Anything that has since gone away drops: the follow-up is advisory, not canonical."""
        room_id, agent_id = pointer["roomId"], pointer["agentId"]

        def drop(outcome: str) -> None:
            put_metric("messaging.followup.dispatched", dimensions={"outcome": outcome})

        row = await messaging_repo.get_follow_up(room_id, pointer["followUpId"])
        if row is None:
            return drop("expired")
        meta = await messaging_repo.get_room(room_id)
        if meta is None or meta.get("deletedAt"):
            return drop("room_gone")
        if meta.get("archivedAt"):
            return drop("archived")
        member = await room_agent(room_id, agent_id, meta)
        if member is None:
            return drop("not_member")
        message = await messaging_repo.get_message_by_id(room_id, row["triggerMessageId"])
        if message is None:
            return drop("message_gone")
        if row.get("reason") == STEER_LOST:
            # A follow-up steered into a run that failed before answering: its own normal turn.
            await self._dispatch_message(meta, member, message, row["eventKind"], steer=False)
            return drop("steer_redispatched")
        await self._dispatch_message(
            meta,
            member,
            message,
            row["eventKind"],
            follow_up={"id": pointer["followUpId"], "reason": row["reason"], "text": row["text"]},
        )
        put_metric("messaging.followup.dispatched", dimensions={"outcome": "dispatched"})

    async def _dispatch_message(
        self,
        meta: dict[str, Any],
        member: dict[str, Any],
        message: dict[str, Any],
        event_kind: str,
        *,
        follow_up: dict[str, str] | None = None,
        steer: bool = True,
    ) -> bool:
        owner_id = meta["ownerId"]
        run_key = message["messageId"] if follow_up is None else f"{message['messageId']}:followup:{follow_up['id']}"
        run_id = messaging_repo.run_id_for(member["id"], run_key)
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("agentcore_runtime_arn"):
            # Nothing to wake: AgentCore starts the microVM on the invoke below, so the only
            # reason there is no addressable runtime is that provisioning has not finished.
            # Retry rather than drop -- the queue's own backoff is the wait.
            put_metric("messaging.wake.outcome", dimensions={"outcome": "no_runtime"})
            raise WorkRetry("runtime is not provisioned yet")
        token = container.get("gateway_token")
        if not isinstance(token, str) or not token:
            raise WorkRetry("gateway token unavailable")
        room_members = await messaging_repo.list_members(meta["roomId"])
        members = with_human_handles(room_members, await human_emails(room_members))
        if not any(row.get("kind") == "agent" and row.get("id") == member["id"] for row in members):
            members.append(member)  # a feed author is not seated; the bridge needs it in participants
        from core.services import local_sessions

        # live_sessions_for_room withholds the summary itself for a room shared
        # with another organization (#2644).
        sessions = await local_sessions.live_sessions_for_room(owner_id, meta["roomId"], room_members)
        body = await build_ingress_body(
            meta=meta,
            target_agent_id=member["id"],
            event_kind=event_kind,
            message=message,
            members=members,
            # On the wire only when a session is live; the plugin reads a missing field as [].
            sessions=sessions,
            history_through_seq=int(meta.get("lastSeq") or message["messageSeq"]) if follow_up is not None else None,
            allow_oversize=True,
        )
        oversized = body.pop("_oversized", False)
        # Who started this run (T35): the Stop ledger and the Monday "stop rate after Jev" read it.
        if follow_up is not None:
            routed_by, routing_p = "follow_up", None
        elif event_kind == "user_request":
            routed_by, routing_p = ("dm" if meta.get("kind") in ("dm", "group-dm") else "mention"), None
        else:
            routed_by, routing_p = event_kind, None  # room_event: set below from Jev's decision
        if follow_up is None:
            # Reply where the conversation is: a top-level message in the main channel (or DM), a
            # threaded one in its thread. Without this the bridge threads every channel reply
            # (its old default), which the owner rejected. Jev's explicit placement overrides it
            # below; a follow-up keeps the bridge's placement.
            body["replyThreadRootId"] = message.get("threadRootMessageId")
        if event_kind == "room_event" and follow_up is None:
            # This agent's own live run in this conversation hears the line BEFORE Jev decides
            # (routing v2 §4.1): Jev may pick someone else or nobody, and a run that never saw
            # "Bob: $42k" posts its duplicate anyway. Jev's state.open then shows the run.
            if steer and await self._steer_live_run(meta, member, message, container, token, event_kind):
                return True
            steer = False  # tried: no live run took it, so the turn below is the message's own
            # Jev picks at most one Listening responder per message (spec §3a/§5a). A follow-up is
            # deterministic (the agent asked for that result): never routed.
            from core.services import jev

            decision = await jev.route(
                meta,
                member["id"],
                message,
                latest=body.get("message") or message,
                members=members,
                history=body.get("recentHistory") or [],
            )
            candidates = [
                item["agentId"]
                for item in (message.get("dispatchTargets") or [])
                if item.get("eventKind") == "room_event"
            ]
            if decision is not None and decision.get("retry"):
                # Jev was rate-limited: every consumer backs off and re-reads; the first one back
                # re-claims and asks again. Jitter spreads a burst's retries apart.
                put_metric("messaging.jev.route", dimensions={"outcome": "retry_rate_limited"})
                backoff = max(1, math.ceil(float(decision.get("retryAt") or 0) - time.time())) + random.randint(0, 5)
                raise WorkRetry("Jev rate-limited; retrying the routing decision", visibility=backoff)
            if decision is None and len(candidates) >= 2:
                # route() returns None only when routing itself errored, so this consumer can't
                # see what the others decided: guessing could run beside Jev's real pick, and
                # skipping could silence the agent Jev chose. Retry: the next delivery re-reads
                # the shared decision (the queue's own backoff is the wait).
                put_metric("messaging.jev.route", dimensions={"outcome": "error_retry"})
                raise WorkRetry("Jev routing errored; retrying to read the shared decision")
            forced = (decision or {}).get("forced")
            if decision is None or decision.get("fallback") or forced:
                if (message.get("author") or {}).get("kind") != "human":
                    # An agent's message with Jev unable to decide wakes nobody: the fallback
                    # below always picks someone, and the CEO-first pick made two agents answer
                    # each other indefinitely. Only a human must never talk into silence.
                    put_metric("messaging.jev.route", dimensions={"outcome": "fallback_agent_none"})
                    return False
                if any(item.get("eventKind") == "user_request" for item in (message.get("dispatchTargets") or [])):
                    # Somebody is already answering this exact message directly (an @mention or a
                    # DM target has their own user_request run) -- the fallback exists so a human
                    # is never left talking into silence, and here they are not. Don't also wake
                    # ambient room_event listeners onto a message addressed to someone else (prod
                    # 2026-09-27: "@friday ..." correctly ran Friday, and this fallback incorrectly
                    # also ran the lowest-id other active agent).
                    put_metric("messaging.jev.route", dimensions={"outcome": "fallback_addressed"})
                    return False
                if jev.speaks_to_people(message, members):
                    # Spoken to a person: without Jev nothing can judge whether a reply helps, and
                    # blind replies are what made a lone listener answer "8" and "@aden @leo" (prod
                    # 2026-09-27, Jev down). Silence is the default here.
                    put_metric("messaging.jev.route", dimensions={"outcome": "fallback_to_people"})
                    return False
                if len(candidates) >= 2:
                    # Jev down or undecided with more than one candidate (or a "none" the policy
                    # refused): dispatch exactly one deterministic responder (#2942).
                    chosen = await _fallback_single(owner_id, candidates, message, body.get("recentHistory") or [])
                    if member["id"] != chosen:
                        put_metric("messaging.jev.route", dimensions={"outcome": "skipped"})
                        return False
                    put_metric("messaging.jev.route", dimensions={"outcome": "forced" if forced else "fallback_single"})
                else:
                    # Jev down or undecided, and only one candidate: it runs, the bridge gate decides.
                    put_metric("messaging.jev.route", dimensions={"outcome": "forced" if forced else "fallback"})
                if forced:
                    # Once per message: only the one agent that runs gets here.
                    put_metric("messaging.jev.forced", dimensions={"reason": forced})
                routed_by = "fallback"
            elif decision.get("who") == member["id"]:
                # ``who`` is final: jev already decided whether silence was clear.
                put_metric("messaging.jev.route", dimensions={"outcome": "routed"})
                routed_by = "jev"
                try:
                    routing_p = float((decision.get("whoP") or {}).get(member["id"]))
                except (TypeError, ValueError):
                    routing_p = None
                placement = decision.get("placement")
                body["routing"] = {"decidedBy": "jev", "placement": placement}
                # Jev's judgement, not a rule: "thread" joins the trigger's thread (or starts one
                # under it), "main" is top level even for a threaded trigger.
                body["replyThreadRootId"] = (
                    (message.get("threadRootMessageId") or message["messageId"]) if placement == "thread" else None
                )
            else:
                # Another agent or "none": no invoke, no run. Returning
                # lets _room_dispatch advance this agent's cursor as for an untargeted message.
                put_metric("messaging.jev.route", dimensions={"outcome": "skipped"})
                return False
        # The earlier read chose the work; this one is deliberately adjacent to
        # the side effect so queued work cannot outlive membership revocation.
        if await room_agent(meta["roomId"], member["id"], meta) is None:
            return False
        if (
            follow_up is None
            and steer
            and await self._steer_live_run(meta, member, message, container, token, event_kind)
        ):
            return True
        if follow_up is not None:
            body["eventId"] = follow_up["id"]
            body["followUp"] = follow_up
        workspace_id = await messaging_repo.workspace_id_for_meta(messaging_repo.room_pk(meta["roomId"]), "META", meta)
        created = await messaging_repo.create_run(
            owner_id,
            run_id,
            room_id=meta["roomId"],
            agent_id=member["id"],
            workspace_id=workspace_id,
            trigger_message_id=message["messageId"],
            event_kind=event_kind,
            # A follow-up carries no placement; the bridge threads it like its own reply_thread_root.
            thread_root_id=body.get("replyThreadRootId")
            if follow_up is None
            else message.get("threadRootMessageId") or (None if meta.get("kind") == "dm" else message["messageId"]),
            routed_by=routed_by,
            routing_p=routing_p,
        )
        if oversized:
            # Route before reporting failure: Jev may choose another listener
            # or nobody. Only the selected agent's run gets a visible error.
            if created or await messaging_repo.redispatch_run(owner_id, run_id) is not None:
                await _close_refused_run(owner_id, run_id, 413, event_kind)
            return False
        body["run"] = {
            "runId": run_id,
            "workspaceId": workspace_id,
            "frameChannel": f"/ws/{workspace_id}/rooms/{meta['roomId']}/{meta['deliveryKey']}",
            "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        }
        if not created and await messaging_repo.redispatch_run(owner_id, run_id) is None:
            # The runtime already started it, it ended, or it was stopped. Invoking again would
            # re-run the turn -- its tokens and every side effect -- because the bridge's replay
            # guard does not survive a microVM restart.
            put_metric("messaging.run.dispatched", dimensions={"outcome": "already_handled", "event_kind": event_kind})
            run = await messaging_repo.get_run(owner_id, run_id)
            return bool(run and run.get("startedAt"))
        put_metric(
            "messaging.run.dispatched",
            dimensions={"outcome": "new" if created else "redelivered", "event_kind": event_kind},
        )
        from core.services import local_session_grants
        from core.services.messaging import ApiProblem

        invocation = None
        try:
            author = message.get("author") or {}
            invocation = local_session_grants.mint(
                owner_id,
                meta["roomId"],
                member["id"],
                message["messageId"],
                author.get("id") if author.get("kind") == "human" else None,
            )
        except (RuntimeError, ApiProblem):
            logger.warning("Local session invocation unavailable")
        ingress_path = f"/plugins/lightbulb-channel/inbound/{member['id']}"
        started = time.perf_counter()
        try:
            response = await self._post(container, ingress_path, body, token, invocation)
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
                ended = await messaging_repo.end_run(owner_id, run_id, outcome="not_admitted")
                if ended is not None:
                    from core.services import meeting_notes  # meeting_notes imports this module

                    await meeting_notes.on_notes_run_ended(owner_id, ended)
                put_metric("messaging.ambient_gate", dimensions={"outcome": "not_admitted", "event_kind": event_kind})
                return False
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                # Permanent: the bridge refuses this same body on every retry (prod 2026-09-28: a
                # zip in history 400'd ~30 retries into the DLQ and the agent went silent for
                # good). Close the run visibly; returning advances the cursor past it.
                await _close_refused_run(owner_id, run_id, response.status_code, event_kind)
                return False
            if response.status_code not in (200, 202):
                raise WorkRetry(f"plugin refused ingress ({response.status_code})")
            try:
                status = response.json().get("status")
            except (AttributeError, TypeError, ValueError):
                status = "unknown"
            if status not in {"accepted", "replayed"}:
                status = "unknown"
            put_metric("messaging.ingress.ack", dimensions={"status": status, "event_kind": event_kind})
            return True
        except IngressTooLarge:
            # Run metadata can push a near-cap body over the final wire limit.
            await _close_refused_run(owner_id, run_id, 413, event_kind)
            return False
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

    async def _steer_live_run(
        self,
        meta: dict[str, Any],
        member: dict[str, Any],
        message: dict[str, Any],
        container: dict[str, Any],
        token: str,
        event_kind: str,
    ) -> bool:
        """One run per agent per conversation: a human follow-up to an agent whose run in the
        same thread (or DM) is still live is steered into that run instead of queueing a second
        one. True only when the runtime took it; anything else (no live run, the run is
        finishing, an old image, a transport error) falls back to a normal turn, which is at
        worst the message delivered twice -- never lost."""
        author = message.get("author") or {}
        if author.get("kind") != "human" or message.get("attachments"):
            return False  # a steer carries text only; attachments need a turn of their own
        root = message.get("threadRootMessageId")
        try:
            runs = await messaging_repo.list_open_runs_for_room(meta["ownerId"], meta["roomId"])
        except Exception:
            logger.warning("steer: open-run lookup failed; dispatching a turn", exc_info=True)
            return False
        if any(run.get("triggerMessageId") == message["messageId"] for run in runs):
            return False  # a redelivery of a message that already has its own run
        live = [
            run
            for run in runs
            if run.get("agentId") == member["id"]
            and run.get("status") in ("dispatched", "started")
            # Same conversation: any DM; else the run's own thread (a reply under its trigger,
            # or the thread it answers in). Rows from before threadRootId existed only match a
            # reply under their trigger.
            and (
                meta["kind"] == "dm"
                or root == run.get("triggerMessageId")
                # In a channel each top-level message starts its own thread, so one with no root
                # never joins a run whose root is also empty (run 10, 3 Nov 2026, was steered into
                # run 9's run and never got a turn). A group DM is one flat conversation: it does.
                or ((root is not None or meta.get("kind") != "channel") and run.get("threadRootId") == root)
            )
        ]
        if not live:
            return False
        run = max(live, key=lambda row: row.get("dispatchedAt") or "")
        if await room_agent(meta["roomId"], member["id"], meta) is None:
            return False  # adjacent to the side effect: queued work must not outlive membership
        ambient = event_kind == "room_event"
        kind = "ambient" if ambient and run.get("eventKind") == "room_event" else "addressed"
        text = f"{author.get('displayName') or 'A teammate'}: {message.get('text') or ''}"
        if kind == "ambient":
            # An ambient line into an ambient run may stand it down (routing v2 §4.1; the bridge
            # maps a [SILENT] pass, also after a steer). Anything addressed -- the run, or the
            # line (an @mention into an ambient run) -- keeps the bare line: the person chose
            # that agent, and a hedged human answer must not silence it.
            text += AMBIENT_STEER_SUFFIX
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if lowered := await steer_grant(meta["ownerId"], run, author.get("id")):
                headers["X-Lightbulb-Local-Session-Token"] = lowered
        except (RuntimeError, ApiProblem, KeyError):
            return False  # the grant cannot be lowered: the message gets a turn (and grant) of its own
        try:
            response = await runtime_transport.request(
                container,
                f"/plugins/lightbulb-channel/runs/{run['runId']}/steer",
                method="POST",
                json_body={"text": text, "messageId": message["messageId"]},
                headers=headers,
                timeout_s=10.0,
            )
            steered = response.status_code == 202
        except (httpx.TimeoutException, httpx.TransportError):
            steered = False
        put_metric("messaging.run.steered", dimensions={"outcome": "steered" if steered else "fallback", "kind": kind})
        if not steered:
            return False
        try:
            # Recorded so a run that fails before answering re-dispatches it
            # (messaging.redispatch_lost_steers) -- a room event back through Jev, never as an
            # answer forced from this agent. A run that ended meanwhile never saw it.
            if not await messaging_repo.add_steered_message(
                meta["ownerId"], run["runId"], message["messageId"], ambient=ambient
            ):
                put_metric("messaging.run.steered", dimensions={"outcome": "ended_fallback", "kind": kind})
                return False
        except Exception:
            logger.warning("steer record failed; the steer stands", exc_info=True)
        frame = {"type": "steered", "runId": run["runId"], "agentId": member["id"], "messageId": message["messageId"]}
        try:
            # The "<agent> saw this" receipt. Best-effort: the steer already landed, and a retry
            # here would re-steer the same text.
            await get_live_event_publisher().publish_room(
                meta["roomId"],
                make_live_event(
                    "room.run.frame",
                    {"roomId": meta["roomId"], "agentId": member["id"], "runId": run["runId"], "frame": frame},
                    room_id=meta["roomId"],
                ),
            )
        except Exception:
            logger.warning("steer receipt publish failed", exc_info=True)
        return True

    async def _post(
        self,
        container: dict[str, Any],
        path: str,
        body: dict[str, Any],
        token: str,
        invocation: str | None = None,
    ):
        """Deliver one ingress to the owner's gateway.

        The 1 MiB cap is the plugin's own contract, enforced here rather than relying on
        AgentCore's far higher invoke-payload ceiling.
        """
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if invocation is not None:
            headers["X-Lightbulb-Local-Session-Token"] = invocation
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(content) > _INGRESS_MAX_BYTES:
            raise IngressTooLarge("Lightbulb ingress exceeds 1 MiB")
        return await runtime_transport.request(
            container,
            path,
            method="POST",
            json_body=body,
            headers=headers,
            # This is the call that wakes a parked workspace: AgentCore holds the invoke
            # through the microVM start (~35s) and the gateway boot (measured 54-57s end
            # to end on 2026-09-16), so the budget must outlast a cold start. A warm
            # session acks in milliseconds; queue visibility must outlast this budget.
            timeout_s=_AGENTCORE_INGRESS_TIMEOUT_S,
        )

    async def _network_wake(self, pointer: dict[str, Any]) -> None:
        """The invoke starts the microVM; the bridge's ``network.wake`` starts the sidecar. Best
        effort: a failed wake is not retried, the ringing Mac rings again."""
        from core.services import container_rpc

        try:
            await container_rpc.call(pointer["ownerId"], "network.wake", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("network wake failed owner=%s (%s)", pointer["ownerId"], type(exc).__name__)

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


async def _close_refused_run(owner_id: str, run_id: str, status_code: int, event_kind: str) -> None:
    from core.services.messaging import end_run_and_announce, post_run_failure_message, publish_run_error_frames

    logger.warning("runtime refused ingress permanently (%s) run=%s", status_code, run_id)
    put_metric("messaging.ingress.refused", dimensions={"status": str(status_code), "event_kind": event_kind})
    ended = await end_run_and_announce(owner_id, run_id, outcome="error", error_kind=INGRESS_REFUSED)
    if ended is None:
        return  # already closed by an earlier delivery: its note is posted
    await post_run_failure_message(owner_id, ended, "error", INGRESS_REFUSED)
    try:
        await publish_run_error_frames(ended, INGRESS_REFUSED)
    except Exception:
        logger.warning("ingress refusal frame publish failed run=%s", run_id, exc_info=True)
