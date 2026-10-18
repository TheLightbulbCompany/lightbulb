"""Photon inbound webhook: a customer iMessages a business's line; store it in the workspace's
mail inbox as a text and wake the business's front-desk agent (the Photon twin of sendblue_inbound.py).

The request is a Standard Webhook: verified over the RAW body before anything is parsed. Transient
failures (a delivery ``error``, any unexpected exception) roll the dedup row back and answer 503 so
Photon retries; every deterministic drop stays 200.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.agent_email_repo import get_by_address, text_line_by_photon_user
from core.repositories.agent_mail_inbox_repo import find_by_message_id, put_message, put_sent, reply_key
from core.repositories.container_repo import try_consume_stranger_wake
from core.services.agent_email import mail_changed, team_address
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

# Standard Webhooks header names, pinned in one place (Task 0 confirms them from a real delivery).
HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"
MAX_CLOCK_SKEW_S = 300


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
    if code == 200:
        return {"status": status}
    return JSONResponse(status_code=code, content={"status": status})


def _verify(headers, raw: bytes) -> str | None:
    """None when the delivery is authentic and fresh, else the 401 status string."""
    wid, ts, sigs = headers.get(HEADER_ID), headers.get(HEADER_TIMESTAMP), headers.get(HEADER_SIGNATURE)
    if not (wid and ts and sigs):
        return "bad_signature"
    try:
        key = base64.b64decode(settings.PHOTON_WEBHOOK_SECRET.removeprefix("whsec_"))
        sent_at = int(ts)
    except (binascii.Error, ValueError):
        return "bad_signature"
    if abs(time.time() - sent_at) > MAX_CLOCK_SKEW_S:
        return "stale_timestamp"
    mac = hmac.new(key, f"{wid}.{ts}.".encode() + raw, hashlib.sha256).digest()
    expected = base64.b64encode(mac)
    for entry in sigs.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected):
            return None
    return "bad_signature"


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
        bad = _verify(request.headers, raw)
        if bad:
            return _final(bad, code=401)
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
        line = await get_by_address(received_by.handle) if received_by.handle else None
        if not line or line.get("kind") != "text":
            line = await text_line_by_photon_user(received_by.id) if received_by.id else None
        if not line:
            return _final("dropped_unknown_line")
        owner_id, agent_id = line["owner_id"], line["agent_id"]
        line_user = received_by.id or line.get("photon_user_id")
        customer = sent_by.handle or (f"photon:{sent_by.id}" if sent_by.id else "")
        if not customer:
            return _final("dropped_no_sender")
        # Task 11: member check goes here
        address = await team_address(owner_id)
        if not address:
            return _final("dropped_no_address")

        text = content.text or "" if content.type == "text" else f"[Attachment: {content.attachmentId}]"
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

        if customer in (line.get("human_threads") or {}):
            return _final("stored_human_thread")

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if await trial_block_reason(owner_id):
            if not await try_consume_stranger_wake(
                owner_id,
                sender_key=stranger_key(customer),
                day=day,
                owner_cap=TEXT_WAKES_PER_OWNER_PER_DAY,
                sender_cap=1,
                attr_prefix="text_paused",
            ):
                return _final("stored_capped_quiet")
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

        if not await try_consume_stranger_wake(
            owner_id,
            sender_key=stranger_key(customer),
            day=day,
            owner_cap=TEXT_WAKES_PER_OWNER_PER_DAY,
            sender_cap=TEXT_WAKES_PER_SENDER_PER_DAY,
            attr_prefix="text_wake",
        ):
            return _final("stored_not_woken_rate_limited")

        await send_typing(sender_id=line_user, space_id=space.id)
        await mark_read(obj.id)

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
