"""Text replies: an agent (hook token) or a person in the Mail panel (Clerk) answers a customer's text.

The backend sends a text only as a reply to a stored inbound text row, always from the workspace's
line and always as the line's agent.
"""

import re
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.observability.metrics import put_metric
from core.repositories.agent_email_repo import set_business_phone, set_thread_mode, text_line_for_owner
from core.repositories.agent_mail_inbox_repo import get_message, has_sent_to, parse_reply_key, put_sent
from core.services.agent_email import mail_changed
from core.services.sendblue import SendblueError, enabled, send_text
from routers.agent_mail import _require_hook_token
from routers.sendblue_inbound import _business_name

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
    if not enabled():
        return "disabled"
    found = await _text_row(owner_id, reply_key)
    if not found:
        return "bad_reply_key"
    address, row = found
    line = await text_line_for_owner(owner_id)
    if not line:
        return "no_line"
    customer = row["from_addr"]
    if not by_human and customer in (line.get("human_threads") or {}):
        return "human_thread"
    if not by_human and not await has_sent_to(address, customer, text_prefix=DISCLOSURE_PREFIX):
        text = f"{DISCLOSURE.format(business=await _business_name(owner_id))} {text}"
    try:
        resp = await send_text(from_number=line["address"], to_number=customer, content=text)
    except (SendblueError, httpx.HTTPError):
        put_metric("text.send", dimensions={"result": "failed"})
        return "send_failed"
    put_metric("text.send", dimensions={"result": "sent"})
    await put_sent(
        address=address,
        message_id=resp.get("message_handle") or f"sendblue-reply:{uuid.uuid4().hex}",
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
    line = await text_line_for_owner(resolve_owner_id(auth))
    if not line:
        raise HTTPException(status_code=404, detail="no_line")
    return {
        "number": line["address"],
        "agentId": line["agent_id"],
        "businessPhone": line.get("business_phone"),
        "humanThreads": list((line.get("human_threads") or {}).keys()),
    }


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
