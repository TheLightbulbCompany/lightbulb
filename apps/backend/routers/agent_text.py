"""Text replies: an agent (hook token) or a person in the Mail panel (Clerk) answers a customer's text.

The backend sends a text only as a reply to a stored inbound text row, always from the workspace's
line and always as the line's agent.
"""

import hashlib
import logging
import re
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.observability.metrics import put_metric
from core.services import text_lines
from core.repositories.agent_email_repo import (
    set_business_phone,
    set_member_phone,
    set_thread_mode,
    text_line_for_owner,
)
from core.repositories.agent_mail_inbox_repo import get_message, has_sent_to, parse_reply_key, put_sent
from core.services.agent_email import mail_changed
from core.services import photon
from core.services.text_inbound import business_name
from routers.agent_mail import _require_hook_token

logger = logging.getLogger(__name__)

router = APIRouter()

DISCLOSURE_PREFIX = "(Automated assistant for"
DISCLOSURE = DISCLOSURE_PREFIX + " {business})"


async def _text_row(owner_id: str, key: str) -> tuple[str, dict] | None:
    parsed = parse_reply_key(key)
    row = await get_message(*parsed) if parsed else None
    if not row or row.get("owner_id") != owner_id or row.get("channel") != "text" or row.get("direction"):
        return None
    return parsed[0], row


async def send_reply(*, owner_id: str, reply_key: str, text: str, by_human: bool = False) -> str:
    if not photon.enabled():
        return "disabled"
    found = await _text_row(owner_id, reply_key)
    if not found:
        return "bad_reply_key"
    address, row = found
    line = await text_line_for_owner(owner_id)
    if not line:
        return "no_line"
    provider = row.get("provider") or {}
    space_id = provider.get("space_id")
    # The line's own Photon user is authoritative; the id stored with the inbound row is only a fallback.
    sender_id = line.get("photon_user_id") or provider.get("line_user_id")
    if not space_id or not sender_id:
        return "bad_reply_key"
    customer = row["from_addr"]
    if not by_human and customer in (line.get("human_threads") or {}):
        return "human_thread"
    if (
        not by_human
        and customer not in (line.get("member_phones") or {})
        and not await has_sent_to(address, customer, text_prefix=DISCLOSURE_PREFIX)
    ):
        text = f"{DISCLOSURE.format(business=await business_name(owner_id))} {text}"
    try:
        resp = await photon.send_text(
            sender_id=sender_id,
            space_id=space_id,
            text=text,
            idempotency_key=f"text-reply:{row['received_key']}:{hashlib.sha256(text.encode()).hexdigest()[:16]}",
        )
    except (photon.PhotonError, httpx.HTTPError):
        put_metric("text.send", dimensions={"result": "failed"})
        return "send_failed"
    put_metric("text.send", dimensions={"result": "sent"})
    await put_sent(
        address=address,
        message_id=resp.get("id") or f"photon-reply:{uuid.uuid4().hex}",
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
    phone: str


@router.post("/u/{owner_id}/send")
async def hook_send(owner_id: str, body: AgentSend, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    return {"status": await send_reply(owner_id=owner_id, reply_key=body.reply_key, text=body.text)}


@router.post("/u/{owner_id}/business-phone")
async def hook_business_phone(owner_id: str, body: BusinessPhone, request: Request):
    await _require_hook_token(owner_id, request)
    if not re.fullmatch(r"\+[1-9]\d{6,14}", body.phone):
        return JSONResponse(status_code=400, content={"status": "bad_phone"})
    line = await text_line_for_owner(owner_id)
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    await set_business_phone(line["address"], body.phone)
    return {"status": "ok"}


class Reply(BaseModel):
    key: str
    text: str


class Thread(BaseModel):
    key: str
    mode: Literal["human", "agent"]


@router.get("/line")
async def get_line(auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    line = await text_line_for_owner(owner_id)
    if not line:
        try:
            if await text_lines.complete_pending(owner_id) == "provisioned":
                line = await text_line_for_owner(owner_id)
        except (photon.PhotonError, httpx.HTTPError):
            logger.warning("Completing the pending line failed for owner %s", owner_id, exc_info=True)
    # A line Photon is still creating reads as 404 too: useTextLine maps 404 to null, and a 202 body
    # without `number` would read as a line.
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    return {
        "number": line["address"],
        "agentId": line["agent_id"],
        "businessPhone": line.get("business_phone"),
        "humanThreads": list((line.get("human_threads") or {}).keys()),
        "myPhone": next((p for p, uid in (line.get("member_phones") or {}).items() if uid == auth.user_id), None),
    }


@router.put("/me/phone")
async def put_my_phone(body: BusinessPhone, auth: AuthContext = Depends(get_current_user)):
    if not re.fullmatch(r"\+[1-9]\d{6,14}", body.phone):
        return JSONResponse(status_code=400, content={"status": "bad_phone"})
    line = await text_line_for_owner(resolve_owner_id(auth))
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    # One phone per member: drop any earlier one before saving the new.
    for old, uid in (line.get("member_phones") or {}).items():
        if uid == auth.user_id and old != body.phone:
            await set_member_phone(line["address"], old, None)
    await set_member_phone(line["address"], body.phone, auth.user_id)
    return {"status": "ok"}


@router.delete("/me/phone")
async def delete_my_phone(auth: AuthContext = Depends(get_current_user)) -> dict:
    line = await text_line_for_owner(resolve_owner_id(auth))
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    for phone, uid in (line.get("member_phones") or {}).items():
        if uid == auth.user_id:
            await set_member_phone(line["address"], phone, None)
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
    line = await text_line_for_owner(owner_id)
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    await set_thread_mode(line["address"], found[1]["from_addr"], body.mode)
    return {"status": "ok"}
