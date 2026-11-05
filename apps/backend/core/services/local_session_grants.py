"""Short-lived local-session authority for one agent turn; never a provider credential.

Three kinds (spec 2026-10-07-agent-session-approval §3.1): a room turn's grant, minted per room
dispatch; a Paperclip run's (run, heartbeat or routine), minted at the facade; and a task run's
(contracts-v2 §14.3), minted by runtime_runs.create_run. Paperclip and task grants are background
grants: no room and no requester."""

from dataclasses import dataclass
import re
import time
import uuid

import jwt

from core.services.messaging import ApiProblem
from core.tasks_schema import worker_session_key
from core.services.service_token import _signing_key

PURPOSE = "lightbulb_local_session_invocation"
AUDIENCE = "lightbulb:local-sessions"
MAX_LIFETIME = 1800

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
_AGENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_RUN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ISSUE_RE = re.compile(r"[A-Za-z0-9-]{1,128}")
_CLAIMS = {"kind", "aud", "ownerId", "roomId", "agentId", "eventId", "requesterId", "sessionKey", "iat", "exp", "jti"}


@dataclass(frozen=True)
class AgentInvocation:
    owner_id: str
    # None (or "") for a Paperclip run: it has no room.
    room_id: str | None
    agent_id: str
    expires_at: int
    invocation_id: str
    # The human whose message triggered this turn, derived by the backend from the canonical
    # message author -- never from the agent. None for agent-authored or routine triggers.
    requester_id: str | None = None
    origin: dict | None = None
    # One run: the room trigger's message id, or the Paperclip run id. A decline holds for it.
    turn_id: str | None = None
    # Whose access the turn READS rooms with when nobody signed in asked (owner ruling 2026-10-13: a
    # Google Meet reads as the member who invited the agents). Rooms only: every other right a grant
    # carries (local sessions, the Mac) is the requester's, and a Meet guest is never one.
    reader_id: str | None = None


def session_key(agent: str, room: str) -> str:
    return f"agent:{agent}:lightbulb:channel:{room}"


def _sign(claims: dict) -> str:
    now = int(time.time())
    token = jwt.encode(
        {**claims, "kind": PURPOSE, "aud": AUDIENCE, "iat": now, "exp": now + MAX_LIFETIME, "jti": str(uuid.uuid4())},
        _signing_key(),
        algorithm="HS256",
    )
    verify(token)
    return token


def mint(owner: str, room: str, agent: str, event: str, requester: str | None = None, reader: str | None = None) -> str:
    return _sign(
        {
            "ownerId": owner,
            "roomId": room,
            "agentId": agent,
            "eventId": event,
            "requesterId": requester,
            "sessionKey": session_key(agent, room),
            "origin": {"kind": "room", "roomId": room},
            **({"readerId": reader} if reader else {}),
        }
    )


def mint_paperclip(owner: str, agent: str, hermes_session_key: str, run_id: str, issue_id: str | None) -> str:
    """The grant for one verified Paperclip run (``create_paperclip_hermes_run``)."""
    return _sign(
        {
            "ownerId": owner,
            "roomId": None,
            "agentId": agent,
            "eventId": None,
            "requesterId": None,
            "sessionKey": hermes_session_key,
            "origin": {"kind": "paperclip", "runId": run_id, "issueId": issue_id},
        }
    )


def mint_task(owner: str, agent: str, session_key: str, run_id: str, task_id: str) -> str:
    """The grant for one task run (``runtime_runs.create_run``): background, like a Paperclip run's."""
    return _sign(
        {
            "ownerId": owner,
            "roomId": None,
            "agentId": agent,
            "eventId": None,
            "requesterId": None,
            "sessionKey": session_key,
            "origin": {"kind": "task", "runId": run_id, "taskId": task_id},
        }
    )


def _uuid(value) -> bool:
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _turn(claims: dict) -> str:
    """Check the origin-specific claims; answer the run they belong to."""
    agent, room = claims["agentId"], claims["roomId"]
    origin = claims.get("origin", {"kind": "room", "roomId": room})
    if origin == {"kind": "room", "roomId": room}:
        if not (_uuid(room) and _uuid(claims["eventId"])) or claims["sessionKey"] != session_key(agent, room):
            raise ValueError()
        return claims["eventId"]
    if isinstance(origin, dict) and set(origin) == {"kind", "runId", "taskId"} and origin["kind"] == "task":
        run, task = origin["runId"], origin["taskId"]
        if (
            room is not None
            or claims["eventId"] is not None
            or claims["requesterId"] is not None
            or not (_uuid(run) and _uuid(task))
            or claims["sessionKey"] != worker_session_key(agent, task)
        ):
            raise ValueError()
        return run
    if not (isinstance(origin, dict) and set(origin) == {"kind", "runId", "issueId"} and origin["kind"] == "paperclip"):
        raise ValueError()
    run, issue = origin["runId"], origin["issueId"]
    if (
        room is not None
        or claims["eventId"] is not None
        or claims["requesterId"] is not None
        or not (isinstance(run, str) and _RUN_RE.fullmatch(run))
        or not (issue is None or (isinstance(issue, str) and _ISSUE_RE.fullmatch(issue)))
    ):
        raise ValueError()
    # The facade's verified X-Hermes-Session-Key, bound to this agent and this run or issue.
    scope = f"issue:{issue}" if issue else f"run:{run}"
    key = claims["sessionKey"]
    if not (
        isinstance(key, str)
        and re.fullmatch(rf"paperclip:company:[A-Za-z0-9-]+:agent:{re.escape(agent)}:{re.escape(scope)}", key)
    ):
        raise ValueError()
    return run


def verify(token: str) -> AgentInvocation:
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError()
        claims = jwt.decode(token, _signing_key(), algorithms=["HS256"], audience=AUDIENCE)
        # A grant without `origin` is a room grant minted by an older task during a roll.
        keys = set(claims) - {"readerId"}
        if keys != _CLAIMS | {"origin"} and not (keys == _CLAIMS and claims.get("roomId")):
            raise ValueError()
        reader = claims.get("readerId")
        if reader is not None and not (isinstance(reader, str) and _ID_RE.fullmatch(reader) and claims.get("roomId")):
            raise ValueError()
        if claims["kind"] != PURPOSE or claims["aud"] != AUDIENCE:
            raise ValueError()
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            raise ValueError()
        if not claims["iat"] <= int(time.time()) < claims["exp"] <= claims["iat"] + MAX_LIFETIME:
            raise ValueError()
        if not isinstance(claims["ownerId"], str) or not _ID_RE.fullmatch(claims["ownerId"]):
            raise ValueError()
        if not isinstance(claims["agentId"], str) or not _AGENT_RE.fullmatch(claims["agentId"]):
            raise ValueError()
        if claims["requesterId"] is not None and not (
            isinstance(claims["requesterId"], str) and _ID_RE.fullmatch(claims["requesterId"])
        ):
            raise ValueError()
        if not _uuid(claims["jti"]):
            raise ValueError()
        turn = _turn(claims)
        return AgentInvocation(
            claims["ownerId"],
            claims["roomId"],
            claims["agentId"],
            claims["exp"],
            claims["jti"],
            claims["requesterId"],
            claims.get("origin", {"kind": "room", "roomId": claims["roomId"]}),
            turn,
            reader_id=reader,
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, RuntimeError):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session invocation is unavailable.") from None
