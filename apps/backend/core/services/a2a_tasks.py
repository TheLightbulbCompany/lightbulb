"""A2A tasks on the hosted door: the authority request, the decision, dispatch to a workspace agent,
reply linking and the signed record of every state."""

import hashlib
import json
import logging
import time
import uuid
from typing import Literal

from a2a import types as a2a_types
from google.protobuf.json_format import MessageToDict
from pydantic import ValidationError

from core.auth import AuthContext
from core.config import settings
from core.repositories import a2a_repo, local_sessions_repo, messaging_repo
from core.services import a2a_rules, hosted_mcp, messaging, network_proof, network_route
from core.services.a2a_auth import READ_SCOPE_A2A, SEND_SCOPE, A2ACaller
from core.services.a2a_authority import (
    URI,
    AuthorityError,
    AuthorityRequest,
    Operation,
    mint,
    sign_record,
    sign_rules,
    verify,
)
from core.services.a2a_rules import Rules, default_rules
from core.services.network_proof import _unb64

logger = logging.getLogger(__name__)

HOLD_LIFE_S = 86_400
WORK_LIFE_S = 86_400
SESSION_SEND_TIMEOUT_S = 10.0

STATES = {
    "working": "TASK_STATE_WORKING",
    "held": "TASK_STATE_AUTH_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "rejected": "TASK_STATE_REJECTED",
    "canceled": "TASK_STATE_CANCELED",
    "failed": "TASK_STATE_FAILED",
}


class A2AError(Exception):
    """A JSON-RPC error: ``code`` per A2A §5.4 (-32008 extension required, -32602 invalid params, ...)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def agent_card(org: str, agent_id: str, agent_name: str, base_url: str, oauth: dict[str, str]) -> a2a_types.AgentCard:
    """``oauth``: Clerk's ``authorization_url`` and ``token_url`` (``a2a_auth.oauth_endpoints``)."""
    return a2a_types.AgentCard(
        name=agent_name,
        description=f"{agent_name}, a Lightbulb workspace agent. Every task carries signed authority.",
        version="1",
        supported_interfaces=[
            a2a_types.AgentInterface(
                url=f"{base_url}/a2a/{org}/{agent_id}", protocol_binding="JSONRPC", protocol_version="1.0", tenant=org
            )
        ],
        capabilities=a2a_types.AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[
                a2a_types.AgentExtension(
                    uri=URI,
                    required=True,
                    description="Signed authority: who asked, through which agents, for whom, and exactly what.",
                )
            ],
        ),
        security_schemes={
            "clerk": a2a_types.SecurityScheme(
                http_auth_security_scheme=a2a_types.HTTPAuthSecurityScheme(scheme="Bearer", bearer_format="JWT")
            ),
            "oauth": a2a_types.SecurityScheme(
                oauth2_security_scheme=a2a_types.OAuth2SecurityScheme(
                    oauth2_metadata_url=f"{settings.CLERK_ISSUER.rstrip('/')}/.well-known/oauth-authorization-server",
                    flows=a2a_types.OAuthFlows(
                        authorization_code=a2a_types.AuthorizationCodeOAuthFlow(
                            **oauth,
                            scopes={SEND_SCOPE: "Send the agent tasks.", READ_SCOPE_A2A: "Read your tasks."},
                            pkce_required=True,
                        )
                    ),
                )
            ),
        },
        # Either one: a Lightbulb sign-in, or an OAuth grant for this door's audience.
        security_requirements=[
            a2a_types.SecurityRequirement(schemes={"clerk": a2a_types.StringList()}),
            a2a_types.SecurityRequirement(schemes={"oauth": a2a_types.StringList()}),
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            a2a_types.AgentSkill(
                id="message",
                name="Message",
                description="Send the agent a message and get its reply.",
                tags=["message"],
            ),
            a2a_types.AgentSkill(
                id="ask_session",
                name="Ask a session",
                description="Ask one of the workspace's laptop sessions. Workspace agents only in this version.",
                tags=["session"],
            ),
        ],
    )


def signed_agent_card(
    org: str, agent_id: str, agent_name: str, base_url: str, oauth: dict[str, str]
) -> a2a_types.AgentCard:
    """The card with a JWS signature (RFC 7515, EdDSA) by the issuer key that ``/a2a/keys.json`` serves.
    Without ``NETWORK_PROOF_KEY`` the card is served unsigned, with a warning."""
    card = agent_card(org, agent_id, agent_name, base_url, oauth)
    if not settings.NETWORK_PROOF_KEY:
        logger.warning("a2a card served unsigned: NETWORK_PROOF_KEY is not set")
        return card
    from a2a.utils.signing import create_agent_card_signer
    from jwt import PyJWK
    from jwt.algorithms import OKPAlgorithm

    kid = network_proof.kid()
    jwk = {**OKPAlgorithm.to_jwk(network_proof._private(), as_dict=True), "kid": kid}
    header = {"typ": "JOSE", "alg": "EdDSA", "kid": kid, "jku": f"{base_url}/a2a/keys.json"}
    return create_agent_card_signer(PyJWK.from_dict(jwk), header)(card)


async def agent_name(org: str, agent_id: str) -> str | None:
    """The agent's display name from the workspace roster (``messaging.load_agent_roster``); None if absent."""
    row = next((a for a in await messaging.load_agent_roster(org) if a["id"] == agent_id), None)
    return row["name"] if row else None


def _claims(jws: str) -> dict:
    """The payload of a JWS this module signed (signature checked by ``verify`` where it matters)."""
    return json.loads(_unb64(jws.split(".")[1]))


def _record_jws(
    row: dict,
    decision: str,
    *,
    now: int,
    rule: str | None = None,
    holder: str | list[str] | None = None,
    answered_by: str | None = None,
) -> str:
    """The signed record of this state (spec §6 shape)."""
    claims = _claims(row["authority"])
    record = {
        "task": row["id"],
        "authority_id": claims["id"],
        "authority_hash": "sha256:" + hashlib.sha256(row["authority"].encode()).hexdigest(),
        "decision": decision,
        "rule": rule or row["rule"],
        "policy_revision": row["policyRevision"],
        "holder": holder,
        "answered_by": answered_by,
        "decided_at": now,
        "iat": now,
        "input": {k: claims[k] for k in ("requester", "chain", "operation", "intent")},
    }
    return sign_record(record)


async def _record(org: str, row: dict, decision: str, *, now: int, rule: str | None = None, **who) -> str:
    """Sign and append the record of this state; returns its JWS."""
    jws = _record_jws(row, decision, now=now, rule=rule, **who)
    await a2a_repo.append_record(org, row["id"], {"jws": jws})
    return jws


def _authority_request(message: dict, agent_id: str) -> tuple[AuthorityRequest, str]:
    if not isinstance(message, dict) or URI not in (message.get("extensions") or []):
        raise A2AError(-32008, f"Activate the required extension {URI}.")
    metadata = message.get("metadata")
    if not isinstance(metadata, dict) or URI not in metadata:
        raise A2AError(-32008, f"Carry the authority request in Message.metadata[{URI}].")
    raw = metadata[URI]
    try:
        req = AuthorityRequest.model_validate(raw)
    except ValidationError:
        raise A2AError(-32602, "invalid authority: shape") from None
    if req.operation.skill != "message":  # the door's one skill; ask_session comes from a hosted agent
        raise A2AError(-32602, "invalid authority: skill")
    if req.operation.resource != f"agent:{agent_id}":
        raise A2AError(-32602, f"invalid authority: resource (must be agent:{agent_id})")
    text = "\n".join(p["text"] for p in message.get("parts") or [] if isinstance(p, dict) and "text" in p)
    if not text:
        raise A2AError(-32602, "The message has no text part.")
    # The signed operands are exactly what the agent receives.
    if req.operation.operands != {"text": text}:
        raise A2AError(-32602, "invalid authority: operands")
    return req, text


async def send_message(caller: A2ACaller, org: str, agent_id: str, message: dict, *, now: int | None = None) -> dict:
    auth = caller.auth
    now = int(time.time()) if now is None else now
    # Only a workspace agent: any other id (a person's user_...) would get a system post in a human DM.
    if await agent_name(org, agent_id) is None:
        raise A2AError(-32602, f"Unknown agent {agent_id} in this workspace.")
    req, text = _authority_request(message, agent_id)
    try:
        jws, claims = mint(
            requester_person=auth.user_id,
            org=org,
            verified_by=caller.verified_by,
            # An OAuth client is the first hop's agent; rules match it as agent:<client_id>.
            chain=[{"agent": caller.client_id, "session": None, "person": auth.user_id, "at": now}],
            request=req,
            now=now,
        )
        verify(jws, now=now)
    except AuthorityError as exc:
        raise A2AError(-32602, f"invalid authority: {exc.check}") from None
    if not await a2a_repo.claim_authority_id(org, claims["id"], now):
        raise A2AError(-32602, "replay")
    doc = await a2a_repo.get_rules(org)
    rules = Rules.model_validate(doc) if doc else default_rules(org, [auth.user_id] if auth.is_org_admin else [])
    row = await _decide_and_store(
        auth,
        org,
        agent_id,
        jws,
        claims,
        text,
        rules=rules,
        chain_agents=[caller.client_id] if caller.client_id else [],
        session_person=None,
        now=now,
    )
    return to_a2a_task(row)


async def _decide_and_store(
    auth: AuthContext,
    org: str,
    agent_id: str,
    jws: str,
    claims: dict,
    text: str,
    *,
    rules: Rules,
    chain_agents: list[str],
    session_person: str | None,
    now: int,
    origin_is_person: bool = True,
) -> dict:
    """Decide a minted authority, dispatch when allowed, sign its record, store the task and ask any
    holders; the stored row."""
    requester = claims["requester"]["person"]
    op = claims["operation"]
    exception = await a2a_repo.get_exception(org, op["resource"], op["skill"], requester, op["operands_hash"], now)
    d = a2a_rules.decide(
        rules,
        org=org,
        requester_person=requester,
        requester_org=auth.org_id,
        chain_agents=chain_agents,
        resource=op["resource"],
        skill=op["skill"],
        session_person=session_person,
        exception=exception,
        origin_is_person=origin_is_person,
        operands=op["operands"],
        intent=claims["intent"],
        verified_by=claims["requester"]["verified_by"],
    )
    row = {
        "id": str(uuid.uuid4()),
        "agentId": agent_id,
        "requester": requester,
        "authority": jws,
        "policyRevision": rules.revision,
        "rule": d.rule,
        "holders": d.holders,
        "createdAt": now,
        # every decide input beyond the authority itself, so a rules change can re-decide the task
        "requesterOrg": auth.org_id,
        "chainAgents": chain_agents,
        "sessionPerson": session_person,
        "originIsPerson": origin_is_person,
        "verifiedBy": claims["requester"]["verified_by"],
    }
    if d.effect == "hold" and not d.holders:
        # A hold nobody can answer is a silent hang: refuse it.
        row.update(state="rejected", rule="no_holder")
        decision = "deny"
    elif d.effect == "hold":
        row.update(state="held", heldAt=now, text=text)  # the text is dispatched if a holder allows
        decision = "hold"
    elif d.effect == "allow":
        row["state"] = "working"
        decision = "allow"
    else:
        row["state"] = "rejected"
        decision = "deny"
    failed = row["state"] == "working" and not await _dispatch(auth, org, agent_id, row, text)
    # ponytail: the task row lands after dispatch; a reply faster than this write stays unlinked
    row["record"] = await _record(org, row, decision, now=now, holder=d.holders if decision == "hold" else None)
    if failed:
        await _fail_dispatch(org, row, now)
    elif row.get("delivered"):
        await _complete_delivered(org, row, now)
    await a2a_repo.put_task(org, row)
    if row["state"] == "held":
        await _notify_holders(org, agent_id, row, claims)
    return row


async def _origin_person(owner_id: str, room_id: str | None, message_id: str | None) -> str | None:
    """The human in this workspace who wrote the origin message; None when there is none."""
    if not room_id or not message_id:
        return None
    room = await messaging_repo.get_room(room_id)
    message = await messaging_repo.get_message_by_id(room_id, message_id)
    if not room or room.get("ownerId") != owner_id or not message:
        return None
    author = message.get("author") or {}
    if author.get("kind") != "human" or not str(author.get("id", "")).startswith("user_"):
        return None
    member = await messaging_repo.get_member(room_id, "human", author["id"])
    if not member or messaging_repo.member_home(member, owner_id) != owner_id:
        return None  # a foreign human in a shared room does not ask under this workspace
    return author["id"]


async def ask_session(owner_id: str, body, *, now: int | None = None) -> dict:
    """A hosted agent asks a laptop session, under the person whose message it is answering (else the
    agent's first holder); held for the session's person unless a rule allows it."""
    now = int(time.time()) if now is None else now
    if await agent_name(owner_id, body.agent_id) is None:
        raise A2AError(404, "unknown_agent")
    session = await local_sessions_repo.get_session(owner_id, body.session_id)
    if session is None:
        raise A2AError(404, "unknown_session")
    doc = await a2a_repo.get_rules(owner_id)
    rules = Rules.model_validate(doc) if doc else default_rules(owner_id, [])
    person = await _origin_person(owner_id, body.room_id, body.origin_message_id)
    verified_by = "origin-message"
    if body.origin_message_id and person is None:
        raise A2AError(403, "bad_origin")  # a named origin that fails a check never falls back
    if person is None:
        # ponytail: the agent's first holder stands in as requester; holders are admins by convention,
        # not checked against Clerk here
        holders = a2a_rules._holders(rules, f"agent:{body.agent_id}", None)
        if not holders:
            raise A2AError(403, "no_requester")
        person, verified_by = holders[0], "agent-runtime"
    req = AuthorityRequest(
        operation=Operation(skill="ask_session", resource=f"session:{body.session_id}", operands={"text": body.text}),
        intent=body.intent,
    )
    chain = [
        {"agent": None, "session": None, "person": person, "at": now},
        {"agent": body.agent_id, "session": body.run_id, "person": person, "at": now},
    ]
    try:
        jws, claims = mint(
            requester_person=person, org=owner_id, verified_by=verified_by, chain=chain, request=req, now=now
        )
    except AuthorityError as exc:
        raise A2AError(400, f"invalid authority: {exc.check}") from None
    row = await _decide_and_store(
        AuthContext(user_id=person, org_id=owner_id),
        owner_id,
        body.agent_id,
        jws,
        claims,
        body.text,
        rules=rules,
        chain_agents=[body.agent_id],
        session_person=session.get("humanUserId"),
        now=now,
        origin_is_person=verified_by == "origin-message",
    )
    return {"taskId": row["id"], "state": row["state"], "holders": row["holders"]}


def session_header(*, requester_display: str, for_whom: str, intent: str, record_url: str) -> str:
    """The first line a laptop session sees: who asked, for whom, why, and where the signed record is."""

    def one(v: str) -> str:
        return v.replace("\r", " ").replace("\n", " ")

    head = f"[A2A] {one(requester_display)} for {one(for_whom)}: {one(intent[:200])} — record: {record_url}\n"
    if len(head.encode()) > 512:  # hard cap: cut the whole header on a character boundary, keep the newline
        head = head.encode()[:511].decode(errors="ignore") + "\n"
    return head


async def _fail_dispatch(org: str, row: dict, now: int) -> None:
    reason = row.pop("failReason", "dispatch_failed")
    row.update(state="failed", reason=reason, record=await _record(org, row, "failed", now=now, rule=reason))


async def _complete_delivered(org: str, row: dict, now: int) -> None:
    """A laptop session took the send: the task completes as delivered (its reply does not come back
    to the task in phase 1)."""
    row.update(
        state="completed",
        delivered=True,
        artifact={"artifactId": row["dispatchRequestId"], "parts": [{"text": "delivered"}]},
    )
    row["record"] = await _record(org, row, "completed", now=now)


async def _dispatch_session(org: str, agent_id: str, row: dict, text: str) -> bool:
    """``app.send`` to the session through the origin person's most recent computer link, as
    ``<agent name> via <person>``; sets ``delivered`` on success."""
    claims = _claims(row["authority"])
    origin = claims["chain"][0]["person"]
    url = f"{settings.PUBLIC_API_BASE.rstrip('/')}/a2a/{org}/{agent_id}/tasks/{row['id']}/record"
    header = session_header(
        requester_display=row["requester"], for_whom=origin, intent=claims["intent"], record_url=url
    )
    request_id = hosted_mcp._network_request_id(row["id"])
    try:
        # ponytail: the send rides the origin person's computer link; the cloud member bridge replaces this in phase 2
        link = await network_route.best_link(origin)
        if link is None:
            row["failReason"] = "undeliverable"
            return False
        route = {"kind": "computer", "key": link["key"], "name": link.get("name") or "Your computer"}
        await network_route.app_call(
            origin,
            agent_id,
            await agent_name(org, agent_id) or agent_id,
            "send",
            SESSION_SEND_TIMEOUT_S,
            route=route,
            to=claims["operation"]["resource"].removeprefix("session:"),
            text=header + text,
            request_id=request_id,
        )
    except Exception:
        logger.exception("a2a session dispatch failed", extra={"org": org, "task": row["id"]})
        row["failReason"] = "undeliverable"
        return False
    row.update(dispatchRequestId=request_id, delivered=True)
    return True


async def _dispatch(auth: AuthContext, org: str, agent_id: str, row: dict, text: str) -> bool:
    """Post the text to the agent in the requester's DM with it; sets roomId/dispatchMessageId on the row.
    A ``session:`` resource goes to that laptop session instead. False when the post failed."""
    if _claims(row["authority"])["operation"]["resource"].startswith("session:"):
        return await _dispatch_session(org, agent_id, row, text)
    try:
        # Per operation, never per pair (person_dm docstring): a pair key could replay a DM turned group.
        room = await hosted_mcp.person_dm(auth, agent_id, create_key=row["id"])
        sent = await messaging.create_human_message(
            auth,
            room,
            messaging.CreateMessageRequest(
                client_message_id=uuid.UUID(row["id"]),
                text=text,
                mentions=[],
                session_mentions=[],
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="lightbulb_mcp",
            author_override={
                "kind": "system",
                "id": f"a2a:{auth.user_id}",
                "displayName": f"A2A via {auth.user_id}",
            },
        )
    except Exception:
        logger.exception("a2a dispatch failed", extra={"org": org, "task": row["id"]})
        return False
    row.update(roomId=room, dispatchMessageId=str(sent.message_id))
    return True


def _inert(v: str) -> str:
    """Requester text on one line with no backtick, so it can never open an ``openui-lang`` card."""
    return v.replace("\r", " ").replace("\n", " ").replace("`", "'")


async def _notify_holders(org: str, agent_id: str, row: dict, claims: dict) -> None:
    """The agent asks each holder in their DM with it; a failed post is logged (the hold still expires)."""
    op = claims["operation"]
    base = settings.PUBLIC_API_BASE.rstrip("/")
    operands = op["operands"]
    shown = operands["text"] if isinstance(operands.get("text"), str) else json.dumps(operands, separators=(",", ":"))
    chain = _inert(" → ".join(h.get("agent") or h["person"] for h in claims["chain"]))
    # Answered by pressing the card (``answer_from_message``) or at the REST route with a sign-in token.
    text = (
        f"I need your OK to {op['skill']} on {op['resource']} for {row['requester']} (via {chain}): "
        f"{_inert(claims['intent'][:200])}\n"
        f"Request: {_inert(shown[:500])}\n"
        f"Answer with POST {base}/a2a/{org}/{agent_id}/tasks/{row['id']}/decide "
        '{"verdict": "allow"} or {"verdict": "deny"}, signed in to Lightbulb.\n'
        f"```openui-lang\n{_CARD}\n```"
    )
    sent: dict[str, str] = {}
    for holder in row["holders"]:
        try:
            room = await hosted_mcp.person_dm(AuthContext(user_id=holder, org_id=org), agent_id, create_key=row["id"])
            receipt = await messaging.create_agent_message(
                org,
                messaging.AgentMessageRequest(
                    event_id=f"a2a:{row['id']}:hold:{holder}",
                    room_id=room,
                    agent_id=agent_id,
                    text=text,
                    reply_to_message_id=None,
                    thread_root_message_id=None,
                ),
            )
            sent[holder] = str(receipt.message_id)
            await a2a_repo.put_notification(org, sent[holder], row["id"], holder, str(room))
        except Exception:
            logger.exception("a2a holder notification failed", extra={"org": org, "task": row["id"]})
    if sent:
        row["notificationMessageIds"] = sent
        # expect="held": a holder who already answered keeps the task's new state
        await a2a_repo.update_task(org, row["id"], state="held", extra={"notificationMessageIds": sent}, expect="held")


_CARD = 'root = Stack([b])\nb = Buttons([Button("Approve", "primary"), Button("Deny", "secondary")])'
_VERDICTS: dict[str, Literal["allow", "deny"]] = {"Approve": "allow", "Deny": "deny"}


async def answer_from_message(
    auth: AuthContext,
    org: str,
    room_id: str,
    request: messaging.CreateMessageRequest,
    *,
    now: int | None = None,
) -> bool:
    """A holder's press on their hold card (a reply ``Approve``/``Deny`` to the notification, in the
    notification's room) decides the hold; True when the message was that press (stored without waking
    the agent). A refused decision is told back in the room by the agent."""
    verdict = _VERDICTS.get(request.text.strip())
    if verdict is None or request.reply_to_message_id is None:
        return False
    found = await a2a_repo.get_task_by_notification(org, str(request.reply_to_message_id))
    if found is None or found[2] != room_id:
        return False
    task_id, holder, room = found
    row = await a2a_repo.get_task(org, task_id)
    if row is None:
        return False
    if holder != auth.user_id:
        error = "not_holder"  # spec §10.2: someone else's card, pressed in its room, records nothing
    else:
        try:
            await decide(auth, org, row["agentId"], task_id, verdict=verdict, reason="in-app", now=now)
            return True
        except A2AError as exc:
            error = exc.message
        if error == "not_held" and ((await a2a_repo.get_task(org, task_id)) or {}).get("answeredBy") == auth.user_id:
            return True  # the same holder pressed twice (or the client resent the press): already recorded
    try:  # the press is still an answer attempt: never wake the agent on it
        await messaging.create_agent_message(
            org,
            messaging.AgentMessageRequest(
                event_id=f"a2a:{task_id}:answer-failed:{auth.user_id}",
                room_id=room,
                agent_id=row["agentId"],
                text=f"could not record your answer: {error}",
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
        )
    except Exception:
        logger.exception("a2a answer refusal post failed", extra={"org": org, "task": task_id})
    return True


async def _leave(
    org: str, row: dict, state: str, decision: str, *, now: int, extra: dict, expect: str = "held", **who
) -> dict | None:
    """``expect`` -> ``state`` once, with its signed record; the updated row, or None when the task already
    left ``expect`` (another holder, the expiry, a rules change or a reply got there first)."""
    done = {**row, **extra, "state": state}
    done["record"] = _record_jws(done, decision, now=now, rule=extra.get("reason"), **who)
    if not await a2a_repo.update_task(
        org, row["id"], state=state, extra={**extra, "record": done["record"]}, expect=expect
    ):
        return None
    await a2a_repo.append_record(org, row["id"], {"jws": done["record"]})
    return done


async def _current(org: str, row: dict, now: int) -> dict:
    """The row as of now; a hold seen expired is written canceled, and a working task past its day
    failed, once (with its record)."""
    seen = _as_of(row, now)
    if seen["state"] == row["state"]:
        return seen
    done = await _leave(
        org, row, seen["state"], seen["state"], now=now, extra={"reason": seen["reason"]}, expect=row["state"]
    )
    return done or await a2a_repo.get_task(org, row["id"])


async def decide(
    auth: AuthContext,
    org: str,
    agent_id: str,
    task_id: str,
    *,
    verdict: Literal["allow", "deny"],
    reason: str,
    now: int | None = None,
) -> dict:
    """A holder answers a held task: allow writes a one-day exception for these operands and dispatches,
    deny rejects."""
    now = int(time.time()) if now is None else now
    row = await a2a_repo.get_task(org, task_id)
    if row is None or row["agentId"] != agent_id:
        raise A2AError(404, "not_found")
    # An agent-runtime ask's requester is a stand-in (the agent's first holder), not a person asking.
    if auth.user_id == row["requester"] and _claims(row["authority"])["requester"]["verified_by"] != "agent-runtime":
        raise A2AError(403, "self")
    holders = row.get("holders") or []
    # Checked against the holders before any re-decide below: in the rules-change race a newly added
    # holder gets 403 and a removed holder's press still triggers the re-decide (accepted).
    if auth.user_id not in holders:
        raise A2AError(403, "not_holder")
    row = await _current(org, row, now)
    if row["state"] == "canceled" and row.get("reason") == "expired":
        raise A2AError(409, "expired")
    state, decision = ("working", "allow_by_holder") if verdict == "allow" else ("rejected", "deny_by_holder")
    if row["state"] != "held":
        raise A2AError(409, "not_held")
    doc = await a2a_repo.get_rules(org)
    if row["policyRevision"] != (doc or {}).get("revision", "default"):
        # Never answered under rules the hold was not decided by (a put_rules raced this hold): decide
        # it again first, and answer only if it is still held and this person still holds it.
        row = await _redecide(org, row, Rules.model_validate(doc) if doc else default_rules(org, []), now=now)
        if row["state"] != "held":
            raise A2AError(409, "not_held")
        if auth.user_id not in row["holders"]:
            raise A2AError(403, "not_holder")
    # Claim the answer first: a second holder, the expiry or a rules change can only lose the race.
    row = await _leave(
        org,
        row,
        state,
        decision,
        now=now,
        extra={"answeredBy": auth.user_id, "decisionReason": reason, "dispatchedAt": now},
        holder=auth.user_id,
        answered_by=auth.user_id,
    )
    if not row:
        raise A2AError(409, "not_held")
    if verdict == "deny":
        return to_a2a_task(row)
    op = _claims(row["authority"])["operation"]
    await a2a_repo.put_exception(
        org,
        op["resource"],
        op["skill"],
        row["requester"],
        op["operands_hash"],
        holder=auth.user_id,
        task_id=task_id,
        now=now,
    )
    await _dispatch_from_hold(org, row, now)
    return to_a2a_task(row)


async def _dispatch_from_hold(org: str, row: dict, now: int) -> None:
    """Dispatch a task that just left held for working, as its requester (their DM with the agent),
    and write the outcome; a failed dispatch ends the task failed, never stuck."""
    if await _dispatch(AuthContext(user_id=row["requester"], org_id=org), org, row["agentId"], row, row["text"]):
        if row.get("delivered"):
            await _complete_delivered(org, row, now)
        keys = ("roomId", "dispatchMessageId", "dispatchRequestId", "delivered", "artifact", "record")
        extra = {k: row[k] for k in keys if k in row}
    else:
        await _fail_dispatch(org, row, now)
        extra = {"reason": row["reason"], "record": row["record"]}
    # ponytail: a reply landing between dispatch and this write finds no room on the task and stays
    # unlinked, the same window send_message has
    await a2a_repo.update_task(org, row["id"], state=row["state"], extra=extra)


async def _redecide(org: str, row: dict, rules: Rules, *, now: int) -> dict:
    """Decide a held task again under ``rules`` and act on it; the updated row. Allow dispatches
    (``allow_by_policy``, no exception), deny rejects (``deny_by_policy``), hold stays held with the new
    holders and revision (``redecided``, holders asked again if the set changed), a hold nobody can
    answer is rejected ``no_holder``. An expired hold cancels as expired; a row stored before its
    decide inputs were (phase 1) cancels ``policy_changed``. Every write expects held: a holder who
    answered first keeps their answer."""
    if ((await a2a_repo.get_rules(org)) or {}).get("revision", "default") != rules.revision:
        return row  # a newer document landed while this loop ran: its own re-decide handles the row
    if _as_of(row, now)["state"] != "held":
        return await _current(org, row, now)
    if "requesterOrg" not in row:
        done = await _leave(org, row, "canceled", "canceled", now=now, extra={"reason": "policy_changed"})
        return done or await a2a_repo.get_task(org, row["id"])
    claims = _claims(row["authority"])
    op = claims["operation"]
    d = a2a_rules.decide(
        rules,
        org=org,
        requester_person=row["requester"],
        requester_org=row["requesterOrg"],
        chain_agents=row["chainAgents"],
        resource=op["resource"],
        skill=op["skill"],
        session_person=row["sessionPerson"],
        exception=await a2a_repo.get_exception(
            org, op["resource"], op["skill"], row["requester"], op["operands_hash"], now
        ),
        origin_is_person=row["originIsPerson"],
        operands=op["operands"],
        intent=claims["intent"],
        verified_by=row["verifiedBy"],
    )
    extra = {"rule": d.rule, "holders": d.holders, "policyRevision": rules.revision}
    if d.effect == "hold" and d.holders:
        done = await _leave(org, row, "held", "redecided", now=now, extra=extra, holder=d.holders)
        if done and set(d.holders) != set(row.get("holders") or []):
            # Fresh cards and NOTIF rows; the old ones still resolve to this task (ruling 1).
            await _notify_holders(org, row["agentId"], done, claims)
    elif d.effect == "allow":
        done = await _leave(org, row, "working", "allow_by_policy", now=now, extra={**extra, "dispatchedAt": now})
        if done:
            await _dispatch_from_hold(org, done, now)
    else:  # deny, or a hold nobody can answer
        if d.effect == "hold":
            extra["rule"] = "no_holder"
        done = await _leave(org, row, "rejected", "deny_by_policy", now=now, extra=extra)
    return done or await a2a_repo.get_task(org, row["id"])


def revision(rules: Rules) -> str:
    """The server's revision: sha256 hex of the document's canonical JSON without ``revision`` and
    without unset fields (a document with no ``when`` keeps its phase-1 revision)."""
    doc = rules.model_dump(exclude={"revision"}, exclude_none=True)
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def put_rules(auth: AuthContext, org: str, rules: Rules, *, now: int | None = None) -> dict:
    """Store the rules under a server-assigned revision; a new revision then re-decides every held task
    under it (``_redecide``), and any PUT re-decides a held task left under an older revision (one
    whose re-decide failed, or a hold stored after the scan; ``decide``'s revision check also catches
    those). Returns the stored document."""
    if not auth.is_org_admin:
        raise A2AError(403, "admin")
    now = int(time.time()) if now is None else now
    doc = {**rules.model_dump(), "revision": revision(rules)}
    await a2a_repo.put_rules(org, doc)  # first: a hold made from now on is decided by the new rules
    stored = Rules.model_validate(doc)
    # ponytail: one task at a time inside the request; a queue when an org holds hundreds
    for row in await a2a_repo.list_held_tasks(org):
        if row["policyRevision"] == doc["revision"]:
            continue  # already decided by this document; a retried PUT repairs any task that failed
        try:
            await _redecide(org, row, stored, now=now)
        except Exception:
            logger.exception("a2a re-decide failed", extra={"org": org, "task": row["id"]})
    try:
        await network_route.push_rules(org, sign_rules(org, doc, now=now))
    except Exception as exc:  # the laptops catch up on their next connect; the PUT stands
        logger.warning("a2a rules push failed: %s", type(exc).__name__, extra={"org": org})
        logger.debug("a2a rules push failure", exc_info=True)
    return doc


async def current_rules_jws(org: str, *, now: int | None = None) -> str:
    """The workspace's stored rules document signed, or the shipped default when none is stored."""
    doc = await a2a_repo.get_rules(org) or default_rules(org, []).model_dump()
    return sign_rules(org, doc, now=int(time.time()) if now is None else now)


def _readable(auth: AuthContext, row: dict) -> bool:
    return auth.user_id == row["requester"] or auth.is_org_admin or auth.user_id in (row.get("holders") or [])


def _as_of(row: dict, now: int) -> dict:
    """Expiry computed on read (spec §5 step 6, §6): no sweeper."""
    if row["state"] == "held" and now - int(row["heldAt"]) >= HOLD_LIFE_S:
        return {**row, "state": "canceled", "reason": "expired"}
    if row["state"] == "working" and now - int(row.get("dispatchedAt") or row["createdAt"]) >= WORK_LIFE_S:
        return {**row, "state": "failed", "reason": "undeliverable"}
    return row


async def get_task(org: str, task_id: str, *, reader: AuthContext, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    row = await a2a_repo.get_task(org, task_id)
    if row is None or not _readable(reader, row):
        raise A2AError(-32001, "Task not found.")
    return to_a2a_task(await _current(org, row, now))


async def records(org: str, task_id: str, *, reader: AuthContext) -> list[dict]:
    """Every signed record for the task, oldest first."""
    row = await a2a_repo.get_task(org, task_id)
    if row is None or not _readable(reader, row):
        raise A2AError(-32001, "Task not found.")
    return [{"record": _claims(r["jws"]), "jws": r["jws"]} for r in await a2a_repo.list_records(org, task_id)]


async def on_agent_reply(
    owner_id: str, *, room_id: str, agent_id: str, message_id: str, text: str, reply_to_message_id: str | None = None
) -> None:
    """Link the agent's final reply to the task it answers: only a reply addressed to the dispatch
    (the bridge sets ``replyToMessageId`` to the trigger message, ``turn.py``)."""
    if reply_to_message_id is None:
        return
    tasks = await a2a_repo.list_room_tasks(owner_id, room_id, agent_id)
    if any((t.get("artifact") or {}).get("artifactId") == message_id for t in tasks):
        return  # a retried post of a reply already linked
    row = next(
        (t for t in tasks if t["state"] == "working" and t.get("dispatchMessageId") == reply_to_message_id), None
    )
    if row is None:
        return
    now = int(time.time())
    row = await _current(owner_id, row, now)
    if row["state"] != "working":
        return  # past its day: written failed, a late reply completes nothing
    jws = _record_jws(row, "completed", now=now)
    artifact = {"artifactId": message_id, "parts": [{"text": text}]}
    if await a2a_repo.complete_task(owner_id, row["id"], artifact=artifact, record=jws):
        await a2a_repo.append_record(owner_id, row["id"], {"jws": jws})


def to_a2a_task(row: dict) -> dict:
    task = a2a_types.Task(
        id=row["id"],
        context_id=row.get("roomId", ""),
        status=a2a_types.TaskStatus(state=a2a_types.TaskState.Value(STATES[row["state"]])),
    )
    if row.get("artifact"):
        art = row["artifact"]
        task.artifacts.append(
            a2a_types.Artifact(
                artifact_id=art["artifactId"], parts=[a2a_types.Part(text=p["text"]) for p in art["parts"]]
            )
        )
    meta = {"state": row["state"], "record": row["record"], "rule": row["rule"]}
    if row.get("reason"):
        meta["reason"] = row["reason"]
    if row.get("delivered"):
        meta["delivered"] = True
    task.metadata.update({URI: meta})
    return MessageToDict(task)
