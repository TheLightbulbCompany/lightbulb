"""Webhook agents: agents that live in someone else's cloud and wake on a webhook (Grok Bot first).

Agent network phase 3. A person registers one in Settings with its webhook URL and bearer key.
It is no longer a send target (agents message through the engine; a webhook agent reaches the
team through the connector like any app). Sends held before that still wait for its person's
answer here: Send or Allow delivers them as ONE POST each, Decline drops them.

Rows live in the messaging table under the workspace's OWNER# partition (both wipes remove them):
    WEBHOOKAGENT#<agentId>              the agent: its person, name, kind, who it lets straight in
    WEBHOOKSEND#<agentId>#<requestId>   one send: its sender, state, and its text only while held
The URL and key are ONE Secrets Manager secret under the owner's integrations prefix, so the owner
secret sweep (``owner_secret_sweep``) deletes it with the workspace. They are never returned or logged.

A delivery keeps the header defang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as repo
from core.secrets_manager import create_or_update_secret
from core.services import link_preview
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

MAX_AGENTS_PER_PERSON = 10
TIMEOUT_S = 5.0
RETRY_DELAYS_S = (0.5, 1.5)  # three attempts in all
HEADER_PREFIX = "[Lightbulb teammate contribution"  # the Mac's header (local_sessions.rs)

# Tests swap this for an httpx.MockTransport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _agent_sk(agent_id: str) -> str:
    return f"WEBHOOKAGENT#{agent_id}"


def _send_sk(agent_id: str, request_id: str) -> str:
    return f"WEBHOOKSEND#{agent_id}#{request_id}"


def secret_name(owner_id: str, agent_id: str) -> str:
    return f"lightbulb/{settings.ENVIRONMENT or 'dev'}/integrations/{owner_id}/agent-webhooks/{agent_id}"


def _sm():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


# ---- defang (a port of the Mac's, local_sessions.rs) ---------------------


def _invisible(c: str) -> bool:
    o = ord(c)
    return (
        o in (0x00AD, 0x180E, 0xFEFF)
        or 0x200B <= o <= 0x200F
        or 0x202A <= o <= 0x202E
        or 0x2060 <= o <= 0x2064
        or 0x2066 <= o <= 0x2069
    )


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not _invisible(c)).lower()


def defang(body: str) -> str:
    """A body line that reads as our header (lookalikes and invisibles included) gets its leading
    ``[`` swapped for ``［``, so a body can never forge a second frame."""
    out = []
    for line in body.split("\n"):
        lead = next((i for i, c in enumerate(line) if not (c.isspace() or _invisible(c))), len(line))
        if line[lead:].startswith("[") and _fold(line[lead:]).startswith(_fold(HEADER_PREFIX)):
            line = line[:lead] + "［" + line[lead + 1 :]
        out.append(line)
    return "\n".join(out)


def _clean(label: str) -> str:
    return "".join(c for c in label if c not in "{}[]\r\n")


# ---- the registry --------------------------------------------------------


def check_url(url: str) -> str:
    """https to a default port, no credentials. The host is resolved (public only) at every POST."""
    try:
        scheme, _, _ = link_preview._check_url(url)
    except link_preview.Refused as exc:
        raise ApiProblem("INVALID_REQUEST", f"Webhook URL refused: {exc}.") from exc
    if scheme != "https" or len(url) > 2048:
        raise ApiProblem("INVALID_REQUEST", "The webhook URL must be https.")
    return url


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in ("agentId", "name", "kind", "host", "createdAt", "allowedSenders")}


async def get_agent(owner_id: str, agent_id: str) -> dict | None:
    return await repo._get(repo.owner_pk(owner_id), _agent_sk(agent_id))


async def list_agents(owner_id: str) -> list[dict]:
    rows = await repo._query(repo.owner_pk(owner_id), begins_with="WEBHOOKAGENT#")
    return sorted(rows, key=lambda r: r["createdAt"])


async def list_mine(owner_id: str, user_id: str) -> list[dict]:
    """A person's own agents, each with the senders waiting for their Allow."""
    from core.services.messaging import human_display_name

    mine = [r for r in await list_agents(owner_id) if r["ownerUserId"] == user_id]
    result = []
    for row in mine:
        waiting: dict[str, int] = {}
        for send in await _held(owner_id, row["agentId"]):
            waiting[send["actorUserId"]] = waiting.get(send["actorUserId"], 0) + 1
        allowed = row.get("allowedSenders") or []
        names = dict(zip([*waiting, *allowed], [await human_display_name(u) for u in [*waiting, *allowed]]))
        result.append(
            {
                **_public(row),
                "waiting": [{"userId": u, "name": names[u], "count": n} for u, n in waiting.items()],
                "allowedSenders": [{"userId": u, "name": names[u]} for u in allowed],
            }
        )
    return result


async def register(owner_id: str, user_id: str, name: str, kind: str, url: str, key: str) -> dict:
    name, kind = " ".join(name.split()), " ".join(kind.split()) or "Webhook agent"
    if not name or len(name) > 60 or len(kind) > 40:
        raise ApiProblem("INVALID_REQUEST", "Name the agent in 1-60 characters.")
    if not key.strip() or len(key) > 4096:
        raise ApiProblem("INVALID_REQUEST", "Paste the webhook's key.")
    check_url(url)
    # ponytail: count-then-write, so two racing registers can pass 10; agent_keys' counter row if it matters.
    if sum(r["ownerUserId"] == user_id for r in await list_agents(owner_id)) >= MAX_AGENTS_PER_PERSON:
        raise ApiProblem("INVALID_REQUEST", f"You can have at most {MAX_AGENTS_PER_PERSON} webhook agents.")
    agent_id = str(uuid.uuid4())
    secret = json.dumps({"url": url, "key": key.strip()})
    await run_in_thread(create_or_update_secret, _sm(), secret_name(owner_id, agent_id), secret)
    row = {
        "pk": repo.owner_pk(owner_id),
        "sk": _agent_sk(agent_id),
        "agentId": agent_id,
        "ownerUserId": user_id,
        "name": name,
        "kind": kind,
        "host": urlsplit(url).hostname,
        "createdAt": utc_now_iso(),
        "allowedSenders": [],
    }
    await run_in_thread(repo._table().put_item, Item=row)
    logger.info("webhook agent registered owner=%s user=%s agent=%s", owner_id, user_id, agent_id)
    return _public(row)


async def _owned(owner_id: str, user_id: str, agent_id: str) -> dict:
    row = await get_agent(owner_id, agent_id)
    if row is None or row["ownerUserId"] != user_id:
        raise ApiProblem("MEMBER_NOT_FOUND", "No such webhook agent of yours.")
    return row


async def _delete(owner_id: str, row: dict) -> None:
    sm = _sm()
    try:
        await run_in_thread(
            sm.delete_secret, SecretId=secret_name(owner_id, row["agentId"]), ForceDeleteWithoutRecovery=True
        )
    except sm.exceptions.ResourceNotFoundException:
        pass
    table = repo._table()
    await run_in_thread(table.delete_item, Key={"pk": row["pk"], "sk": row["sk"]})  # unreachable from here on
    for send in await repo._query(repo.owner_pk(owner_id), begins_with=f"WEBHOOKSEND#{row['agentId']}#"):
        await run_in_thread(table.delete_item, Key={"pk": send["pk"], "sk": send["sk"]})


async def remove(owner_id: str, user_id: str, agent_id: str) -> None:
    await _delete(owner_id, await _owned(owner_id, user_id, agent_id))
    logger.info("webhook agent removed owner=%s user=%s agent=%s", owner_id, user_id, agent_id)


async def delete_for_member(owner_id: str, user_id: str) -> None:
    """Membership removal deletes the person's webhook agents (and their key)."""
    for row in await list_agents(owner_id):
        if row["ownerUserId"] == user_id:
            await _delete(owner_id, row)


# ---- delivery ------------------------------------------------------------


async def _credential(owner_id: str, agent_id: str) -> dict:
    sm = _sm()
    value = await run_in_thread(sm.get_secret_value, SecretId=secret_name(owner_id, agent_id))
    return json.loads(value["SecretString"])


async def post(owner_id: str, agent_id: str, payload: dict) -> tuple[bool, str]:
    """ONE webhook call with a bounded retry: (delivered, why not). The URL's host must resolve to
    public addresses only, and the connection goes to the address checked (link_preview's guard)."""
    try:
        cred = await _credential(owner_id, agent_id)
    except Exception:  # noqa: BLE001 -- a missing secret is a failed delivery, not a crash
        logger.warning("webhook agent credential unavailable agent=%s", agent_id)
        return False, "credential unavailable"
    reason = "unreachable"
    async with httpx.AsyncClient(
        transport=_TRANSPORT, timeout=TIMEOUT_S, follow_redirects=False, trust_env=False
    ) as client:
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                check_url(cred["url"])
                request = await link_preview.pinned_request(
                    client,
                    "POST",
                    cred["url"],
                    headers={"Authorization": f"Bearer {cred['key']}", "User-Agent": "Lightbulb-Webhook/1.0"},
                    json=payload,
                )
                response = await client.send(request)
            except (ApiProblem, link_preview.Refused):
                return False, "webhook URL refused"
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            else:
                if response.is_success:
                    return True, ""
                reason = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    return False, reason  # the webhook said no: retrying will not change that
            if attempt < len(RETRY_DELAYS_S):
                await asyncio.sleep(RETRY_DELAYS_S[attempt])
    logger.info("webhook agent delivery failed agent=%s reason=%s", agent_id, reason)
    return False, reason


def _reply_hint(send: dict, agent: dict) -> dict:
    """How the agent answers: the hosted MCP's send_message, to whoever wrote."""
    from core.services.hosted_mcp import RESOURCE

    if send["actorUserId"] != agent["ownerUserId"]:
        to = {"person_id": send["actorUserId"]}
    else:
        to = {}  # your own message from an OAuth client: nothing to address but a room
    where = ", ".join(f"{k}={v}" for k, v in to.items()) or "a room_id from find_rooms"
    return {
        "mcp": RESOURCE,
        "tool": "send_message",
        "arguments": to,
        "text": f"To reply, call send_message on the Lightbulb MCP ({RESOURCE}) with {where} and a new request_id.",
    }


def payload(send: dict, agent: dict, text: str) -> dict:
    body = defang(text)
    sender = send["from"]
    hint = _reply_hint(send, agent)
    return {
        "type": "lightbulb.message",
        "agentId": agent["agentId"],
        "requestId": send["requestId"],
        "from": sender,
        "text": body,
        # The whole thing as one prompt, for a routine that passes the body through as text.
        "prompt": f"{HEADER_PREFIX} from {_clean(sender)}]\n{body}\n\n{hint['text']}",
        "reply": hint,
    }


async def _deliver(owner_id: str, agent: dict, send: dict, text: str) -> dict:
    delivered, reason = await post(owner_id, agent["agentId"], payload(send, agent, text))
    state = "delivered" if delivered else "failed"
    updated = await run_in_thread(
        repo._table().update_item,
        Key={"pk": send["pk"], "sk": send["sk"]},
        UpdateExpression="SET #s = :s, reason = :r, settledAt = :t REMOVE #text",
        ExpressionAttributeNames={"#s": "state", "#text": "text"},
        ExpressionAttributeValues={":s": state, ":r": reason, ":t": int(time.time())},
        ReturnValues="ALL_NEW",
    )
    return updated["Attributes"]


def ask_item_id(agent_id: str, sender: str, ask_id: str) -> str:
    """The Activity item for one person's ask to reach a webhook agent (``activity.run_action``)."""
    return f"agenthook:{agent_id}:{sender}:{ask_id}"


async def _invalidate_activity(owner_id: str, user_id: str) -> None:
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    try:
        await get_live_event_publisher().publish_user(
            owner_id, user_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"})
        )
    except Exception:  # noqa: BLE001 -- the badge catches up on the next focus
        logger.warning("webhook agent activity live event failed owner=%s", owner_id)


async def _held(owner_id: str, agent_id: str, sender: str | None = None) -> list[dict]:
    now = time.time()
    return sorted(
        (
            s
            for s in await repo._query(repo.owner_pk(owner_id), begins_with=f"WEBHOOKSEND#{agent_id}#")
            if s["state"] == "awaiting_approval"
            and int(s.get("expiresAt") or 0) > now
            and (sender is None or s["actorUserId"] == sender)
        ),
        key=lambda s: (s["submittedAt"], s.get("submittedNs", 0)),
    )


async def decide(owner_id: str, user_id: str, agent_id: str, sender: str, decision: str) -> int:
    """The agent's person answers a held sender, from Settings or the Activity item alike: ``send``
    delivers what waited (oldest first) and nothing more; ``allow`` also lets them (and so all their
    agents) straight in from now on; ``decline`` drops what waited. Returns how many."""
    if decision not in ("send", "allow", "decline"):
        raise ApiProblem("INVALID_REQUEST", "Answer send, allow or decline.")
    agent = await _owned(owner_id, user_id, agent_id)
    table = repo._table()
    if decision == "allow" and sender not in (agent.get("allowedSenders") or []):
        await run_in_thread(
            table.update_item,
            Key={"pk": agent["pk"], "sk": agent["sk"]},
            UpdateExpression="SET allowedSenders = list_append(if_not_exists(allowedSenders, :none), :one)",
            ExpressionAttributeValues={":none": [], ":one": [sender]},
        )
    count = 0
    for held in await _held(owner_id, agent_id, sender):
        try:  # claim it, so a second Allow never posts it again
            await run_in_thread(
                table.update_item,
                Key={"pk": held["pk"], "sk": held["sk"]},
                UpdateExpression="SET #s = :s",
                ConditionExpression="#s = :held",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":s": "declined" if decision == "decline" else "sending",
                    ":held": "awaiting_approval",
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue
        if decision != "decline":
            await _deliver(owner_id, agent, held, held.get("text") or "")
        else:
            await run_in_thread(
                table.update_item,
                Key={"pk": held["pk"], "sk": held["sk"]},
                UpdateExpression="REMOVE #text",
                ExpressionAttributeNames={"#text": "text"},
            )
        count += 1
    if count:
        await _invalidate_activity(owner_id, user_id)  # the Needs-you item now reads answered
    return count


async def ask_view(owner_id: str, agent_id: str, sender: str, ask_id: str) -> tuple[dict, list[dict]] | None:
    """(the agent, this person's sends still held) for one ask, or None when the agent is gone.
    The ask is open while the send that filed it is still held."""
    agent = await get_agent(owner_id, agent_id)
    if agent is None:
        return None
    held = await _held(owner_id, agent_id, sender)
    return agent, [s for s in held if (s.get("askId") or s["requestId"]) == ask_id]


async def remove_sender(owner_id: str, user_id: str, agent_id: str, sender: str) -> None:
    agent = await _owned(owner_id, user_id, agent_id)
    kept = [u for u in agent.get("allowedSenders") or [] if u != sender]
    await run_in_thread(
        repo._table().update_item,
        Key={"pk": agent["pk"], "sk": agent["sk"]},
        UpdateExpression="SET allowedSenders = :kept",
        ExpressionAttributeValues={":kept": kept},
    )


async def ping(owner_id: str, user_id: str, agent_id: str) -> dict:
    agent = await _owned(owner_id, user_id, agent_id)
    delivered, reason = await post(
        owner_id,
        agent_id,
        {
            "type": "lightbulb.ping",
            "agentId": agent_id,
            "text": f"Test ping from Lightbulb for {agent['name']}. Nothing to do.",
        },
    )
    return {"delivered": delivered, **({"reason": reason} if reason else {})}
