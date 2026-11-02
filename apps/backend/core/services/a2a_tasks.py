"""A2A tasks on the hosted door: the authority request, the decision, dispatch to a workspace agent,
reply linking and the signed record of every state."""

import hashlib
import json
import logging
import time
import uuid

from a2a import types as a2a_types
from google.protobuf.json_format import MessageToDict
from pydantic import ValidationError

from core.auth import AuthContext
from core.repositories import a2a_repo
from core.services import hosted_mcp, messaging
from core.services.a2a_authority import URI, AuthorityError, AuthorityRequest, mint, sign_record, verify
from core.services.a2a_rules import Rules, decide, default_rules
from core.services.network_proof import _unb64

logger = logging.getLogger(__name__)

HOLD_LIFE_S = 86_400
WORK_LIFE_S = 86_400

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


def agent_card(org: str, agent_id: str, agent_name: str, base_url: str) -> a2a_types.AgentCard:
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
            )
        },
        security_requirements=[a2a_types.SecurityRequirement(schemes={"clerk": a2a_types.StringList()})],
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


async def agent_name(org: str, agent_id: str) -> str | None:
    """The agent's display name from the workspace roster (``messaging.load_agent_roster``); None if absent."""
    row = next((a for a in await messaging.load_agent_roster(org) if a["id"] == agent_id), None)
    return row["name"] if row else None


def _claims(jws: str) -> dict:
    """The payload of a JWS this module signed (signature checked by ``verify`` where it matters)."""
    return json.loads(_unb64(jws.split(".")[1]))


async def _record(org: str, row: dict, decision: str, *, now: int, rule: str | None = None) -> str:
    """Sign and append the record of this state (spec §6 shape); returns its JWS."""
    claims = _claims(row["authority"])
    record = {
        "task": row["id"],
        "authority_id": claims["id"],
        "authority_hash": "sha256:" + hashlib.sha256(row["authority"].encode()).hexdigest(),
        "decision": decision,
        "rule": rule or row["rule"],
        "policy_revision": row["policyRevision"],
        "holder": None,
        "answered_by": None,
        "decided_at": now,
        "iat": now,
        "input": {k: claims[k] for k in ("requester", "chain", "operation", "intent")},
    }
    jws = sign_record(record)
    await a2a_repo.append_record(org, row["id"], {"jws": jws})
    return jws


def _authority_request(message: dict, agent_id: str) -> tuple[AuthorityRequest, str]:
    if not isinstance(message, dict) or URI not in (message.get("extensions") or []):
        raise A2AError(-32008, f"Activate the required extension {URI}.")
    raw = (message.get("metadata") or {}).get(URI)
    try:
        req = AuthorityRequest.model_validate(raw)
    except ValidationError:
        raise A2AError(-32008, f"Message.metadata[{URI}] must be an authority request.") from None
    if req.operation.resource != f"agent:{agent_id}":
        raise A2AError(-32008, f"operation.resource must be agent:{agent_id}.")
    text = "\n".join(p["text"] for p in message.get("parts") or [] if isinstance(p, dict) and "text" in p)
    if not text:
        raise A2AError(-32602, "The message has no text part.")
    # The signed operands are exactly what the agent receives.
    if req.operation.skill == "message" and req.operation.operands != {"text": text}:
        raise A2AError(-32602, "invalid authority: operands")
    return req, text


async def send_message(auth: AuthContext, org: str, agent_id: str, message: dict, *, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    req, text = _authority_request(message, agent_id)
    try:
        jws, claims = mint(
            requester_person=auth.user_id,
            org=org,
            verified_by="clerk-session",
            chain=[{"agent": None, "session": None, "person": auth.user_id, "at": now}],
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
    d = decide(
        rules,
        org=org,
        requester_person=auth.user_id,
        requester_org=auth.org_id,
        chain_agents=[],
        resource=req.operation.resource,
        skill=req.operation.skill,
        session_person=None,
        exception=None,
    )
    row = {
        "id": str(uuid.uuid4()),
        "agentId": agent_id,
        "requester": auth.user_id,
        "authority": jws,
        "policyRevision": rules.revision,
        "rule": d.rule,
        "holders": d.holders,
        "createdAt": now,
    }
    if d.effect == "hold" and not d.holders:
        # A hold nobody can answer is a silent hang: refuse it.
        row.update(state="rejected", rule="no_holder")
        decision = "deny"
    elif d.effect == "hold":
        row.update(state="held", heldAt=now)  # Task 5 notifies the holders
        decision = "hold"
    elif d.effect == "allow":
        row["state"] = "working"
        decision = "allow"
    else:  # deny, or reject/self
        row["state"] = "rejected"
        decision = "deny"
    failed = False
    if row["state"] == "working":
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
            row.update(roomId=room, dispatchMessageId=str(sent.message_id), dispatchSeq=sent.message_seq)
        except Exception:
            logger.exception("a2a dispatch failed", extra={"org": org, "task": row["id"]})
            failed = True
    # ponytail: the task row lands after dispatch; a reply faster than this write stays unlinked
    row["record"] = await _record(org, row, decision, now=now)
    if failed:
        row.update(state="failed", reason="dispatch_failed", record=await _record(org, row, "failed", now=now))
    await a2a_repo.put_task(org, row)
    return to_a2a_task(row)


def _readable(auth: AuthContext, row: dict) -> bool:
    return auth.user_id == row["requester"] or auth.is_org_admin or auth.user_id in (row.get("holders") or [])


def _as_of(row: dict, now: int) -> dict:
    """Expiry computed on read (spec §5 step 6, §6): no sweeper."""
    if row["state"] == "held" and now - int(row["heldAt"]) >= HOLD_LIFE_S:
        return {**row, "state": "canceled", "reason": "expired"}
    if row["state"] == "working" and now - int(row["createdAt"]) >= WORK_LIFE_S:
        return {**row, "state": "failed", "reason": "undeliverable"}
    return row


async def get_task(org: str, task_id: str, *, reader: AuthContext, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    row = await a2a_repo.get_task(org, task_id)
    if row is None or not _readable(reader, row):
        raise A2AError(-32001, "Task not found.")
    return to_a2a_task(_as_of(row, now))


async def records(org: str, task_id: str, *, reader: AuthContext) -> list[dict]:
    """Every signed record for the task, oldest first."""
    row = await a2a_repo.get_task(org, task_id)
    if row is None or not _readable(reader, row):
        raise A2AError(-32001, "Task not found.")
    return [{"record": _claims(r["jws"]), "jws": r["jws"]} for r in await a2a_repo.list_records(org, task_id)]


async def on_agent_reply(owner_id: str, *, room_id: str, agent_id: str, message_id: str, text: str) -> None:
    # ponytail: one open task per DM room completes in order
    open_tasks = await a2a_repo.list_working_tasks(owner_id, room_id, agent_id)
    if not open_tasks:
        return
    row = min(open_tasks, key=lambda t: int(t["dispatchSeq"]))
    now = int(time.time())
    artifact = {"artifactId": message_id, "parts": [{"text": text}]}
    jws = await _record(owner_id, row, "completed", now=now)
    await a2a_repo.update_task(owner_id, row["id"], state="completed", extra={"artifact": artifact, "record": jws})


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
    task.metadata.update({URI: meta})
    return MessageToDict(task)
