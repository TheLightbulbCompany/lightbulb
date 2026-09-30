"""Voice calls: one containers-table row per live call, and the ONE close seam.

A call leaves nothing behind: ``close_call`` bills and deletes the row in one transaction.
The row sits on the sparse ``status-index`` while ``call_open`` / ``call_closing`` so the
sweeper finds it with one query.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.billing import metronome_client, voice_billing
from core.billing import metronome_fast_gate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import openai_live, runtime_transport
from core.services.live_events import get_live_event_publisher

logger = logging.getLogger(__name__)

CALL_KEY_PREFIX = "__call__#"
OPEN = "call_open"
CLOSING = "call_closing"


def _table():
    return get_table("containers")


def _key(call_id: str) -> dict:
    return {"owner_id": f"{CALL_KEY_PREFIX}{call_id}"}


def _fast_gate_table_name() -> str:
    return metronome_fast_gate._table().name


def _metronome(http: httpx.AsyncClient):
    return metronome_client.for_settings(http)


async def put_call(call: dict) -> None:
    await run_in_thread(
        _table().put_item, Item={**_key(call["callId"]), **call}, ConditionExpression="attribute_not_exists(owner_id)"
    )


async def get_call(call_id: str) -> dict | None:
    return (await run_in_thread(_table().get_item, Key=_key(call_id), ConsistentRead=True)).get("Item")


async def set_turn(call_id: str, run_id: str) -> None:
    await run_in_thread(
        _table().update_item,
        Key=_key(call_id),
        UpdateExpression="SET turnRunId = :r",
        ConditionExpression="#s = :open",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": run_id, ":open": OPEN},
    )


async def release_hold(owner_id: str, hold: int) -> None:
    """Used only when a call never started (OpenAI refused the create)."""
    if hold <= 0:
        return
    await run_in_thread(
        metronome_fast_gate._table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET openrouter_reserved_microcents = openrouter_reserved_microcents - :h",
        ConditionExpression="attribute_exists(owner_id) AND openrouter_reserved_microcents >= :h",
        ExpressionAttributeValues={":h": hold},
    )


async def _claim(call_id: str, now: int) -> dict | None:
    """open → closing, or re-claim a closing row whose closer died >5 min ago."""
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key=_key(call_id),
            UpdateExpression="SET #s = :closing, closingAt = :now",
            ConditionExpression="#s = :open OR (#s = :closing AND closingAt < :stale)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":closing": CLOSING, ":open": OPEN, ":now": now, ":stale": now - 300},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


async def close_call(call_id: str, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    call = await _claim(call_id, now)
    if call is None:
        return False
    await _stop_turn(call)
    seconds = call.get("usageSeconds")
    if seconds is None:
        seconds = await openai_live.close_session(call["sessionId"])
        if seconds is None:
            # ponytail: fallback bills wall-clock to the deadline; the dev spike may switch this to 0.
            put_metric("voice.usage.fallback")
            seconds = max(0, min(now, int(call["deadline"])) - int(call["startedAt"]))
        # Persist so a re-claim bills the same number instead of recomputing with a later `now`.
        await run_in_thread(
            _table().update_item,
            Key=_key(call_id),
            UpdateExpression="SET usageSeconds = :s",
            ConditionExpression="#s = :closing",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": int(seconds), ":closing": CLOSING},
        )
    await _bill(call, int(seconds))
    return True


def _failed_on_first(exc: ClientError) -> bool:
    """True when a cancelled transaction failed on item 0 (the counter / fast-gate), not the call-row Delete."""
    if exc.response["Error"]["Code"] != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or []
    return bool(reasons) and reasons[0].get("Code") == "ConditionalCheckFailed"


async def _bill(call: dict, seconds: int) -> None:
    owner = call["ownerId"]
    call_key = {"owner_id": {"S": f"{CALL_KEY_PREFIX}{call['callId']}"}}
    delete = {
        "Delete": {
            "TableName": _table().name,
            "Key": call_key,
            "ConditionExpression": "#s = :closing",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":closing": {"S": CLOSING}},
        }
    }
    if call["billing"] == "credits":
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
            await _metronome(http).ingest(
                [
                    {
                        "transaction_id": call["callId"],
                        "customer_id": owner,
                        "event_type": "voice_call",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(call["startedAt"]))),
                        "properties": {
                            "provider_cost_nanodollars": str(voice_billing.provider_nanodollars(seconds)),
                            "seconds": str(seconds),
                            "agent_id": call["agentId"],
                        },
                    }
                ]
            )
        first = {
            "Update": {
                "TableName": _fast_gate_table_name(),
                "Key": {"owner_id": {"S": owner}},
                "UpdateExpression": "SET openrouter_reserved_microcents = openrouter_reserved_microcents - :hold ADD remaining_microcents :debit",
                "ConditionExpression": "attribute_exists(owner_id) AND openrouter_reserved_microcents >= :hold",
                "ExpressionAttributeValues": {
                    ":debit": {"N": str(-voice_billing.charge_microcents(seconds))},
                    ":hold": {"N": str(int(call["holdMicrocents"]))},
                },
            }
        }
    else:
        today = voice_billing.trial_cap.utc_today()
        first = {
            "Update": {
                "TableName": _table().name,
                "Key": {"owner_id": {"S": f"{voice_billing.FREE_KEY_PREFIX}{owner}"}},
                "UpdateExpression": "SET usedSeconds = if_not_exists(usedSeconds, :zero) + :s, #d = :day",
                "ConditionExpression": "attribute_not_exists(#d) OR #d = :day",
                "ExpressionAttributeNames": {"#d": "day"},
                "ExpressionAttributeValues": {":s": {"N": str(seconds)}, ":zero": {"N": "0"}, ":day": {"S": today}},
            }
        }
    tx = _table().meta.client.transact_write_items
    try:
        await run_in_thread(tx, TransactItems=[first, delete])
    except ClientError as exc:
        if not _failed_on_first(exc):
            raise
        if call["billing"] == "free":
            # Counter still holds yesterday: reset it to this call's seconds.
            first["Update"]["UpdateExpression"] = "SET usedSeconds = :s, #d = :day"
            first["Update"]["ConditionExpression"] = "#d <> :day"
            first["Update"]["ExpressionAttributeValues"].pop(":zero")
            await run_in_thread(tx, TransactItems=[first, delete])
        else:
            # Fast-gate row gone or hold already released: Metronome (the ledger) has the charge and the
            # row is a cache the next refresh overwrites, so just finish the close.
            put_metric("voice.bill.gate_skipped")
            await run_in_thread(tx, TransactItems=[delete])
    put_metric("voice.call.closed", dimensions={"billing": call["billing"]})


_BRIDGE_RUNS = "/internal/calls/v1/runs"
# API Gateway cuts at 30 s. runtime_transport adds read margin min(5, t) plus wait_for +2, so
# 18 + 5 + 2 = 25 s. The call start pre-wakes the runtime.
_TURN_TIMEOUT_S = 18.0
_TRANSCRIPT_CHARS = 6000
_background: set[asyncio.Task] = set()

PERSONA = """You are {agent}, speaking with a teammate on a live voice call. Speak naturally at an unhurried pace. Be clear and direct, not overly cheerful. If they are frustrated, acknowledge it briefly and focus on the next helpful step.

Backchannel policy: use moderate backchannels ("mm-hm", "got it") while they talk.
Interruption policy: stop speaking when they interrupt, and listen.

Delegation policy: you have one backend, {agent}'s full agent: it can run commands, browse the web, read and edit files, file and update issues, remember things across sessions, and reason carefully. It does the real work and knows the facts. Delegate for any fact, current information or reasoning, for any do/check/find/make/fix/run/remember request, and for corrections to work already requested. Do not delegate greetings, small talk, repeating a result already given, or brief clarifications. Delegate before giving an answer that depends on backend work. Never guess a result while waiting: say briefly that you are checking, then wait."""

TURN_NOTE = """[Note: this request comes from a live spoken conversation. The text is a voice transcript (it may contain mis-hearings, hesitations and later corrections; use the latest intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain conversational sentences, keep it short (a few sentences unless the user asked for detail), no markdown, no lists, no code blocks, no URLs read out character by character. Do the work with your tools as usual; only the final facts need to be spoken. Do not claim an action succeeded before it actually did.]"""


def _problem(status: int, **detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def _company_id(owner_id: str) -> str:
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    if row is None:
        raise _problem(409, code="workspace_not_ready")
    return row.company_id


async def _verify_agent(owner_id: str, company_id: str, agent_id: str) -> None:
    from routers.messaging import _verify_paperclip_hermes_identity  # the one agent-belongs-to-company check

    await _verify_paperclip_hermes_identity(owner_id, {"companyId": company_id, "agentId": agent_id}, full=True)


async def _frame_channel(owner_id: str, user_id: str) -> str:
    channel = await get_live_event_publisher().user_channel(owner_id, user_id)
    if channel is None:
        raise _problem(409, code="live_channel_missing")
    return channel


async def _hold(owner_id: str, seconds: int) -> int:
    # ponytail: read-then-hold is racy across two simultaneous starts; the gate's own reservation
    # and the next Metronome refresh bound the overspend to one call. Upgrade: conditional hold.
    hold = voice_billing.charge_microcents(seconds)
    await run_in_thread(
        metronome_fast_gate._table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET openrouter_reserved_microcents = if_not_exists(openrouter_reserved_microcents, :z) + :h",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":h": hold, ":z": 0},
    )
    return hold


def _identity(call: dict, run_id: str) -> dict:
    # Must equal the bridge's turn.call_session_key: a call has its own Hermes session, not a room's
    # and not a Paperclip run's.
    return {
        "runId": run_id,
        "companyId": call["companyId"],
        "agentId": call["agentId"],
        "callId": call["callId"],
        "sessionId": f"agent:{call['agentId']}:lightbulb:call:{call['callId']}",
    }


async def _runtime(call: dict, path: str, body: dict, *, timeout_s: float = _TURN_TIMEOUT_S):
    container = await container_repo.get_by_owner_id(call["ownerId"])
    if not container:
        raise _problem(409, code="workspace_not_ready")
    return await runtime_transport.request(
        container,
        path,
        method="POST",
        json_body=body,
        headers={"Authorization": f"Bearer {container.get('gateway_token') or ''}"},
        timeout_s=timeout_s,
    )


def _prewake(call: dict) -> None:
    """Stopping this call's (not-yet-existing) run wakes the microVM; the bridge's 404 is expected."""

    async def wake():
        try:
            identity = _identity(call, call["callId"])
            container = await container_repo.get_by_owner_id(call["ownerId"])
            if container:
                await runtime_transport.request(
                    container,
                    f"{_BRIDGE_RUNS}/{call['callId']}/stop",
                    method="POST",
                    json_body={"identity": identity},
                    headers={"Authorization": f"Bearer {container.get('gateway_token') or ''}"},
                    timeout_s=runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S,
                )
        except Exception:  # noqa: BLE001 -- best effort; the first turn wakes it otherwise
            logger.info("voice prewake failed", exc_info=True)

    task = asyncio.create_task(wake())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def start_call(*, owner_id: str, user_id: str, agent_id: str, agent_name: str, sdp: str) -> dict:
    if not settings.OPENAI_API_KEY:
        raise _problem(503, code="voice_unavailable")
    mode = voice_billing.billing_mode(await billing_repo.get_by_owner_id(owner_id))
    if mode is None:
        raise _problem(402, code="voice_limit", reason="plan_inactive")
    budget = await voice_billing.budget_seconds(owner_id, mode)
    if budget < voice_billing.MIN_CALL_SECONDS:
        raise _problem(402, code="voice_limit", reason="free_minutes_used" if mode == "free" else "credits_low")
    company_id = await _company_id(owner_id)
    await _verify_agent(owner_id, company_id, agent_id)
    await _frame_channel(owner_id, user_id)  # fail before paying for a session the client can't hear results on
    hold = await _hold(owner_id, budget) if mode == "credits" else 0
    try:
        session_id, answer = await openai_live.create_session(sdp=sdp, instructions=PERSONA.format(agent=agent_name))
    except (openai_live.LiveUpstreamError, httpx.HTTPError, KeyError, ValueError):
        await release_hold(owner_id, hold)
        raise _problem(502, code="voice_upstream")
    now = int(time.time())
    call = {
        "callId": str(uuid.uuid4()),
        "ownerId": owner_id,
        "userId": user_id,
        "agentId": agent_id,
        "companyId": company_id,
        "sessionId": session_id,
        "startedAt": now,
        "deadline": now + budget,
        "billing": mode,
        "holdMicrocents": hold,
        "status": OPEN,
        "turnRunId": None,
    }
    try:
        await put_call(call)
    except Exception:
        # No row means nothing would ever close or bill this session: undo both, then surface the error.
        await release_hold(owner_id, hold)
        try:
            await openai_live.close_session(session_id)
        except Exception:  # noqa: BLE001 -- best effort
            logger.warning("voice session close after put_call failure failed", exc_info=True)
        raise
    _prewake(call)
    return {"callId": call["callId"], "sdp": answer, "deadline": call["deadline"]}


async def _own_open_call(call_id: str, owner_id: str, user_id: str) -> dict:
    call = await get_call(call_id)
    if not call or call["ownerId"] != owner_id or call["userId"] != user_id or call["status"] != OPEN:
        raise _problem(404, code="call_not_found")
    return call


async def _stop_turn(call: dict) -> None:  # replaces the Task 4 no-op
    run_id = call.get("turnRunId")
    if not run_id:
        return
    try:
        await _runtime(call, f"{_BRIDGE_RUNS}/{run_id}/stop", {"identity": _identity(call, run_id)}, timeout_s=10.0)
    except Exception:  # noqa: BLE001 -- a turn we can't stop finishes and its frames are dropped client-side
        logger.info("voice turn stop failed", exc_info=True)


async def start_turn(*, call_id: str, owner_id: str, user_id: str, utterance: str, transcript: str) -> dict:
    call = await _own_open_call(call_id, owner_id, user_id)
    channel = await _frame_channel(owner_id, user_id)  # before the stop: a 409 must not kill the old turn
    # Never awaited by the request: a stop can take 10+ s and the turn must answer inside 30 s.
    task = asyncio.create_task(_stop_turn(call))
    _background.add(task)
    task.add_done_callback(_background.discard)
    run_id = str(uuid.uuid4())
    identity = _identity(call, run_id)
    context = transcript[-_TRANSCRIPT_CHARS:].strip()
    text = (f"[Recent spoken conversation, newest last:\n{context}]\n\n" if context else "") + utterance
    body = {
        "identity": identity,
        "request": {
            "input": text,
            "instructions": TURN_NOTE,
            "session_id": identity["sessionId"],
            "frames": {
                "endpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
                "channel": channel,
            },
        },
    }
    try:
        resp = await _runtime(call, _BRIDGE_RUNS, body)
    except (httpx.HTTPError, ValueError):
        raise _problem(503, code="runtime_unavailable")
    if resp.status_code not in (200, 202):
        raise _problem(503, code="runtime_unavailable")
    await set_turn(call_id, run_id)
    return {"turnId": run_id}


async def end_call(*, call_id: str, owner_id: str, user_id: str) -> dict:
    await _own_open_call(call_id, owner_id, user_id)
    await close_call(call_id)
    return {"ended": True}
