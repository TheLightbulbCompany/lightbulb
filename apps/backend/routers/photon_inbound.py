"""Photon inbound webhook: a customer iMessages a business's line; store it in the workspace's
mail inbox as a text and wake the business's front-desk agent.

The request is a Standard Webhook: verified over the RAW body before anything is parsed. Transient
failures (a delivery ``error``, any unexpected exception) roll the dedup row back and answer 503 so
Photon retries; every deterministic drop stays 200.
"""

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from core.auth import AuthContext
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.agent_email_repo import bind_member_phone, get_by_address
from core.repositories.agent_mail_inbox_repo import (
    find_by_message_id,
    find_sent_by_message_id,
    mark_sent_failed,
    put_message,
    put_sent,
    reply_key,
)
from core.repositories.container_repo import release_stranger_wake, try_consume_stranger_wake
from core.services.agent_email import mail_changed, team_address
from core.services import messaging, standard_webhooks
from core.services.agent_mail_delivery import deliver_to_agent
from core.services.agent_mail_inbound import stranger_key
from core.services.photon import PhotonError, enabled, mark_read, send_text, send_typing
from core.services.text_inbound import (
    TEXT_WAKES_PER_OWNER_PER_DAY,
    TEXT_WAKES_PER_SENDER_PER_DAY,
    build_delivered_text,
    business_name,
    paused_autoreply,
    text_session_key,
)
from core.services.trial_cap import trial_block_reason
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()


class _Party(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    handle: str | None = None
    details: dict | None = None


class _Space(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    participantIds: list[str] | None = None  # noqa: N815 — Photon's wire name


class _Content(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    text: str | None = None
    attachmentId: str | None = None  # noqa: N815


class _Object(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    platform: str | None = None
    status: str | None = None
    space: _Space | None = None
    sentBy: _Party | None = None  # noqa: N815
    receivedBy: _Party | None = None  # noqa: N815
    addressedTo: _Party | None = None  # noqa: N815
    content: _Content | None = None
    timestamps: dict | None = None  # receivedAt: "preserved across replay" (spec), so a redelivery keys the same row
    failure: dict | None = None  # on message.failed: why a send Photon had accepted did not deliver


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")

    projectId: str | None = None  # noqa: N815
    object: _Object


class PhotonEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    type: str | None = None
    data: _Data


def _final(status: str, *, code: int = 200):
    try:
        put_metric("text.inbound", dimensions={"result": status})
    except Exception:  # noqa: BLE001 — telemetry must never break ingestion
        logger.debug("text.inbound metric emission failed", exc_info=True)
    return JSONResponse(status_code=code, content={"status": status})


LINKED_REPLY = "Linked. Texts from this phone now reach the team as you."
_LINK_RE = re.compile(r"link\s+(\d{6})", re.IGNORECASE)


async def _link_by_code(
    line: dict, sender: str, text: str, line_user: str | None, space_id: str | None, obj_id: str
) -> bool:
    """A member links their phone by texting ``LINK <code>`` (the code the Mail panel showed them); the text is
    the proof. True when this text linked ``sender``; the code text is not stored. The LINK word keeps a
    customer's six-digit order number from ever matching, and one live code per member (the route prunes the
    member's earlier ones) keeps a guess at one in a million per text."""
    match = _LINK_RE.fullmatch(text)
    if not match:
        return False
    code = match.group(1)
    entry = (line.get("phone_codes") or {}).get(code)
    now = int(time.time())
    if not entry or int(entry.get("exp", 0)) <= now:
        return False
    user_id = entry["user_id"]
    drop = [p for p, uid in (line.get("member_phones") or {}).items() if uid == user_id and p != sender]
    if not await bind_member_phone(line["address"], code=code, phone=sender, user_id=user_id, now=now, drop=drop):
        return False  # used or expired meanwhile: a customer's text
    try:
        await send_text(sender_id=line_user, space_id=space_id, text=LINKED_REPLY, idempotency_key=f"link:{obj_id}")
    except (PhotonError, httpx.HTTPError):
        logger.warning("photon_inbound: could not confirm the link for %s", obj_id, exc_info=True)
    return True


async def _sent_failed(obj: _Object) -> str:
    """Photon accepted a send (202) and later could not deliver it: the sent row says so, and the Mail panel
    refreshes. The agent is not woken for it: a number that never delivers would wake it on every retry."""
    row = await find_sent_by_message_id(obj.id) if obj.id else None
    if not row:
        return "ignored_event"
    failure = obj.failure or {}
    reason = str(failure.get("code") or failure.get("reason") or failure.get("message") or "failed")
    await mark_sent_failed(row["address"], row["received_key"], reason)
    await mail_changed(row["owner_id"])
    put_metric("text.send", dimensions={"result": "failed_later"})
    return "send_failed_later"


async def _rollback_dedup(event_id: str) -> None:
    try:
        await delete_event(event_id)
    except Exception:  # noqa: BLE001 — a failed rollback must not change the 503
        logger.warning("photon_inbound: dedup rollback failed for %s", event_id, exc_info=True)


@router.post("/photon-inbound", include_in_schema=False)
async def handle_photon_inbound(request: Request):
    event_id = ""
    recorded = False
    try:
        if not enabled():
            return _final("disabled")
        raw = await request.body()
        bad = standard_webhooks.verify(request.headers, raw, settings.PHOTON_WEBHOOK_SECRET)
        if bad:
            # Fail closed. The header names are pinned at the first real delivery (plan 4 Task 10); a refusal
            # logs the names Photon actually sent, never their values, so that pin is one log read.
            names = sorted(request.headers.keys())
            logger.warning("photon_inbound: signature refused (%s); header names: %s", bad, names)
            return _final("stale_timestamp" if bad == "stale" else "bad_signature", code=401)
        try:
            event = PhotonEvent.model_validate_json(raw)
        except ValidationError:
            return _final("bad_body", code=422)

        obj = event.data.object
        content = obj.content or _Content()
        if event.type == "message.failed" and obj.id:
            event_id = f"photon-failed:{obj.id}"
            if await record_event_or_skip(event_id, source="photon_inbound") is WebhookDedupResult.ALREADY_SEEN:
                return _final("duplicate")
            recorded = True
            return _final(await _sent_failed(obj))
        if (
            event.type != "message.received"
            or obj.platform != "imessage"
            or obj.status != "received"
            or content.type not in ("text", "attachment")
            or not obj.id
        ):
            return _final("ignored_event")

        event_id = f"photon:{obj.id}"
        if await record_event_or_skip(event_id, source="photon_inbound") is WebhookDedupResult.ALREADY_SEEN:
            return _final("duplicate")
        recorded = True

        # Both name OUR line (spec: receivedBy, addressedTo) and either may be absent; its handle is the number.
        received_by = obj.receivedBy or obj.addressedTo or _Party()
        sent_by, space = obj.sentBy or _Party(), obj.space or _Space()
        line = await get_by_address(received_by.handle) if received_by.handle else None
        if not line or line.get("kind") != "text":
            return _final("dropped_unknown_line")
        owner_id, agent_id = line["owner_id"], line["agent_id"]
        line_user = received_by.id or line.get("photon_user_id")
        customer = sent_by.handle or (f"photon:{sent_by.id}" if sent_by.id else "")
        if not customer:
            return _final("dropped_no_sender")
        member_id = (line.get("member_phones") or {}).get(customer)
        address = await team_address(owner_id)
        if not address:
            return _final("dropped_no_address")

        text = (content.text or "") if content.type == "text" else f"[Attachment: {content.attachmentId}]"
        if not member_id and await _link_by_code(line, customer, text.strip(), line_user, space.id, obj.id):
            return _final("member_linked")
        stored = await find_by_message_id(address, obj.id)
        if not stored:
            stored = await put_message(
                address=address,
                owner_id=owner_id,
                agent_id=agent_id,
                message_id=obj.id,
                from_addr=customer,
                subject="",
                text=text,
                headers={},
                attachments=[],
                # Photon's own receipt time, not our clock: the by-message-id index behind find_by_message_id is
                # eventually consistent, and a redelivery after a 503 must land on the SAME row and reply key.
                received_at=(obj.timestamps or {}).get("receivedAt"),
                channel="text",
                provider_ids={
                    "space_id": space.id,
                    "sender_user_id": sent_by.id,
                    "line_user_id": line_user,
                    "message_id": obj.id,
                },
            )
            await mail_changed(owner_id)

        if member_id:
            # A linked member is a teammate, not a customer: their text is a room message as them,
            # so listen modes and Jev pick the responder.
            key = reply_key(address, stored["received_key"])
            try:
                await messaging.create_human_message(
                    AuthContext(user_id=member_id, org_id=owner_id),
                    messaging.default_room_id(owner_id),
                    messaging.CreateMessageRequest(
                        client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-text|{obj.id}"),
                        # The backend texts an agent's reply back (core/services/text_relay.py): no send step here.
                        text=f"{text}\n\n(sent by text from {customer}; a reply here is texted back, so keep it short)",
                        reply_to_message_id=None,
                        thread_root_message_id=None,
                    ),
                    via="text",
                    text_reply_key=key,
                )
                if line_user and space.id:
                    await send_typing(sender_id=line_user, space_id=space.id)
            except (messaging.ApiProblem, HTTPException) as e:
                if e.status_code not in (403, 404):
                    raise  # transient: the outer handler rolls the dedup back and answers 503
                # Refused (403: not in the main room, or not in the workspace; 404: no main room): the text is
                # a customer's this time. The link stays: the membership webhook unlinks a member who left,
                # and a member only taken out of the main room is still a member.
                logger.warning("photon_inbound: member post refused for %s", obj.id, exc_info=True)
            else:
                return _final("member_routed")

        if customer in (line.get("human_threads") or {}):
            return _final("stored_human_thread")

        # A paused workspace answers each sender once a day with the paused reply; a live one wakes the agent.
        paused = bool(await trial_block_reason(owner_id))
        sender_key, day = stranger_key(customer), datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not await try_consume_stranger_wake(
            owner_id,
            sender_key=sender_key,
            day=day,
            owner_cap=TEXT_WAKES_PER_OWNER_PER_DAY,
            sender_cap=1 if paused else TEXT_WAKES_PER_SENDER_PER_DAY,
            attr_prefix="text_paused" if paused else "text_wake",
        ):
            return _final("stored_capped_quiet" if paused else "stored_not_woken_rate_limited")

        if paused:
            try:
                reply = paused_autoreply(await business_name(owner_id), line.get("business_phone"))
                resp = await send_text(
                    sender_id=line_user, space_id=space.id, text=reply, idempotency_key=f"paused:{obj.id}"
                )
                await put_sent(
                    address=address,
                    message_id=resp.get("id") or f"photon-paused:{obj.id}",
                    owner_id=owner_id,
                    agent_id=agent_id,
                    to=customer,
                    subject="",
                    text=reply,
                    channel="text",
                )
            except (PhotonError, httpx.HTTPError):
                # Transient: the sender's slot goes back and the dedup row with it, so Photon's redelivery
                # sends the promised reply. A 200 here spent the sender's one reply a day on nothing.
                await release_stranger_wake(owner_id, sender_key=sender_key, day=day, attr_prefix="text_paused")
                await _rollback_dedup(event_id)
                return _final("paused_reply_failed", code=503)
            return _final("capped_notified")

        if line_user:
            await asyncio.gather(send_typing(sender_id=line_user, space_id=space.id), mark_read(obj.id))

        result = await deliver_to_agent(
            owner_id=owner_id,
            agent_id=agent_id,
            message=build_delivered_text(
                from_number=customer,
                service="iMessage",
                text=text,
                reply_key=reply_key(address, stored["received_key"]),
                media_url=None,
            ),
            idempotency_key=event_id,
            session_key=text_session_key(customer),
            # Super-agent workspaces: how the Router's reply gets back to this customer (lane A, A9).
            source="text",
            reply_ref={
                "provider": "photon",
                "address": address,
                "senderId": line_user,
                "spaceId": space.id,
                "to": customer,
            },
        )
        if result == "error":
            await _rollback_dedup(event_id)
            return _final(result, code=503)
        return _final(result)
    except Exception:  # noqa: BLE001 — see module docstring.
        logger.exception("photon_inbound handler failed unexpectedly")
        if recorded:
            await _rollback_dedup(event_id)
        return _final("error", code=503)
