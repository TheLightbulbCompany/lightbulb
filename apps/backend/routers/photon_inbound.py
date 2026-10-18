"""Photon inbound webhook: a customer iMessages a business's line; store it in the workspace's
mail inbox as a text and wake the business's front-desk agent (the Photon twin of sendblue_inbound.py).

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
from core.repositories.agent_email_repo import bind_member_phone, get_by_address, set_member_phone
from core.repositories.agent_mail_inbox_repo import find_by_message_id, put_message, put_sent, reply_key
from core.repositories.container_repo import try_consume_stranger_wake
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
    content: _Content | None = None


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
            return _final("stale_timestamp" if bad == "stale" else "bad_signature", code=401)
        try:
            event = PhotonEvent.model_validate_json(raw)
        except ValidationError:
            return _final("bad_body", code=422)

        obj = event.data.object
        content = obj.content or _Content()
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

        received_by, sent_by, space = obj.receivedBy or _Party(), obj.sentBy or _Party(), obj.space or _Space()
        # Photon's iMessage user snapshot always carries a handle, so the line is found by its number.
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
                        text=f"{text}\n\n(sent by text from {customer}; to text back: "
                        f"/app/skills/lightbulb/scripts/lightbulb-text.sh send --reply {key})",
                        reply_to_message_id=None,
                        thread_root_message_id=None,
                    ),
                    via="text",
                )
            except (messaging.ApiProblem, HTTPException) as e:
                if e.status_code not in (403, 404):
                    raise  # transient: the outer handler rolls the dedup back and answers 503
                # Refused: the text is a customer's this time. A 403 means no longer a member, so the stale
                # link goes too; a 404 (the main room is missing) keeps a live member's link.
                logger.warning("photon_inbound: member post refused for %s", obj.id, exc_info=True)
                if e.status_code == 403:
                    try:
                        await set_member_phone(line["address"], customer, None)
                    except Exception:  # noqa: BLE001 — the next text retries the drop
                        logger.warning("photon_inbound: could not drop the link for %s", obj.id, exc_info=True)
            else:
                return _final("member_routed")

        if customer in (line.get("human_threads") or {}):
            return _final("stored_human_thread")

        # A paused workspace answers each sender once a day with the paused reply; a live one wakes the agent.
        paused = bool(await trial_block_reason(owner_id))
        if not await try_consume_stranger_wake(
            owner_id,
            sender_key=stranger_key(customer),
            day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
                return _final("dropped_over_cap")
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
