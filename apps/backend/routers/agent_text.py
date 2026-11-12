"""Text replies: an agent (hook token) or a person in the Mail panel (Clerk) answers a customer's text.

The backend sends a text only as a reply to a stored inbound text row, always from the workspace's
line and always as the line's agent.
"""

import hashlib
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.observability.metrics import put_metric
from core.services import effect_doors, text_lines
from core.repositories import billing_repo
from core.repositories.agent_email_repo import (
    drop_member_phones,
    pending_line_for_owner,
    put_phone_code,
    set_business_phone,
    set_disclosed,
    set_thread_mode,
    text_line_for_owner,
)
from core.repositories.agent_mail_inbox_repo import (
    get_message,
    parse_reply_key,
    put_sent,
)
from core.services.agent_email import mail_changed
from core.services import photon
from core.services.text_inbound import business_name
from core.services.webhook_dedup import WebhookDedupResult, record_event_or_skip
from routers.agent_mail import _require_hook_token

logger = logging.getLogger(__name__)

router = APIRouter()

DISCLOSURE = "(Automated assistant for {business})"


def _valid_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"\+[1-9]\d{6,14}", phone))


async def _line_or_404(owner_id: str) -> dict:
    line = await text_line_for_owner(owner_id)
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    return line


async def _text_row(owner_id: str, key: str) -> tuple[str, dict] | None:
    parsed = parse_reply_key(key)
    row = await get_message(*parsed) if parsed else None
    if not row or row.get("owner_id") != owner_id or row.get("channel") != "text" or row.get("direction"):
        return None
    return parsed[0], row


async def send_reply(
    *, owner_id: str, reply_key: str, text: str, by_human: bool = False, agent_id: str | None = None
) -> str:
    if not photon.enabled():
        return "disabled"
    found = await _text_row(owner_id, reply_key)
    if not found:
        return "bad_reply_key"
    address, row = found
    line = await text_line_for_owner(owner_id)
    if not line:
        return "no_line"
    # Every agent in the workspace holds the hook token; only the line's own agent texts customers as it.
    if agent_id is not None and agent_id != line["agent_id"]:
        return "not_front_desk"
    provider = row.get("provider") or {}
    space_id = provider.get("space_id")
    # The line's own Photon user is authoritative; the id stored with the inbound row is only a fallback.
    sender_id = line.get("photon_user_id") or provider.get("line_user_id")
    if not space_id or not sender_id:
        return "bad_reply_key"
    customer = row["from_addr"]
    if not by_human and customer in (line.get("human_threads") or {}):
        return "human_thread"
    # The agent's first reply to a customer says it is automated; the line row records that it did.
    disclose = (
        not by_human
        and customer not in (line.get("member_phones") or {})
        and customer not in (line.get("disclosed") or {})
    )
    # Keyed on the reply as written and on who sends it: a retry after the disclosure was recorded carries the
    # same key and replays the first send instead of texting the customer again without the prefix, while a
    # person who takes over and types the same words as the agent did is a new send, not a replay of the agent's.
    sender = "human" if by_human else "agent"
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    idempotency_key = f"text-reply:{row['received_key']}:{sender}:{digest}"
    if disclose:
        text = f"{DISCLOSURE.format(business=await business_name(owner_id))} {text}"
    try:
        resp = await photon.send_text(
            sender_id=sender_id, space_id=space_id, text=text, idempotency_key=idempotency_key
        )
    except (photon.PhotonError, httpx.HTTPError):
        put_metric("text.send", dimensions={"result": "failed"})
        return "send_failed"
    put_metric("text.send", dimensions={"result": "sent"})
    if disclose:
        try:
            await set_disclosed(line["address"], customer)
        except Exception:  # noqa: BLE001 -- the text has gone; a missed mark only repeats the disclosure
            logger.warning("Could not record the disclosure to %s on line %s", customer, line["address"], exc_info=True)
    sent_id = resp.get("id")
    # A retried send is Photon's replay of the first (same idempotency key, same message id): record it once,
    # on a conditional write keyed by that id, so two overlapping retries cannot both record it.
    first = not sent_id or (
        await record_event_or_skip(f"photon-sent:{sent_id}", source="agent_text") is WebhookDedupResult.RECORDED
    )
    if first:
        await put_sent(
            address=address,
            message_id=sent_id or f"photon-reply:{uuid.uuid4().hex}",
            owner_id=owner_id,
            agent_id=line["agent_id"],
            to=customer,
            subject="",
            text=text,
            channel="text",
        )
    await mail_changed(owner_id)
    if by_human:
        await set_thread_mode(line["address"], customer, "human")
    return "sent"


class AgentSend(BaseModel):
    agent_id: str
    reply_key: str
    text: str


class BusinessPhone(BaseModel):
    agent_id: str
    phone: str


@router.post("/u/{owner_id}/send")
async def hook_send(owner_id: str, body: AgentSend, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    try:  # with the switch on, a text must be one a person allowed (Lane Q door gating); off is today's path
        await effect_doors.require_intent(
            owner_id,
            "text.send",
            {"agentId": body.agent_id, "replyKey": body.reply_key, "body": body.text.rstrip("\n")},
        )
    except effect_doors.DoorRefused as exc:
        raise HTTPException(status_code=403, detail={"status": exc.code, "message": str(exc)}) from exc
    status = await send_reply(owner_id=owner_id, reply_key=body.reply_key, text=body.text, agent_id=body.agent_id)
    return {"status": status}


@router.post("/u/{owner_id}/business-phone")
async def hook_business_phone(owner_id: str, body: BusinessPhone, request: Request):
    await _require_hook_token(owner_id, request)
    if not _valid_phone(body.phone):
        return JSONResponse(status_code=400, content={"status": "bad_phone"})
    line = await _line_or_404(owner_id)
    if body.agent_id != line["agent_id"]:
        raise HTTPException(status_code=403, detail="not_front_desk")
    await set_business_phone(line["address"], body.phone)
    return {"status": "ok"}


class Reply(BaseModel):
    key: str
    text: str


class Thread(BaseModel):
    key: str
    mode: Literal["human", "agent"]


async def _recheck(owner_id: str) -> str:
    """A failed or cancelled operation found on a read: ask Stripe, through the subscription, whether a line is
    still owed. Never ``ensure_line`` directly, so a line cancelled on purpose is not bought back."""
    try:
        sub_id = (await billing_repo.get_by_owner_id(owner_id) or {}).get("stripe_subscription_id")
        if not sub_id:
            return "absent"
        again = await text_lines.sync_front_desk(owner_id, subscription_id=sub_id)
    except Exception:
        logger.warning("Re-checking the line failed for owner %s", owner_id, exc_info=True)
        return "absent"
    return again if again in ("provisioned", "pending") else "absent"


@router.get("/line")
async def get_line(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    line = await text_line_for_owner(owner_id)
    # A pending row beside a line is a surplus line whose release is not done: this read retries it.
    if not line or await pending_line_for_owner(owner_id):
        try:
            outcome = await text_lines.complete_pending(owner_id)
            if outcome == "failed":
                outcome = await _recheck(owner_id)
            if outcome == "provisioned":
                line = await text_line_for_owner(owner_id)
        except (photon.PhotonError, httpx.HTTPError):
            # Only reading the operation raises, and that means a pending row is there.
            logger.warning("Completing the pending line failed for owner %s", owner_id, exc_info=True)
            outcome = "pending"
    # No line reads as 404 (useTextLine maps it to null; a 202 body without `number` would read as a
    # line). Its status says whether the number is still being set up, so the panel polls only then.
    if not line:
        return JSONResponse(status_code=404, content={"status": "absent" if outcome == "absent" else "pending"})
    return {
        "number": line["address"],
        "agentId": line["agent_id"],
        "businessPhone": line.get("business_phone"),
        "humanThreads": list((line.get("human_threads") or {}).keys()),
        "myPhone": next((p for p, uid in (line.get("member_phones") or {}).items() if uid == auth.user_id), None),
    }


PHONE_CODE_TTL_S = 15 * 60


@router.post("/me/phone/code")
async def my_phone_code(auth: AuthContext = Depends(get_current_user)) -> dict:
    """A member links their phone by texting this code to the line: the inbound text is the proof."""
    line = await _line_or_404(resolve_owner_id(auth))
    now = int(time.time())
    expires_at = now + PHONE_CODE_TTL_S
    # One live code per member (a guess stays one in a million per text), and expired ones go with it.
    prune = [
        c
        for c, v in (line.get("phone_codes") or {}).items()
        if int(v.get("exp", 0)) <= now or v.get("user_id") == auth.user_id
    ]
    for _ in range(2):  # a live collision is one in a million; draw once more, then give up
        code = f"{secrets.randbelow(10**6):06d}"
        if await put_phone_code(line["address"], code, auth.user_id, expires_at, prune=prune):
            return {
                "code": code,
                "number": line["address"],
                "expiresAt": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            }
    raise HTTPException(status_code=503, detail="code_unavailable")


@router.delete("/me/phone")
async def delete_my_phone(auth: AuthContext = Depends(get_current_user)) -> dict:
    await drop_member_phones(await _line_or_404(resolve_owner_id(auth)), auth.user_id)
    return {"status": "ok"}


@router.post("/reply")
async def reply(body: Reply, auth: AuthContext = Depends(get_current_user)) -> dict:
    status = await send_reply(owner_id=resolve_owner_id(auth), reply_key=body.key, text=body.text, by_human=True)
    return {"status": status}


@router.post("/thread")
async def thread(body: Thread, auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    found = await _text_row(owner_id, body.key)
    if not found:
        raise HTTPException(status_code=404, detail="bad_reply_key")
    line = await _line_or_404(owner_id)
    await set_thread_mode(line["address"], found[1]["from_addr"], body.mode)
    return {"status": "ok"}
