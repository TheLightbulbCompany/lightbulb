"""A person's own webhook agents (Settings > Connections > Webhook agents). The URL and key go in
once and never come back out: no response or log carries either."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import webhook_agents
from core.services.messaging import ApiProblem
from routers.agent_keys import require_live_member

router = APIRouter()


class RegisterWebhookAgent(BaseModel):
    name: str
    kind: str = "Webhook agent"
    url: str
    key: str


class SenderDecision(BaseModel):
    decision: Literal["allow", "decline"]


async def _call(call):
    try:
        return await call
    except ApiProblem as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.error.message) from exc


@router.get("")
async def list_webhook_agents(auth: AuthContext = Depends(get_current_user)):
    return {"agents": await webhook_agents.list_mine(resolve_owner_id(auth), auth.user_id)}


@router.post("")
async def register_webhook_agent(body: RegisterWebhookAgent, auth: AuthContext = Depends(get_current_user)):
    owner_id = await require_live_member(auth)
    return await _call(webhook_agents.register(owner_id, auth.user_id, body.name, body.kind, body.url, body.key))


@router.delete("/{agent_id}")
async def remove_webhook_agent(agent_id: str, auth: AuthContext = Depends(get_current_user)):
    await _call(webhook_agents.remove(resolve_owner_id(auth), auth.user_id, agent_id))
    return {"status": "ok"}


@router.post("/{agent_id}/test")
async def ping_webhook_agent(agent_id: str, auth: AuthContext = Depends(get_current_user)):
    return await _call(webhook_agents.ping(resolve_owner_id(auth), auth.user_id, agent_id))


@router.post("/{agent_id}/senders/{user_id}")
async def decide_sender(
    agent_id: str, user_id: str, body: SenderDecision, auth: AuthContext = Depends(get_current_user)
):
    count = await _call(webhook_agents.decide(resolve_owner_id(auth), auth.user_id, agent_id, user_id, body.decision))
    return {"status": "ok", "count": count}


@router.delete("/{agent_id}/senders/{user_id}")
async def remove_sender(agent_id: str, user_id: str, auth: AuthContext = Depends(get_current_user)):
    await _call(webhook_agents.remove_sender(resolve_owner_id(auth), auth.user_id, agent_id, user_id))
    return {"status": "ok"}
