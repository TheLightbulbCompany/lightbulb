"""Agent mail — container routes (hook-token) + owner settings (Clerk).

Container routes live under ``/u/{owner_id}/…``: the owner is NAMED in the
path and the bearer must timing-safe-match THAT owner's ``containers.hook_token``
(the same model the hooks proxy uses) — a container can only ever act for its
own owner. Policy for WHO a message may go to lives in
``core/services/agent_mail_send.send``; this router only authenticates, rate
limits, validates shapes, and relays the result string.

The per-minute bucket below is burst smoothing ONLY — it is per-process, and
several backend tasks run behind the ALB, so it is not a real quota. The
durable fence is the per-owner daily cap enforced in ``agent_mail_send``.
"""

import hmac
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import agent_email_repo, container_repo
from core.repositories.agent_mail_inbox_repo import list_messages, reply_key
from core.services.agent_email import EMAIL_ADDRESS_RE, ensure_agent_address, resolve_from_address
from core.services.agent_mail_send import _owner_email, send
from core.services.rate_bucket import RateBucket

router = APIRouter()

RATE_LIMIT_PER_MINUTE = 60
_RATE = RateBucket(RATE_LIMIT_PER_MINUTE)
_INBOX_TEXT_CHARS = 8_000
_DOMAIN_RE = re.compile(r"^@[^@\s]+\.[^@\s]+$")


def _check_rate_limit(owner_id: str) -> bool:
    return _RATE.allow(owner_id)


async def _require_hook_token(owner_id: str, request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    presented = auth[len("Bearer ") :]
    container = await container_repo.get_by_owner_id(owner_id)
    expected = container.get("hook_token") if container else ""
    # Compare bytes, not str: hmac.compare_digest raises TypeError on `str`
    # arguments containing non-ASCII characters, and `presented` comes straight
    # from the raw Authorization header. An unhandled TypeError would surface
    # as a 500 instead of the intended 401. `.encode("utf-8")` on any `str`
    # always succeeds, and comparing bytes is still constant-time.
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail={"status": "invalid_token"})
    if not _check_rate_limit(owner_id):
        raise HTTPException(status_code=429, detail={"status": "rate_limited"})


class SendRequest(BaseModel):
    agent_id: str
    subject: str
    body_text: str
    body_html: str | None = None
    to: str | None = None
    reply_key: str | None = None
    purpose: str = "routine"


@router.post("/u/{owner_id}/send")
async def send_mail(owner_id: str, body: SendRequest, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if body.to and body.reply_key:
        raise HTTPException(status_code=400, detail={"status": "to_and_reply_key"})
    result = await send(
        owner_id=owner_id,
        agent_id=body.agent_id,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        to=body.to,
        reply_key=body.reply_key,
        purpose=body.purpose,
    )
    return {"status": result}


class AddressRequest(BaseModel):
    agent_id: str


@router.post("/u/{owner_id}/address")
async def mint_address(owner_id: str, body: AddressRequest, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    owner_email = await _owner_email(owner_id)
    address, reason = await ensure_agent_address(
        owner_id=owner_id, agent_id=body.agent_id, owner_email=owner_email or ""
    )
    return {"address": address, "reason": reason}


@router.get("/u/{owner_id}/inbox")
async def inbox(owner_id: str, agent_id: str, request: Request, since: str | None = None, limit: int = 20) -> dict:
    await _require_hook_token(owner_id, request)
    address = await resolve_from_address(owner_id, agent_id)
    if not address:
        return {"address": None, "messages": []}
    rows = await list_messages(address, since=since, limit=limit)
    return {
        "address": address,
        "messages": [
            {
                "key": reply_key(address, r["received_key"]),
                "from": r.get("from_addr", ""),
                "subject": r.get("subject", ""),
                "received_at": r.get("received_at", ""),
                "text": (r.get("text") or "")[:_INBOX_TEXT_CHARS],
            }
            for r in rows
        ],
    }


# ---- owner settings (Clerk) -------------------------------------------------


def _project(row: dict) -> dict:
    return {
        "address": row.get("address"),
        "agent_id": row.get("agent_id"),
        "verified_senders": row.get("verified_senders") or [],
        "send_to_anyone": bool(row.get("send_to_anyone")),
        "outbound_disabled": row.get("outbound_disabled"),
        "status": row.get("status"),
    }


def _validate_senders(entries: list[str]) -> list[str]:
    out: list[str] = []
    for raw in entries:
        e = (raw or "").strip().lower()
        if not (EMAIL_ADDRESS_RE.match(e) or _DOMAIN_RE.match(e)):
            raise ValueError(e)
        out.append(e)
    return out


@router.get("/addresses")
async def list_addresses(auth: AuthContext = Depends(get_current_user)) -> dict:
    rows = await agent_email_repo.query_by_owner(resolve_owner_id(auth))
    return {"addresses": [_project(r) for r in rows if r.get("status") == "active"]}


class AddressSettings(BaseModel):
    verified_senders: list[str] | None = None
    send_to_anyone: bool | None = None
    outbound_disabled: str | None = None  # only explicit null is honoured (re-enable)


@router.patch("/addresses/{agent_id}")
async def patch_address(agent_id: str, body: AddressSettings, auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    address = await resolve_from_address(owner_id, agent_id)
    if not address:
        owner_email = await _owner_email(owner_id)
        address, reason = await ensure_agent_address(
            owner_id=owner_id, agent_id=agent_id, owner_email=owner_email or ""
        )
        if not address:
            raise HTTPException(status_code=409, detail={"status": reason})
    kwargs: dict = {}
    if body.verified_senders is not None:
        try:
            kwargs["verified_senders"] = _validate_senders(body.verified_senders)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"status": "bad_sender", "entry": str(e)}) from e
    if body.send_to_anyone is not None:
        kwargs["send_to_anyone"] = body.send_to_anyone
    if "outbound_disabled" in body.model_fields_set:
        if body.outbound_disabled is not None:
            raise HTTPException(status_code=400, detail={"status": "outbound_disabled_readonly"})
        kwargs["outbound_disabled"] = None
    row = await agent_email_repo.update_settings(address, **kwargs)
    if row is None:
        raise HTTPException(status_code=404, detail={"status": "no_address"})
    return _project(row)
