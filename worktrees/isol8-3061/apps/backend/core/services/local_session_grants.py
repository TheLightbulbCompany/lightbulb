"""Short-lived, canonical room ingress authority; never a provider credential."""

from dataclasses import dataclass
import re
import time
import uuid

import jwt

from core.services.messaging import ApiProblem
from core.services.service_token import _signing_key

PURPOSE = "lightbulb_local_session_invocation"
AUDIENCE = "lightbulb:local-sessions"
MAX_LIFETIME = 300


@dataclass(frozen=True)
class AgentInvocation:
    owner_id: str
    room_id: str
    agent_id: str
    expires_at: int
    invocation_id: str


def session_key(agent: str, room: str) -> str:
    return f"agent:{agent}:lightbulb:channel:{room}"


def mint(owner: str, room: str, agent: str, event: str) -> str:
    now = int(time.time())
    claims = {
        "kind": PURPOSE,
        "aud": AUDIENCE,
        "ownerId": owner,
        "roomId": room,
        "agentId": agent,
        "eventId": event,
        "sessionKey": session_key(agent, room),
        "iat": now,
        "exp": now + MAX_LIFETIME,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(claims, _signing_key(), algorithm="HS256")
    verify(token)
    return token


def verify(token: str) -> AgentInvocation:
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError()
        claims = jwt.decode(token, _signing_key(), algorithms=["HS256"], audience=AUDIENCE)
        if set(claims) != {"kind", "aud", "ownerId", "roomId", "agentId", "eventId", "sessionKey", "iat", "exp", "jti"}:
            raise ValueError()
        if claims["kind"] != PURPOSE or claims["aud"] != AUDIENCE:
            raise ValueError()
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            raise ValueError()
        if not claims["iat"] <= int(time.time()) < claims["exp"] <= claims["iat"] + MAX_LIFETIME:
            raise ValueError()
        if not isinstance(claims["ownerId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", claims["ownerId"]):
            raise ValueError()
        if not isinstance(claims["agentId"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", claims["agentId"]):
            raise ValueError()
        for key in ("roomId", "eventId", "jti"):
            if not isinstance(claims[key], str) or str(uuid.UUID(claims[key])) != claims[key]:
                raise ValueError()
        if claims["sessionKey"] != session_key(claims["agentId"], claims["roomId"]):
            raise ValueError()
        return AgentInvocation(claims["ownerId"], claims["roomId"], claims["agentId"], claims["exp"], claims["jti"])
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, RuntimeError):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.") from None
