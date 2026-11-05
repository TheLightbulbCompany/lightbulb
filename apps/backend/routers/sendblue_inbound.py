"""Sendblue inbound webhook: a customer texts a business's line; store it in the workspace's
mail inbox as a text and wake the business's front-desk agent (the text twin of ses_inbound.py).

Transient failures (a delivery ``error``, any unexpected exception) roll the dedup row back and
answer 503 so Sendblue retries; every deterministic drop stays 200.
"""

import hmac
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.agent_email_repo import get_by_address
from core.repositories.agent_mail_inbox_repo import find_by_message_id, put_message, put_sent, reply_key
from core.repositories.container_repo import try_consume_stranger_wake
from core.services.agent_email import mail_changed, team_address
from core.services.agent_mail_delivery import deliver_to_agent
from core.services.agent_mail_inbound import stranger_key
from core.services.text_inbound import (
    TEXT_WAKES_PER_OWNER_PER_DAY,
    TEXT_WAKES_PER_SENDER_PER_DAY,
    build_delivered_text,
    business_name,
    paused_autoreply,
    text_session_key,
)
from core.services.sendblue import SendblueError, enabled, send_text, send_typing
from core.services.trial_cap import trial_block_reason
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()


class SendblueInbound(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_handle: str = ""
    content: str = ""
    from_number: str = ""
    number: str = ""
    to_number: str = ""
    sendblue_number: str = ""
    is_outbound: bool = False
    status: str = ""
    service: str = ""
    media_url: str | None = None
    message_type: str = "message"
    opted_out: bool = False
    date_sent: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _null_is_default(cls, v, info):
        return cls.model_fields[info.field_name].default if v is None else v


def _final(status: str, *, code: int = 200):
    try:
        put_metric("text.inbound", dimensions={"result": status})
    except Exception:  # noqa: BLE001 — telemetry must never break ingestion
        logger.debug("text.inbound metric emission failed", exc_info=True)
    if code == 200:
        return {"status": status}
    return JSONResponse(status_code=code, content={"status": status})


async def _rollback_dedup(event_id: str) -> None:
    try:
        await delete_event(event_id)
    except Exception:  # noqa: BLE001 — a failed rollback must not change the 503
        logger.warning("sendblue_inbound: dedup rollback failed for %s", event_id, exc_info=True)


@router.post("/sendblue-inbound", include_in_schema=False)
async def handle_sendblue_inbound(body: SendblueInbound, request: Request):
    event_id = f"sendblue:{body.message_handle}"
    recorded = False
    try:
        if not enabled():
            return _final("disabled")
        # ponytail: Sendblue's docs do not name the secret header; pin it from a captured dev request (Task 10).
        secret = settings.SENDBLUE_WEBHOOK_SECRET.encode()
        if not any(hmac.compare_digest(v.encode(), secret) for v in request.headers.values()):
            return _final("bad_secret", code=401)
        if body.is_outbound:
            return _final("ignored_outbound")
        if body.message_type != "message":
            return _final("ignored_group")
        if not body.message_handle:
            return _final("dropped_no_handle")

        if await record_event_or_skip(event_id, source="sendblue_inbound") is WebhookDedupResult.ALREADY_SEEN:
            return _final("duplicate")
        recorded = True

        line = await get_by_address(body.to_number or body.sendblue_number)
        if not line or line.get("kind") != "text":
            return _final("dropped_unknown_line")
        owner_id, agent_id = line["owner_id"], line["agent_id"]
        customer = body.from_number or body.number
        if not customer:
            return _final("dropped_no_sender")
        address = await team_address(owner_id)
        if not address:
            return _final("dropped_no_address")

        stored = await find_by_message_id(address, body.message_handle)
        if not stored:
            stored = await put_message(
                address=address,
                owner_id=owner_id,
                agent_id=agent_id,
                message_id=body.message_handle,
                from_addr=customer,
                subject="",
                text=body.content,
                headers={},
                attachments=[],
                channel="text",
            )
            await mail_changed(owner_id)

        if customer in (line.get("human_threads") or {}):
            return _final("stored_human_thread")

        if await trial_block_reason(owner_id):
            if not await try_consume_stranger_wake(
                owner_id,
                sender_key=stranger_key(customer),
                day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                owner_cap=TEXT_WAKES_PER_OWNER_PER_DAY,
                sender_cap=1,
                attr_prefix="text_paused",
            ):
                return _final("stored_capped_quiet")
            try:
                reply = paused_autoreply(await business_name(owner_id), line.get("business_phone"))
                await send_text(from_number=line["address"], to_number=customer, content=reply)
                await put_sent(
                    address=address,
                    message_id=f"sendblue-paused:{body.message_handle}",
                    owner_id=owner_id,
                    agent_id=agent_id,
                    to=customer,
                    subject="",
                    text=reply,
                    channel="text",
                )
            except (SendblueError, httpx.HTTPError):
                return _final("dropped_over_cap")
            return _final("capped_notified")

        if not await try_consume_stranger_wake(
            owner_id,
            sender_key=stranger_key(customer),
            day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            owner_cap=TEXT_WAKES_PER_OWNER_PER_DAY,
            sender_cap=TEXT_WAKES_PER_SENDER_PER_DAY,
            attr_prefix="text_wake",
        ):
            return _final("stored_not_woken_rate_limited")

        await send_typing(from_number=line["address"], to_number=customer)

        result = await deliver_to_agent(
            owner_id=owner_id,
            agent_id=agent_id,
            message=build_delivered_text(
                from_number=customer,
                service=body.service,
                text=body.content,
                reply_key=reply_key(address, stored["received_key"]),
                media_url=body.media_url,
            ),
            idempotency_key=event_id,
            session_key=text_session_key(customer),
            # Super-agent workspaces: how the Router's reply gets back to this customer (lane A, A9).
            source="text",
            reply_ref={"provider": "sendblue", "address": address, "fromNumber": line["address"], "toNumber": customer},
        )
        if result == "error":
            await _rollback_dedup(event_id)
            return _final(result, code=503)
        return _final(result)
    except Exception:  # noqa: BLE001 — see module docstring.
        logger.exception("sendblue_inbound handler failed unexpectedly")
        if recorded:
            await _rollback_dedup(event_id)
        return _final("error", code=503)
