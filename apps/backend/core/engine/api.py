"""The engine's operations for products (engine-service plan Task 4; research `2026-11-20-engine-boundary.md` §6.1).

Lightbulb Phone and Lightbulb Meetings call these, and only these, to put lines in a conversation, start and answer
tasks and read what the agents said. In process today; the same shapes go over HTTP later (`core.engine.contract`).
Every operation takes the tenant explicitly: none reads a Clerk organisation, and a `member` principal is the tenant's
member on the product's word (as the phone trusts a call's `ownerUserId`), never looked up. A refusal is an
`EngineError` carrying the status and body the routes answered before; `http` turns it back into that `HTTPException`
for a route.

Ids, sequence numbers, idempotency keys and trigger ids are the ones products already hold for live calls.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from typing import Any, TypeVar

from fastapi import HTTPException
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from core import tasks_schema as ts
from core.auth import AuthContext
from core.constants import SYSTEM_ACTOR_ID
from core.dynamodb import utc_now_iso
from core.engine import hooks
from core.engine.contract import (
    PRODUCT_META_FIELDS,
    ActIn,
    AnnotateIn,
    AnswerIn,
    ConversationOut,
    EngineError,
    EventIn,
    EventOut,
    LineIn,
    LineOut,
    OpenIn,
    Principal,
    QuestionRef,
    StartIn,
    StartOut,
    start_task_id,
)
from core.engine.hooks import EngineEvent
from core.repositories import effects_repo, messaging_repo, questions_repo, tasks_repo
from core.services import admission, chat_gate, container_rpc, messaging, questions, task_conversation, tasks_access
from core.services.messaging import human_display_name
from core.temporal import client as temporal_client
from core.temporal.task_questions import AnswerRef
from core.temporal.types import ActionRequest, StopRequest

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def http(call: Awaitable[T]) -> T:
    """A route's adapter: an operation's refusal as the HTTPException the route raised before (same status, body)."""
    try:
        return await call
    except EngineError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


def _problem(status: int, code: str, **extra: Any) -> EngineError:
    return EngineError(code, status, {"code": code, **extra})


def _refused(e: messaging.ApiProblem) -> EngineError:
    """The app's own refusal (a room door, a create), with its status and body."""
    return EngineError(e.error.code, e.status_code, e.body())


def _not_found() -> EngineError:
    return EngineError("not_found", 404, {"code": "not_found"})


async def _owned(tenant_id: str, conversation_id: str, *, include_deleted: bool = False) -> dict:
    """The conversation's record, when it is this tenant's and not deleted (`include_deleted`: a read that asks for
    a deleted one too); else not found. An operation names a conversation only after this, so one tenant's line or
    task never lands in another's room."""
    meta = await messaging_repo.get_room(conversation_id)
    if not meta or meta.get("ownerId") != tenant_id or (meta.get("deletedAt") and not include_deleted):
        raise _not_found()
    return meta


# --- conversations ------------------------------------------------------------------------------------------------


def _conversation(conversation_id: str, meta: dict, members: list[dict] | None = None) -> ConversationOut:
    return ConversationOut(
        conversation_id=conversation_id,
        tenant_id=meta["ownerId"],
        kind=meta.get("kind") or "",
        revision=int(meta.get("revision", 0)),
        created_at=meta.get("createdAt"),
        front_agent_id=meta.get("frontAgentId"),
        archived=bool(meta.get("archivedAt")),
        deleted=bool(meta.get("deletedAt")),
        members=members or [],
        annotations={k: meta[k] for k in PRODUCT_META_FIELDS if k in meta},
    )


async def get_conversation(
    tenant_id: str,
    conversation_id: str,
    *,
    with_members: bool = False,
    member: str | None = None,
    include_deleted: bool = False,
) -> ConversationOut | None:
    """The tenant's conversation (its members too, `with_members`), or None: missing, deleted (unless
    `include_deleted`: then returned with `deleted`) or another tenant's.
    With `member`, only as that person may open it in the app: the room door's refusal (not found, no longer a
    member) is raised, with its status and body."""
    if member is not None:
        try:
            meta, _ = await messaging.require_room_member(tenant_id, member, conversation_id)
        except messaging.ApiProblem as e:
            raise _refused(e) from e
        if meta.get("ownerId") != tenant_id:  # a guest in another tenant's shared room
            return None
        return _conversation(
            conversation_id, meta, await messaging_repo.list_members(conversation_id) if with_members else []
        )
    reads = [
        _owned(tenant_id, conversation_id, include_deleted=include_deleted),
        *([messaging_repo.list_members(conversation_id)] if with_members else []),
    ]
    try:
        meta, *members = await asyncio.gather(*reads)
    except EngineError:
        return None
    return _conversation(conversation_id, meta, members[0] if members else [])


async def get_front_agent(tenant_id: str, conversation_id: str | None = None) -> str | None:
    """The agent that speaks for the tenant as Lightbulb (in `conversation_id`, the one it seats), or None when its
    chosen one (or every one) is unavailable, or the conversation is not the tenant's."""
    try:
        return await task_conversation.front_agent_id(tenant_id, room_id=conversation_id)
    except task_conversation.NoFrontAgent:
        return None


async def find_conversation(conversation_id: str, *, include_deleted: bool = False) -> ConversationOut | None:
    """The one read keyed without a tenant: a conversation by id alone, or None (missing, or deleted unless
    `include_deleted`). Only for a product's entry point that is handed nothing but the id and authenticates it with
    a credential the product stored on the conversation (a call's token); the tenant it returns is passed to every
    later operation."""
    meta = await messaging_repo.get_room(conversation_id)
    return _conversation(conversation_id, meta) if meta and (include_deleted or not meta.get("deletedAt")) else None


async def list_conversations(tenant_id: str, kinds: list[str], limit: int) -> list[ConversationOut]:
    """The tenant's `limit` newest conversations of `kinds`, newest first (by when they opened)."""
    rows = [r for r in await messaging_repo.list_owner_rooms(tenant_id) if r.get("kind") in kinds]
    rows = sorted(rows, key=lambda r: r.get("createdAt") or "", reverse=True)[:limit]
    metas = await asyncio.gather(*(messaging_repo.get_room(r["roomId"]) for r in rows))
    return [
        _conversation(r["roomId"], m)
        for r, m in zip(rows, metas)
        if m and m.get("ownerId") == tenant_id and not m.get("deletedAt")
    ]


async def open_conversation(req: OpenIn) -> ConversationOut:
    """Open a conversation, or get the one `client_key` already opened (a retry). A `call`: the creator as its owner
    member, the front agent seated when asked and there is one. A `channel`: the creator and every agent, on
    mentions. A `dm`: the creator's with its agent, found or created as the app opens it. The request hash is
    (tenant, kind, client key). An id already taken by a deleted conversation, or another tenant's, is not found."""
    if fields := sorted(set(req.annotations) - PRODUCT_META_FIELDS):
        raise _problem(400, "not_product_field", fields=fields)
    if req.kind == "dm":
        return await _open_dm(req)
    if not req.conversation_id:
        raise _problem(400, "invalid_open", missing=["conversationId"])
    ws = req.tenant_id
    members = [{"kind": "human", "id": req.creator.id, "displayName": req.creator.display_name,
                "mentionHandle": None, "role": "owner", "listenMode": None}]  # fmt: skip
    front = None
    if req.kind == "channel":
        name = req.creator.display_name or await messaging.human_display_name(req.creator.id)
        members = [{**members[0], "displayName": name}]
        members += await _channel_agents(ws)
        front = next((m for m in members if m.get("mentionHandle") == "lightbulb"), None)
        await messaging_repo.ensure_user_stream(ws, req.creator.id)
    elif req.seat_front_agent:
        try:
            front = await task_conversation._front_agent(ws)
            members.append(front)
        except Exception:  # noqa: BLE001 -- no front agent yet: it opens without one (front_agent_id None)
            logger.info("conversation %s opened without a front agent ws=%s", req.conversation_id, ws, exc_info=True)
    try:
        meta = await messaging_repo.create_room(
            owner_id=ws,
            room_id=req.conversation_id,
            client_room_id=req.client_key,
            creator_user_id=req.creator.id,
            name=req.name,
            kind=req.kind,
            members=members,
            req_hash=messaging_repo.request_hash(ws, req.kind, req.client_key),
            extra={
                **req.annotations,
                **({"hidden": True} if req.hidden else {}),
                **({"frontAgentId": front["id"]} if front else {}),
            },
        )
    except messaging_repo.MessagingRepoError:
        meta = await messaging_repo.get_room(req.conversation_id)  # a replay keeps the conversation it opened
        if not meta:
            raise  # a create cancelled with nothing written: the caller retries
    if meta.get("ownerId") != ws or meta.get("deletedAt"):
        raise _not_found()
    return _conversation(req.conversation_id, meta)


async def _channel_agents(ws: str) -> list[dict]:
    """The tenant's agents a channel seats, on mentions: up to the member cap, its one human aside."""
    # ponytail: the room member cap bounds a channel to the first N agents
    roster = (await messaging.load_agent_roster(ws))[: messaging_repo.MAX_ROOM_MEMBERS - 1]
    handles = await messaging._handles_for(ws, roster)
    return [{"kind": "agent", "id": a["id"], "displayName": a["name"], "mentionHandle": handles[a["id"]],
             "role": "member", "listenMode": "mentions"} for a in roster]  # fmt: skip


async def seat_roster(tenant_id: str, conversation_id: str) -> None:
    """Agents the tenant has added since its channel opened join it on mentions, up to the member cap."""
    await _owned(tenant_id, conversation_id)
    present = {m["id"] for m in await messaging_repo.list_members(conversation_id)}
    roster = await messaging.load_agent_roster(tenant_id)
    handles = messaging.mention_handles(roster)
    for a in roster:
        if a["id"] in present or len(present) >= messaging_repo.MAX_ROOM_MEMBERS:
            continue
        member = {"kind": "agent", "id": a["id"], "displayName": a["name"], "role": "member"}
        member |= {"mentionHandle": handles[a["id"]], "listenMode": "mentions"}
        await messaging_repo.upsert_member(room_id=conversation_id, owner_id=tenant_id, member=member)
        present.add(a["id"])


async def get_roster(tenant_id: str) -> list[dict]:
    """The tenant's agents ({id, name, ...}), the front agent named as Lightbulb."""
    return await messaging.load_agent_roster(tenant_id)


async def _open_dm(req: OpenIn) -> ConversationOut:
    ws, member = req.tenant_id, req.creator.id
    try:
        front = {"id": req.agent_id} if req.agent_id else await task_conversation._front_agent(ws)
        room = await messaging.create_room(
            AuthContext(user_id=member, org_id=ws),
            messaging.CreateRoomRequest(
                client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{req.client_key}|{front['id']}"),
                name=None,
                kind="dm",
                members=[messaging.CreateRoomMember(kind="agent", id=front["id"], role="member", listen_mode="active")],
            ),
        )
    except task_conversation.NoFrontAgent as e:
        raise _problem(409, "no_front_agent") from e
    except messaging.ApiProblem as e:  # a member no longer in the workspace
        raise _refused(e) from e
    return _conversation(str(room.room_id), await _owned(ws, str(room.room_id)))


# --- annotate -------------------------------------------------------------------------------------------------------


async def annotate(req: AnnotateIn) -> bool:
    """Write a product's own fields on its conversation's record (those of `PRODUCT_META_FIELDS` the record's writer
    keeps; any other key is refused, never dropped); the engine stores them and never reads them. With
    `expected_revision`, a writer since the caller's read cancels it (botocore's `TransactionCanceledException`, as
    `patch_room_meta` raises it, so the caller's merge loop retries). With `if_absent`, False when the key is there."""
    if fields := sorted(set(req.set) - (PRODUCT_META_FIELDS & set(messaging_repo.ROOM_META_FIELDS))):
        raise _problem(400, "not_product_field", fields=fields)
    await _owned(req.tenant_id, req.conversation_id)
    if req.if_absent is None:
        await messaging_repo.patch_room_meta(req.conversation_id, req.set, expected_revision=req.expected_revision)
        return True
    claim = req.set.get("meeting")
    if set(req.set) != {"meeting"} or not isinstance(claim, dict) or req.if_absent not in claim:
        raise _problem(400, "bad_claim")
    also = {k: v for k, v in claim.items() if k != req.if_absent}
    return await messaging_repo.set_meeting_field_if_absent(
        req.conversation_id, req.if_absent, claim[req.if_absent], also
    )


# --- append_line ---------------------------------------------------------------------------------------------------


async def append_line(req: LineIn) -> LineOut:
    """A line in a conversation, by `principal.kind`. `member`: a person's message (`create_human_message`, client id
    the uuid5 of `idempotency_key`), after it answers the said question `answers` names (then it wakes nobody).
    `contact`: an outside caller's line (silent, keyed on `idempotency_key`) and its trigger `trigger_key`, taken by
    the conversation's open thread or else admitted. `system`: Lightbulb's own line (`post_as_lightbulb`, event id
    `idempotency_key`). `notice`: the platform's notice line, by no agent and waking nobody, keyed on
    `idempotency_key` (a retry under the key, whatever its words, is the first line); an archived conversation
    refuses it (409 `not_posted`). A `system` or `notice` line's `seq` is not reported."""
    await _owned(req.tenant_id, req.conversation_id)
    if req.principal.kind == "contact":
        return await _contact_line(req)
    if req.principal.kind == "notice":
        line = await messaging._post_redirected_line(req.tenant_id, req.conversation_id, req.idempotency_key, req.text)
        if line is None:
            raise _problem(409, "not_posted")
        return LineOut(message_id=line["messageId"])
    if req.principal.kind == "system":
        message_id = await task_conversation.post_as_lightbulb(
            req.tenant_id,
            req.conversation_id,
            text=req.text,
            event_id=req.idempotency_key,
            attrs=req.system_attrs,
            remember=req.remember,
            in_reply_to=req.in_reply_to,
        )
        return LineOut(message_id=message_id)
    member, key = req.principal.id, req.idempotency_key
    answered = await _answer_said_question(req.tenant_id, req.conversation_id, member, req.answers, req.text, key)
    msg = await messaging.create_human_message(
        AuthContext(user_id=member, org_id=req.tenant_id),
        req.conversation_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            text=req.text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via=req.via,
        wake=req.respond == "auto" and not answered,
    )
    return LineOut(message_id=str(msg.message_id), seq=msg.message_seq, answered=answered)


async def _contact_line(req: LineIn) -> LineOut:
    """An outside caller's line (the product has checked the caller may speak): the caller's message, authored by the caller (a non-member: no admission row, no memory), and its trigger for the conversation, so the
    Router decides it with the caller's scope, or the conversation's open thread takes it."""
    if req.via is None or req.turn is None:  # the trigger's source and the request hash
        raise _problem(400, "invalid_line", missing=[k for k in ("via", "turn") if getattr(req, k) is None])
    ws, requester, call_id = req.tenant_id, req.requester_user_id, req.conversation_id
    who = req.principal.display_name
    key = req.idempotency_key
    msg, _ = await messaging_repo.create_message(
        room_id=call_id,
        owner_id=ws,
        idemp_pk=messaging_repo.owner_pk(ws),
        idemp_sk=messaging_repo.agent_idemp_sk(key),
        req_hash=messaging_repo.request_hash(call_id, req.turn, req.text),
        author={"kind": "human", "id": req.principal.id, "displayName": who},
        text=req.text,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=None,
        client_message_id=None,
        silent=True,
        author_member_check=False,
        via=req.via,
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=ws,
        trigger_id=ts.trigger_id(req.via, call_id, req.trigger_key),
        source=req.via,
        admission="respond",
        lane="shared",  # the batch key is the conversation (trigger_scope): a call never shares a Router prompt
        actor_user_id=requester,  # the product's accountable user for the line, never the model
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "roomId": call_id,
            "messageId": msg["messageId"],
            **({"origin": req.origin} if req.origin else {}),
            **req.product_ref,
            "text": f"{req.text}\n\n{req.hint}" if req.hint else req.text,
        },
    )
    if task_id := await _open_thread(ws, call_id, int(msg["messageSeq"]), req.origin):
        # The conversation's open thread takes the line, as a typed reply in a task's thread does (admission's
        # pass-through): it already holds what it offered and the caller's answers (8 Nov prod final smoke: each
        # line started a new thread, and the "yes" thread asked again and picked a time never offered).
        trig = {**trigger.model_dump(by_alias=True), "routedTo": {"taskId": task_id}}
        trig["ref"]["deliverRoomId"] = call_id
        await tasks_repo.put_trigger(ws, trig)
        if blocked := await task_conversation.gate_input(ws, task_id, trig):  # §14.10, as admission's pass-through
            await task_conversation.post_as_lightbulb(
                ws,
                call_id,
                text=blocked,
                event_id=f"task-gate:{task_id}:{trigger.trigger_id}",
                remember=False,
                in_reply_to=msg["messageId"],
                attrs={"voiceWaiting": False},
            )
            task_id = None
        else:
            await admission._to_task(ws, task_id, trigger.trigger_id)
    else:
        await admission.enqueue_trigger(ws, trigger, room_id=call_id)
    return LineOut(message_id=msg["messageId"], seq=int(msg["messageSeq"]), trigger_id=trigger.trigger_id,
                   task_id=task_id)  # fmt: skip


_CLOSED = frozenset({"resolved", "failed"})  # a task that takes no more input


async def _open_thread(ws: str, call_id: str, before_seq: int, origin: str | None) -> str | None:
    """The conversation's latest thread of `origin` still open (its card on a Lightbulb line in the room), else
    None: a conversation's first line, or a line after its thread failed or was closed, goes to the Router."""
    for row in reversed(await messaging_repo.list_messages_before(call_id, before_seq, 50)):
        for ref in reversed(row.get("taskRefs") or []):
            task = await tasks_repo.get_task(ws, ref.get("taskId") or "")
            if (
                task
                and task.get("origin") == origin
                and (task.get("audience") or {}).get("roomId") == call_id  # this conversation's, never another's
                and task.get("status") not in _CLOSED
            ):
                return task["taskId"]
    return None


async def _answer_said_question(owner_id: str, room_id: str, member: str, ref: QuestionRef | None, text: str,
                               key: str) -> bool:  # fmt: skip
    """The line said right after the voice said a Worker's question answers it, as `member`, through the card's own
    path (`_submit_answer`): a message in the room never answers a Question (task_questions.py). False (the line
    then goes to the Router as a message): no ref, a question this room never got, or a refused answer (closed, not
    an answerer, an approval: a spoken yes never approves)."""
    if ref is None:
        return False

    rows = await messaging_repo.list_messages_before(room_id, 10**19, 100)
    if not any(_question_ref(r) == ref.model_dump(by_alias=True) for r in rows):
        return False
    body = AnswerIn(tenant_id=owner_id, task_id=ref.task_id, qid=ref.qid, principal=Principal(kind="member", id=member),
                    option_id="answer", text=text[:4000],
                    client_answer_id=uuid.uuid5(uuid.NAMESPACE_URL, key).hex)  # fmt: skip
    try:
        await _submit_answer(owner_id, ref.task_id, ref.qid, member, body, member=True)
    except (EngineError, HTTPException) as e:  # a refusal, or a product hook's HTTPException (a call gone)
        logger.info("voice room %s: question %s not answered by voice (%s)", room_id, ref.qid, e.detail)
        return False
    return True


# --- start_task, submit_event --------------------------------------------------------------------------------------


async def start_task(req: StartIn) -> StartOut:
    """A task started with no Router model turn (a scripted `ref.start`), answered in `conversation_id`. The same
    `trigger_id` is the same task. `lane="auto"`: the requester's private lane in a DM, else shared. `ask`
    (`task_asks.open_ask`'s arguments): the task opens waiting on that Question instead of running first."""
    meta = await _owned(req.tenant_id, req.conversation_id)
    requester = req.principal.id if req.principal else None
    lane = "shared"
    if req.lane == "auto":
        lane = ts.router_lane_key(requester) if meta.get("kind") == "dm" else "shared"
    trigger_id = req.trigger_id
    task_id = start_task_id(trigger_id)
    # the opening Question's id: the product's, or one the trigger names, so a retried start opens it once
    ask = {**req.ask, "qid": req.ask.get("qid") or questions.qid_for(f"{trigger_id}:ask")} if req.ask else None
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=req.title,
        brief=req.brief,
        specialist=req.specialist,
        ack=req.ack or f"On it: {req.title}",
        visibility="room",
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=req.tenant_id,
        trigger_id=trigger_id,
        source=req.source,
        admission="respond",
        lane=lane,
        actor_user_id=requester,
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "deliverRoomId": req.conversation_id,
            **req.product_ref,
            "taskId": task_id,
            "start": start.model_dump(by_alias=True),
            **({"origin": req.origin} if req.origin else {}),
            **({"confirmAll": True} if req.confirm_all else {}),  # every effect of the task gets a card
            **({"ask": ask} if ask else {}),
        },
    )
    created = await admission.enqueue_trigger(req.tenant_id, trigger, room_id=req.conversation_id)
    return StartOut(task_id=task_id, trigger_id=trigger_id, created=created, qid=(ask or {}).get("qid"))


async def submit_event(req: EventIn) -> EventOut:
    """A source event for the Router with no message behind it: `respond` (it answers) or `candidate` (it decides
    whether to act). The same `trigger_id` is the same event."""
    if req.conversation_id:
        await _owned(req.tenant_id, req.conversation_id)
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=req.tenant_id,
        created_at=now,
        updated_at=now,
        trigger_id=req.trigger_id,
        source=req.source,
        admission=req.admission,
        lane="shared",
        actor_user_id=req.principal.id if req.principal else None,
        ref={
            **({"roomId": req.conversation_id} if req.conversation_id else {}),
            **({"origin": req.origin} if req.origin else {}),
            **({"confirmAll": True} if req.confirm_all else {}),
            **req.product_ref,
            "text": req.text,
        },
        received_at=now,
    )
    return EventOut(trigger_id=req.trigger_id, created=await admission.enqueue_trigger(req.tenant_id, trigger))


# --- voice lines ---------------------------------------------------------------------------------------------------


async def read_lines(tenant_id: str, conversation_id: str, after: int | None) -> dict:
    """The agent lines a voice says in the conversation after seq `after`, and the next cursor (`agent_lines`). With
    `after` None, no lines: only the first cursor, the newest seq, so a voice says only what comes after it."""
    await _owned(tenant_id, conversation_id)
    if after is None:
        rows = await messaging_repo.list_messages_before(conversation_id, 10**19, 1)
        return {"messages": [], "after": int(rows[-1]["messageSeq"]) if rows else 0}
    return await _agent_lines(tenant_id, conversation_id, after)


def _question_ref(row: dict) -> dict | None:
    """{taskId, qid} of a Worker's question line (task_questions.relay_question posts it in the origin thread)."""
    if row.get("kind") != "question":
        return None
    ref = next((t for t in row.get("taskRefs") or [] if t.get("qid") and t.get("taskId")), None)
    return {"taskId": ref["taskId"], "qid": ref["qid"]} if ref else None


async def _agent_lines(tenant_id: str, room_id: str, after: int) -> dict:
    """The agent messages in the room's main chat after seq `after`, oldest first, and the next cursor. A thread's
    lines and progress lines are not said, except a Worker's question in a thread this room started (with its
    `question` ref: the next line said to the voice answers it, `_answer_said_question`); the meeting never heard
    one and the task waited on it (8 Nov prod Meet). The voice says these word for word (huddle-agent mouth.py).
    `inReplyTo`: the human message a line answers (the voice matches it to that spoken ask); `action`: what the voice
    does with it (the phone's "transfer")."""
    from core.repositories import questions_repo

    rows, _ = await messaging_repo.list_messages(room_id, after, 100)
    said = []
    for r in rows:
        text, question = r.get("editedText") or r.get("text") or "", _question_ref(r)
        if (r.get("author") or {}).get("kind") != "agent" or r.get("progress") or r.get("deletedAt"):
            continue
        if question:  # answered or closed (on its card, or by voice) before the voice got to it: never said
            q = await questions_repo.get_question(tenant_id, question["taskId"], question["qid"])
            if (q or {}).get("state") != "open":
                continue
        if text.strip() and (question or not r.get("threadRootMessageId")):
            said.append({"id": r["messageId"], "seq": int(r["messageSeq"]), "text": text,
                         "waiting": bool(r.get("voiceWaiting", r.get("taskRefs") and r.get("kind") != "task_line")) if not question else False,
                         **({"taskIds": r.get("voiceTaskIds") or [ref["taskId"] for ref in r["taskRefs"]]} if r.get("voiceTaskIds") or r.get("taskRefs") else {}),
                         **({"question": question} if question else {}),
                         **{k: r[k] for k in ("inReplyTo", "action", "voiceDelivery") if r.get(k)}})  # fmt: skip
    return {"messages": said, "after": int(rows[-1]["messageSeq"]) if rows else after}


async def read_history(tenant_id: str, conversation_id: str, limit: int) -> list[dict]:
    """The conversation's `limit` newest messages, oldest first (the stored rows)."""
    await _owned(tenant_id, conversation_id)
    return await messaging_repo.list_messages_before(conversation_id, 10**19, limit)


# --- tasks ---------------------------------------------------------------------------------------------------------


async def get_task(tenant_id: str, task_id: str, *, tree: bool = False) -> dict | None:
    """The tenant's task, or None (tasks are keyed by tenant: another tenant's reads as missing). With `tree`, the
    task with its attempts and questions (`task` None when missing)."""
    if tree:
        return await tasks_repo.get_task_tree(tenant_id, task_id)
    return await tasks_repo.get_task(tenant_id, task_id)


async def get_trigger(tenant_id: str, trigger_id: str) -> dict | None:
    """The tenant's trigger row, or None."""
    return await tasks_repo.get_trigger(tenant_id, trigger_id)


async def list_conversation_tasks(tenant_id: str, conversation_id: str) -> list[dict]:
    """The tasks holding, or that held, the conversation's threads (open or done)."""
    await _owned(tenant_id, conversation_id)
    threads = await tasks_repo.list_room_threads(tenant_id, conversation_id)
    ids = dict.fromkeys(tid for t in threads for tid in (t["taskId"], *(t.get("history") or [])))
    return await tasks_repo.batch_get(tenant_id, [ts.task_sk(tid) for tid in ids])


async def get_lines(tenant_id: str, conversation_id: str, message_ids: list[str | None]) -> list[dict | None]:
    """The conversation's stored messages by id, in order (None for an unknown id)."""
    await _owned(tenant_id, conversation_id)
    return list(await asyncio.gather(*(messaging_repo.get_message_by_id(conversation_id, m) for m in message_ids)))


# --- answer --------------------------------------------------------------------------------------------------------

_STYLES = {"deny": "danger", "request_changes": "secondary"}


async def answer(req: AnswerIn) -> dict:
    """`principal` answers Question `qid` of `task_id` (`_submit_answer`): the question's view and the task's summary."""
    member = req.principal.kind == "member"
    return await _submit_answer(req.tenant_id, req.task_id, req.qid, req.principal.id, req, member=member)


async def _people(ids: list[str | None]) -> dict[str, str]:
    unique = [u for u in dict.fromkeys(ids) if u]
    return dict(zip(unique, await asyncio.gather(*(human_display_name(u) for u in unique))))


def _view(q: dict, viewer: str, names: dict[str, str]) -> dict[str, Any]:
    """`QuestionView` (spec 05 §5.1, states from contracts-v2 §3). `canAnswer` is the stored list;
    the answer route re-checks authority (`questions.can_answer_now`)."""
    answerers = list(q.get("answerers") or [])
    is_open = q["state"] == "open"
    if not is_open:
        waiting = None
    elif viewer in answerers:
        waiting = "Waiting on you"
    else:
        waiting = questions.waiting_label([names.get(u, u) for u in answerers])

    def person(uid: str | None) -> dict | None:
        return {"id": uid, "name": names.get(uid, uid)} if uid else None

    return {
        "id": q["qid"],
        "taskId": q["taskId"],
        "kind": q["kind"],
        "subkind": q.get("subkind"),
        "title": q["title"],
        "text": q.get("text", ""),
        "preview": q.get("preview"),
        "options": [
            {"id": o["id"], "label": o["label"], "style": _STYLES.get(o["id"], "primary" if i == 0 else "secondary")}
            for i, o in enumerate(q.get("options") or [])
        ],
        "state": q["state"],
        "askedBy": q.get("askedBy"),
        "answerers": [person(u) for u in answerers],
        "canAnswer": is_open and viewer in answerers,
        "waitingLabel": waiting,
        "answer": q.get("answer"),
        "answeredBy": person(q.get("answeredBy")),
        "answeredAt": q.get("answeredAt"),
        "reason": q.get("reason"),
        "cancelledBy": None if q.get("cancelledBy") in _NOT_A_PERSON else person(q.get("cancelledBy")),
        "cancelledAt": q.get("cancelledAt"),
        "govRef": q.get("govRef"),
        "createdAt": q["createdAt"],
        "version": q["version"],
    }


async def _views(qs: list[dict], viewer: str) -> list[dict]:
    names = await _people(
        [u for q in qs for u in (*(q.get("answerers") or []), q.get("answeredBy"), q.get("cancelledBy"))]
    )
    return [_view(q, viewer, names) for q in qs]


def _failure_codes(err: BaseException) -> set[str]:
    """Every error type and message along a failed update's cause chain (validator message, Activity type)."""
    codes: set[str] = set()
    seen: BaseException | None = err
    while seen is not None:
        codes.add(type(seen).__name__)
        codes.add(str(getattr(seen, "message", "") or ""))
        if getattr(seen, "type", None):
            codes.add(seen.type)
        seen = seen.__cause__ or getattr(seen, "cause", None)
    return codes


async def _answered_view(ws: str, task: dict, qid: str, viewer: str) -> dict[str, Any]:
    tree = await tasks_repo.get_task_tree(ws, task["taskId"])
    tree["effects"] = await effects_repo.effects_of_task(ws, task["taskId"])
    q = next(q for q in tree["questions"] if q["qid"] == qid)
    [view] = await _views([q], viewer)
    return {"question": view, "next": tasks_access.summarize(tree, viewer)}


_NOT_A_PERSON = frozenset({SYSTEM_ACTOR_ID, "watch"})  # the task ended it (resolve, failure, watchdog, watch)


def _not_open(q: dict) -> EngineError:
    """409 with who closed it. Cancelled: `reason` (stopped, resolved, quiet, lost_run, watch_gave_up, ...) for the
    client's copy, and `by` only when a person did it ("This thread ended", never a system id as a name)."""
    if q["state"] == "cancelled":
        by = q.get("cancelledBy")
        return _problem(
            409,
            "not_open",
            state="cancelled",
            by=None if by in _NOT_A_PERSON else by,
            at=q.get("cancelledAt"),
            reason=q.get("reason"),
        )
    return _problem(409, "not_open", state=q["state"], by=q.get("answeredBy"), at=q.get("answeredAt"))


async def _submit_answer(
    ws: str, task_id: str, qid: str, me: str, body: Any, *, member: bool = False
) -> dict[str, Any]:
    """`me` answers Question `qid`: the card's press, and a line said to the voice right after it said the question
    (`_answer_said_question`). `body`: `option_id`, `text`, `client_answer_id`, `expect_version`. `member`: `me` is
    the tenant's member on the caller's word. Raises the answer route's refusals as EngineError."""
    task = await tasks_repo.get_task(ws, task_id)
    if task is None or not await tasks_access.can_view(task, me, member=member):
        raise _not_found()
    q = await questions_repo.get_question(ws, task_id, qid)
    if q is None:
        raise _not_found()
    if q["state"] != "open":
        if q["state"] == "answered" and q.get("clientAnswerId") == body.client_answer_id and q.get("answeredBy") == me:
            await _remember(ws, q, body, me)  # finishes a write a failed first press left
            return await _answered_view(ws, task, qid, me)  # the same press again
        raise _not_open(q)
    if task.get("status") in _CLOSED:
        # A closed task's Questions are cancelled when it ends; one left open (that cancel failed) is still never
        # answered, and no Temporal update is sent (it would start a fresh run for a finished task).
        raise _problem(409, "task_closed")
    if not await questions.can_answer_now(ws, q, me, member=member):
        raise _problem(403, "not_answerer")
    offered = {o["id"] for o in q.get("options") or []} or {"answer"}
    if body.option_id not in offered:
        raise _problem(400, "invalid_option")
    trigger_id = None
    if body.text:
        # The text rides on a Trigger row; the Temporal update carries ids only.
        trigger_id = ts.trigger_id("answer", qid, body.client_answer_id)
        await tasks_repo.put_trigger(
            ws,
            {
                "triggerId": trigger_id,
                "source": "answer",
                "ref": {"taskId": task_id, "qid": qid, "optionId": body.option_id, "text": body.text},
                "admission": "respond",
                "lane": "shared",
                "actorUserId": me,
                "receivedAt": utc_now_iso(),
            },
        )
    ref = AnswerRef(qid, body.option_id, trigger_id, me, body.client_answer_id, body.expect_version)
    try:
        await temporal_client.update_task(ws, task_id, "answer", ref)
    except temporal_client.TemporalUnavailable as err:
        raise _problem(503, "TEMPORAL_UNAVAILABLE", retryable=True) from err
    except Exception as err:  # noqa: BLE001 -- an update failure: the row says what happened
        codes = _failure_codes(err)
        now = await questions_repo.get_question(ws, task_id, qid)
        if now and now["state"] == "answered" and now.get("clientAnswerId") == body.client_answer_id:
            await _remember(ws, q, body, me)
            return await _answered_view(ws, task, qid, me)
        if now and now["state"] != "open":
            raise _not_open(now) from err
        if "task_closed" in codes or (await tasks_repo.get_task(ws, task_id) or {}).get("status") in _CLOSED:
            raise _problem(409, "task_closed") from err
        if {"not_answerer", "NotAnswerer"} & codes:
            raise _problem(403, "not_answerer") from err
        if "InvalidOption" in codes:
            raise _problem(400, "invalid_option") from err
        if "VersionConflict" in codes:
            raise _problem(409, "stale_version") from err
        raise _problem(502, "answer_failed") from err
    await _remember(ws, q, body, me)
    return await _answered_view(ws, task, qid, me)


async def _remember(ws: str, q: dict, body: Any, me: str) -> None:
    """A Question that names a conversation (only a phone call's does: calls.call_ask) answered: its product hears
    it. Every other Question tells the products nothing."""
    if not (q.get("preview") or {}).get("roomId"):
        return
    answer = {"optionId": body.option_id, "text": body.text}
    await hooks.event(EngineEvent(type="question.answered", tenant_id=ws, question=q, answer=answer, actor_user_id=me))


# --- act -----------------------------------------------------------------------------------------------------------

_GATED = frozenset({"retry"})  # a new attempt: today's chat gate first (§14.10). Reopen runs nothing.


async def act(req: ActIn) -> dict:
    """Stop, resolve, reopen or retry a task as `principal`, at `version` when given: the task's summary after."""
    member = req.principal.kind == "member"
    return await _act(req.tenant_id, req.task_id, req.action, req.principal.id, req.version, member=member)


async def _visible_tree(ws: str, task_id: str, user_id: str, *, member: bool = False) -> dict:
    tree = await tasks_repo.get_task_tree(ws, task_id)
    if tree["task"] is None:
        raise _problem(404, "not_found")
    if not await tasks_access.can_view(tree["task"], user_id, member=member):
        raise _problem(404, "not_found")  # hidden reads as missing: no existence leak (lead ruling, 5 Nov)
    return tree


async def _summary(tree: dict, viewer: str) -> dict:
    ids = list(
        dict.fromkeys(u for q in tree["questions"] if q.get("state") == "open" for u in q.get("answerers") or [])
    )
    names = dict(zip(ids, await asyncio.gather(*(human_display_name(u) for u in ids))))
    return tasks_access.summarize({**tree, "userNames": names}, viewer)


async def _act(ws: str, task_id: str, action: str, user_id: str, version: int | None, *, member: bool = False) -> dict:
    tree = await _visible_tree(ws, task_id, user_id, member=member)
    task = tree["task"]
    if version is not None and task["version"] != version:
        raise _problem(409, "version_conflict", version=task["version"])
    if action in {"retry", "reopen"}:
        trig = await tasks_repo.get_trigger(ws, task["triggerId"]) if task.get("triggerId") else None
        if await admission.paused_room(ts.trigger_room(trig or {}) or (task.get("audience") or {}).get("roomId")):
            raise _problem(409, "project_paused", message=messaging.PAUSED_ROOM_TEXT)
    if action in _GATED:
        gate = await chat_gate.gate_chat(user_id=user_id, owner_id=ws, session_key=f"task:{task_id}")
        if gate.get("blocked"):
            raise _problem(402, gate.get("code") or "blocked", message=gate.get("message"))
    try:
        if action == "stop":
            await temporal_client.update_task(ws, task_id, "stop", StopRequest(by_user_id=user_id))
        else:
            await temporal_client.update_task(ws, task_id, action, ActionRequest(user_id, expected_version=version))
    except WorkflowUpdateFailedError as exc:  # the workflow's validator refused this state
        raise _problem(409, "illegal_state") from exc
    except (temporal_client.TemporalUnavailable, RPCError) as exc:
        raise _problem(503, "TEMPORAL_UNAVAILABLE", retryable=True) from exc
    await _mark_action(ws, task, action, user_id)
    return await _summary(await tasks_repo.get_task_tree(ws, task_id), user_id)


_ACTION_MARKS = {"resolve": "Closed", "stop": "Stopped", "reopen": "Reopened"}


async def _mark_action(ws: str, task: dict, action: str, user_id: str) -> None:
    """The task room's marker for a person's Close, Stop or Reopen, by name ("Closed by Sam"); the workflow's own
    status changes are marked by `activities._announce`. Keyed on the version acted on: one line per press."""
    mark = _ACTION_MARKS.get(action)
    if not mark:
        return
    try:
        await task_conversation.ensure_task_room(ws, task)
        await task_conversation.post_as_lightbulb(
            ws,
            ts.task_room_id(task["taskId"]),
            text=f"{mark} by {await human_display_name(user_id)}",
            event_id=f"task-mark:{task['taskId']}:{action}:{task['version']}",
            kind="marker",
            progress=True,
            remember=False,
        )
    except Exception:  # noqa: BLE001 -- the action happened; its line is cosmetic
        logger.warning("action marker not posted task=%s action=%s", task["taskId"], action, exc_info=True)


# --- warm ----------------------------------------------------------------------------------------------------------

_background: set[asyncio.Task] = set()


def _fire(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def warm(tenant_id: str, agent_ids: list[str], conversation_id: str | None = None) -> None:
    """Fire-and-forget wake of the workspace runtime (one health RPC), then a profile pull for each agent (the
    bridge's profile.warm, off its request thread), so the first summon finds both warm. Pulse's first voice answer
    waited 23 s on its cold profile restore alone.

    With `conversation_id` the bridge also compacts each agent's session for that room if it is near Hermes's
    threshold: a spoken turn never compacts (67-103 s inside a turn, prod 10-07), so the start of the call is where
    it happens. The RPC answers at once either way."""

    async def wake():
        try:
            if conversation_id:
                await _owned(tenant_id, conversation_id)
            await container_rpc.call(tenant_id, "health")
        except Exception:  # noqa: BLE001 -- best effort (a foreign room included); the first turn wakes it otherwise
            logger.info("voice owner prewake failed", exc_info=True)
            return
        for agent_id in agent_ids:
            try:
                params = {"agentId": agent_id, **({"roomId": conversation_id} if conversation_id else {})}
                await container_rpc.call(tenant_id, "profile.warm", params, timeout_s=10.0)
            except Exception:  # noqa: BLE001 -- an old image answers "unavailable"; the summon restores
                logger.info("voice profile prewarm failed agent=%s", agent_id, exc_info=True)

    _fire(wake())
