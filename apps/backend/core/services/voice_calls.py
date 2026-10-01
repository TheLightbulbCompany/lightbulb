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
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from core.billing import metronome_client, voice_billing
from core.billing import metronome_fast_gate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import jev, messaging, openai_live, runtime_transport, voice_persona, voice_routing
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


async def _claim_turn(call_id: str, agent_id: str, run_id: str, seq: int) -> str | None:
    """Make run_id this agent's turn if seq is newer than that agent's holder. Returns the old run id."""
    resp = await run_in_thread(
        _table().update_item,
        Key=_key(call_id),
        UpdateExpression="SET turns.#a = :t",
        ConditionExpression="#s = :open AND (attribute_not_exists(turns.#a) OR turns.#a.seq < :seq)",
        ExpressionAttributeNames={"#s": "status", "#a": agent_id},
        ExpressionAttributeValues={":t": {"runId": run_id, "seq": seq}, ":seq": seq, ":open": OPEN},
        ReturnValues="ALL_OLD",
    )
    return (((resp.get("Attributes") or {}).get("turns") or {}).get(agent_id) or {}).get("runId")


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


def _free_row_key(owner_id: str) -> dict:
    return {"owner_id": f"{voice_billing.FREE_KEY_PREFIX}{owner_id}"}


async def _claim_free(owner_id: str, call_id: str, now: int, until: int) -> None:
    # ponytail: one concurrent free call per workspace caps free minutes at the daily budget;
    # upgrade path: reserved-seconds accounting so parallel free calls can share it.
    try:
        await run_in_thread(
            _table().update_item,
            Key=_free_row_key(owner_id),
            UpdateExpression="SET openCallId = :c, openCallUntil = :until",
            # Self-expiring with the call itself: a leaked claim can't outlive its call's budget + grace.
            ConditionExpression="attribute_not_exists(openCallId) OR openCallUntil < :now",
            ExpressionAttributeValues={
                ":c": call_id,
                ":now": now,
                ":until": until,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise _problem(409, code="voice_busy")
        raise


async def _release_free(owner_id: str, call_id: str) -> None:
    try:
        await run_in_thread(
            _table().update_item,
            Key=_free_row_key(owner_id),
            UpdateExpression="REMOVE openCallId, openCallUntil",
            ConditionExpression="openCallId = :c",
            ExpressionAttributeValues={":c": call_id},
        )
    except Exception:  # noqa: BLE001 -- best effort; a leaked claim blocks free calls until the row is cleared
        logger.warning("voice free-claim release failed", exc_info=True)


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


async def _publish_call_changed(call: dict, state: str) -> None:
    if call.get("kind") != "room" or not call.get("roomId"):
        return
    try:
        await get_live_event_publisher().publish_room(
            call["roomId"],
            messaging.make_live_event(
                "room.call.changed",
                {"roomId": call["roomId"], "callId": call["callId"], "userId": call["userId"], "state": state},
                room_id=call["roomId"],
            ),
        )
    except Exception:  # noqa: BLE001 -- the marker is cosmetic; never fail a call over it
        logger.info("voice call marker publish failed", exc_info=True)


async def active_callers(owner_id: str, user_id: str, room_id: str) -> list[dict]:
    meta, _ = await messaging.require_room_member(owner_id, user_id, room_id)
    # Calls are owned by the room's workspace, so a vendor shared-channel guest filters on that, not their own.
    room_owner = meta.get("ownerId", owner_id)
    rows = await container_repo.get_by_status(OPEN)
    return [
        {"callId": r["callId"], "userId": r["userId"]}
        for r in rows
        # A huddle summon is a call row too, but its marker is the huddle's own, not a caller's.
        if r.get("ownerId") == room_owner and r.get("roomId") == room_id and r.get("kind") != "huddle"
    ]


async def close_call(call_id: str, *, now: int | None = None, usage_seconds: int | None = None) -> bool:
    """usage_seconds: the huddle worker's GPT-Live seconds, capped at the call's budget."""
    if usage_seconds is not None:
        usage_seconds = max(0, int(usage_seconds))  # before the claim: a bad value must not strand a CLOSING row
    now = int(time.time()) if now is None else now
    call = await _claim(call_id, now)
    if call is None:
        return False
    _fire(_stop_all_turns(call))  # billing doesn't depend on it, and /end must answer inside 30 s
    seconds = call.get("usageSeconds")
    if seconds is None:
        if usage_seconds is not None:
            seconds = min(usage_seconds, int(call["deadline"]) - int(call["startedAt"]))
        elif call.get("kind") == "huddle":
            # Only the worker knows GPT-Live seconds; none reported (crash, deadline sweep) bills nothing.
            put_metric("voice.huddle.usage_missing")
            seconds = 0
        else:
            seconds = await openai_live.close_session(call["sessionId"])
        if seconds is None:
            # ponytail: fallback bills wall-clock to the deadline; the dev spike may switch this to 0.
            put_metric("voice.usage.fallback")
            seconds = max(0, min(now, int(call["deadline"])) - int(call["startedAt"]))
        # Persist so a re-claim bills the same number instead of recomputing with a later `now`.
        # Best effort: we already hold the authoritative seconds, so a failed write must not lose them.
        try:
            await run_in_thread(
                _table().update_item,
                Key=_key(call_id),
                UpdateExpression="SET usageSeconds = :s",
                ConditionExpression="#s = :closing",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": int(seconds), ":closing": CLOSING},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise  # another closer already finished: stop, as before
            put_metric("voice.usage.persist_failed")
            logger.warning("voice usage persist failed call=%s", call_id, exc_info=True)
        except Exception:  # noqa: BLE001
            put_metric("voice.usage.persist_failed")
            logger.warning("voice usage persist failed call=%s", call_id, exc_info=True)
    await _publish_call_changed(
        call, "ended"
    )  # best effort, before billing: a failed bill must not leave the marker up
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
    call_key = {"owner_id": f"{CALL_KEY_PREFIX}{call['callId']}"}
    delete = {
        "Delete": {
            "TableName": _table().name,
            "Key": call_key,
            "ConditionExpression": "#s = :closing",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":closing": CLOSING},
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
                "Key": {"owner_id": owner},
                "UpdateExpression": "SET openrouter_reserved_microcents = openrouter_reserved_microcents - :hold ADD remaining_microcents :debit",
                "ConditionExpression": "attribute_exists(owner_id) AND openrouter_reserved_microcents >= :hold",
                "ExpressionAttributeValues": {
                    ":debit": -voice_billing.charge_microcents(seconds),
                    ":hold": int(call["holdMicrocents"]),
                },
            }
        }
    else:
        today = voice_billing.trial_cap.utc_today()
        first = {
            "Update": {
                "TableName": _table().name,
                "Key": {"owner_id": f"{voice_billing.FREE_KEY_PREFIX}{owner}"},
                "UpdateExpression": "SET usedSeconds = if_not_exists(usedSeconds, :zero) + :s, #d = :day",
                "ConditionExpression": "attribute_not_exists(#d) OR #d = :day",
                "ExpressionAttributeNames": {"#d": "day"},
                "ExpressionAttributeValues": {":s": seconds, ":zero": 0, ":day": today},
            }
        }
    # Resource client: plain Python values (boto3 serializes them); typed {"S":..} would become a Map.
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
    if call["billing"] == "free":
        # Conditional on OUR callId: a stale-window takeover may have handed the claim to a newer call.
        await _release_free(owner, call["callId"])
    put_metric("voice.call.closed", dimensions={"billing": call["billing"]})


_BRIDGE_RUNS = "/internal/calls/v1/runs"
# API Gateway cuts at 30 s. runtime_transport adds read margin min(5, t) plus wait_for +2, so
# 18 + 5 + 2 = 25 s. The call start pre-wakes the runtime.
_TURN_TIMEOUT_S = 18.0
_ROUTING_TIMEOUT_S = 3.0  # routing shares the 30 s API Gateway budget with the dispatch
_TRANSCRIPT_CHARS = 6000
_background: set[asyncio.Task] = set()


def _fire(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


TURN_NOTE = """[Note: this request comes from a live spoken conversation. The text is a voice transcript (it may contain mis-hearings, hesitations and later corrections; use the latest intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain conversational sentences, keep it short (a few sentences unless the user asked for detail), no markdown, no lists, no code blocks, no URLs read out character by character. Do the work with your tools as usual; only the final facts need to be spoken. Do not claim an action succeeded before it actually did.]"""


def _problem(status: int, **detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def _owner_row(owner_id: str):
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    if row is None:
        raise _problem(409, code="workspace_not_ready")
    return row


async def _room_team(owner_id: str, user_id: str, room_id: str) -> tuple[dict, list[dict]]:
    meta, _ = await messaging.require_room_member(owner_id, user_id, room_id)
    if meta.get("ownerId") != owner_id:  # a shared-channel guest: the team is another workspace's agents
        raise _problem(404, code="room_not_found")
    if meta.get("archivedAt"):
        raise _problem(409, code="room_archived")
    team = [
        {"id": m["id"], "name": m.get("displayName") or "Agent"}
        for m in await messaging_repo.list_members(room_id)
        if m.get("kind") == "agent" and m.get("listenMode") != "observe"
    ]
    if not team:
        raise _problem(409, code="no_agents")
    return meta, team


async def _verify_agent(owner_id: str, company_id: str, agent_id: str) -> None:
    from routers.messaging import _verify_paperclip_hermes_identity  # the one agent-belongs-to-company check

    await _verify_paperclip_hermes_identity(owner_id, {"companyId": company_id, "agentId": agent_id}, full=True)


async def _frame_channel(owner_id: str, user_id: str) -> str:
    channel = await get_live_event_publisher().user_channel(owner_id, user_id)
    if channel is None:
        raise _problem(409, code="live_channel_missing")
    return channel


_HOLD_ATTEMPTS = 3


def _same(attr: str, value, values: dict, tag: str) -> str:
    """Condition clause: the attribute still equals what we read (or is still absent)."""
    if value is None:
        return f"attribute_not_exists({attr})"
    values[tag] = value
    return f"{attr} = {tag}"


async def _hold(owner_id: str) -> tuple[int, int]:
    """Reserve a call's credits by compare-and-set: DynamoDB can't sum attributes in a condition,
    so the hold lands only if the row still matches the snapshot the budget came from.
    Returns (hold microcents, budget seconds); (0, 0) when credits are too low."""
    for _ in range(_HOLD_ATTEMPTS):
        budget, row = await voice_billing.credit_snapshot(owner_id)
        if budget < voice_billing.MIN_CALL_SECONDS:
            return 0, 0
        hold = voice_billing.charge_microcents(budget)
        values: dict = {":h": hold}
        cond = " AND ".join(
            _same(attr, row.get(attr), values, f":v{i}")
            for i, attr in enumerate(("remaining_microcents", "reserved_microcents", "openrouter_reserved_microcents"))
        )
        try:
            await run_in_thread(
                metronome_fast_gate._table().update_item,
                Key={"owner_id": owner_id},
                UpdateExpression="SET openrouter_reserved_microcents = if_not_exists(openrouter_reserved_microcents, :z) + :h",
                ConditionExpression=cond,
                ExpressionAttributeValues={**values, ":z": 0},
            )
            return hold, budget
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise _problem(409, code="voice_busy")


def _identity(call: dict, run_id: str, agent_id: str) -> dict:
    # Must equal the bridge's turn.call_session_key: one Hermes session per (call, agent).
    return {
        "runId": run_id,
        "companyId": call["companyId"],
        "agentId": agent_id,
        "callId": call["callId"],
        "sessionId": f"agent:{agent_id}:lightbulb:call:{call['callId']}",
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
            identity = _identity(call, call["callId"], call["voiceAgentId"])
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

    _fire(wake())


async def _undo_start(owner_id: str, call_id: str, mode: str, hold: int, session_id: str | None = None) -> None:
    """Every cleanup step is independent: one failing must not skip the rest (or replace the caller's error)."""
    steps = [("hold release", release_hold(owner_id, hold))]
    if mode == "free":
        steps.append(("free-claim release", _release_free(owner_id, call_id)))
    if session_id:
        steps.append(("session close", openai_live.close_session(session_id)))
    for what, step in steps:
        try:
            await step
        except Exception:  # noqa: BLE001
            logger.warning("voice start cleanup: %s failed", what, exc_info=True)


async def _gate(owner_id: str) -> tuple[str, int]:
    """Plan + free-budget gates shared by every call start. Returns (billing mode, free budget seconds)."""
    if not openai_live.configured():
        raise _problem(503, code="voice_unavailable")
    mode = voice_billing.billing_mode(await billing_repo.get_by_owner_id(owner_id))
    if mode is None:
        raise _problem(402, code="voice_limit", reason="plan_inactive")
    budget = 0
    if mode == "free":
        budget = await voice_billing.budget_seconds(owner_id, mode)
        if budget < voice_billing.MIN_CALL_SECONDS:
            raise _problem(402, code="voice_limit", reason="free_minutes_used")
    return mode, budget


async def _room_persona(owner_id: str, voice_agent: dict, team: list[dict]) -> str:
    cards = await asyncio.gather(*(jev.agent_card(owner_id, t["id"], t["name"]) for t in team))
    return voice_persona.build_persona(
        voice_name=voice_agent["name"], team=[{**t, "card": c} for t, c in zip(team, cards)]
    )


async def start_call(
    *,
    owner_id: str,
    user_id: str,
    agent_id: str | None = None,
    agent_name: str | None = None,
    room_id: str | None = None,
    sdp: str,
) -> dict:
    mode, budget = await _gate(owner_id)
    row = await _owner_row(owner_id)
    company_id = row.company_id
    meta: dict = {}
    if room_id:
        meta, team = await _room_team(owner_id, user_id, room_id)
    else:
        await _verify_agent(owner_id, company_id, agent_id)
        team = [{"id": agent_id, "name": agent_name}]
    ceo = (getattr(row, "starter_agent_ids", None) or {}).get("ceo")
    voice_agent = next((t for t in team if t["id"] == ceo), team[0])
    if room_id:
        persona = await _room_persona(owner_id, voice_agent, team)
    else:
        persona = voice_persona.build_persona(voice_name=agent_name, team=None)
    await _frame_channel(owner_id, user_id)  # fail before paying for a session the client can't hear results on
    call_id = str(uuid.uuid4())
    if mode == "free":
        await _claim_free(owner_id, call_id, int(time.time()), int(time.time()) + budget + 180)
    hold = 0
    try:
        if mode == "credits":
            hold, budget = await _hold(owner_id)
            if not hold:
                raise _problem(402, code="voice_limit", reason="credits_low")
        session_id, answer = await openai_live.create_session(
            sdp=sdp, instructions=persona, voice=voice_persona.voice_for(voice_agent["id"], ceo)
        )
    except (openai_live.LiveUpstreamError, httpx.HTTPError, KeyError, ValueError):
        await _undo_start(owner_id, call_id, mode, hold)
        raise _problem(502, code="voice_upstream")
    except BaseException:
        await _undo_start(owner_id, call_id, mode, hold)
        raise
    now = int(time.time())
    call = {
        "callId": call_id,
        "ownerId": owner_id,
        "userId": user_id,
        "agentId": voice_agent["id"],
        "kind": "room" if room_id else "dm",
        "roomId": room_id,
        "roomName": meta.get("name"),
        "team": team,
        "voiceAgentId": voice_agent["id"],
        "ceoId": ceo,
        "turns": {},
        "companyId": company_id,
        "sessionId": session_id,
        "startedAt": now,
        "deadline": now + budget,
        "billing": mode,
        "holdMicrocents": hold,
        "status": OPEN,
    }
    try:
        await put_call(call)
    except BaseException:  # incl. CancelledError
        # No row means nothing would ever close or bill this session: undo everything, then surface the error.
        await _undo_start(owner_id, call_id, mode, hold, session_id)
        raise
    _prewake(call)
    await _publish_call_changed(call, "started")
    return {
        "callId": call["callId"],
        "sdp": answer,
        "deadline": call["deadline"],
        "kind": call["kind"],
        "voiceAgentId": voice_agent["id"],
        "voiceName": voice_agent["name"],
        "team": team,
    }


async def start_huddle_call(*, owner_id: str, room_id: str, speaker_user_id: str) -> dict:
    """A huddle summon: start_call's gates, hold and free claim, but the worker owns the GPT-Live
    session (no OpenAI session here) and frames go to the worker, not a user channel."""
    mode, budget = await _gate(owner_id)
    row = await _owner_row(owner_id)
    meta, team = await _room_team(owner_id, speaker_user_id, room_id)
    ceo = (getattr(row, "starter_agent_ids", None) or {}).get("ceo")
    voice_agent = next((t for t in team if t["id"] == ceo), team[0])
    persona = await _room_persona(owner_id, voice_agent, team)
    call_id = str(uuid.uuid4())
    if mode == "free":
        await _claim_free(owner_id, call_id, int(time.time()), int(time.time()) + budget + 180)
    hold = 0
    try:
        if mode == "credits":
            hold, budget = await _hold(owner_id)
            if not hold:
                raise _problem(402, code="voice_limit", reason="credits_low")
        now = int(time.time())
        call = {
            "callId": call_id,
            "ownerId": owner_id,
            "userId": speaker_user_id,
            "agentId": voice_agent["id"],
            "kind": "huddle",
            "roomId": room_id,
            "roomName": meta.get("name"),
            "team": team,
            "voiceAgentId": voice_agent["id"],
            "ceoId": ceo,
            "turns": {},
            "companyId": row.company_id,
            "sessionId": None,
            "frames": {"sink": "huddle", "roomId": room_id},
            "startedAt": now,
            "deadline": now + budget,
            "billing": mode,
            "holdMicrocents": hold,
            "status": OPEN,
        }
        await put_call(call)
    except BaseException:  # incl. CancelledError: no row means nothing would ever release these
        await _undo_start(owner_id, call_id, mode, hold)
        raise
    _prewake(call)
    return {
        "callId": call_id,
        "persona": persona,
        "voice": voice_persona.voice_for(voice_agent["id"], ceo),
        "voiceName": voice_agent["name"],
        "deadline": call["deadline"],
        "team": team,
    }


async def _own_open_call(call_id: str, owner_id: str, user_id: str) -> dict:
    call = await get_call(call_id)
    if (
        not call
        or call.get("kind") == "huddle"  # huddle rows are the worker's, via the internal routes only
        or call["ownerId"] != owner_id
        or call["userId"] != user_id
        or call["status"] != OPEN
    ):
        raise _problem(404, code="call_not_found")
    return call


async def _stop_turn(call: dict, agent_id: str, run_id: str | None) -> None:
    if not run_id:
        return
    try:
        await _runtime(
            call, f"{_BRIDGE_RUNS}/{run_id}/stop", {"identity": _identity(call, run_id, agent_id)}, timeout_s=10.0
        )
    except Exception:  # noqa: BLE001 -- a turn we can't stop finishes and its frames are dropped client-side
        logger.info("voice turn stop failed", exc_info=True)


async def _stop_all_turns(call: dict) -> None:
    turns = (call.get("turns") or {}).items()
    await asyncio.gather(*(_stop_turn(call, a, (t or {}).get("runId")) for a, t in turns), return_exceptions=True)


async def start_turn(*, call_id: str, owner_id: str, user_id: str, seq: int, utterance: str, transcript: str) -> dict:
    call = await _own_open_call(call_id, owner_id, user_id)
    channel = await _frame_channel(owner_id, user_id)  # before the claim: a 409 must not kill the old turn
    frames = {"endpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT, "channel": channel}
    return await _dispatch_turn(call, call, seq, utterance, transcript, frames)


async def start_huddle_turn(
    *, call_id: str, owner_id: str, speaker_user_id: str, seq: int, utterance: str, transcript: str
) -> dict:
    # Any huddle participant may speak; the worker vouches for the speaker, the route for the owner.
    call = await get_call(call_id)
    if not call or call.get("kind") != "huddle" or call["ownerId"] != owner_id or call["status"] != OPEN:
        raise _problem(404, code="call_not_found")
    base = settings.PUBLIC_API_BASE.rstrip("/")
    frames = {"sink": "backend", "endpoint": f"{base}/api/v1/internal/calls/{call_id}/frames"}
    # voice_routing only routes kind "room" (name -> Jev -> CEO); a huddle is a room conversation.
    return await _dispatch_turn(call, {**call, "kind": "room"}, seq, utterance, transcript, frames)


async def _dispatch_turn(call: dict, route_as: dict, seq: int, utterance: str, transcript: str, frames: dict) -> dict:
    call_id, owner_id = call["callId"], call["ownerId"]
    run_id = str(uuid.uuid4())
    try:
        agent_id = await asyncio.wait_for(
            voice_routing.pick(owner_id, route_as, utterance, transcript, call.get("ceoId")), _ROUTING_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        put_metric("voice.routing.timeout")
        ids = {m["id"] for m in call.get("team") or []}
        agent_id = call.get("ceoId") if call.get("ceoId") in ids else call["voiceAgentId"]

    def slot_run(row: dict | None) -> str | None:
        return (((row or {}).get("turns") or {}).get(agent_id) or {}).get("runId")

    # CLAIM -> STOP PREVIOUS -> DISPATCH -> VERIFY: overlapping turns each own a distinct run id, and the
    # loser of the final check stops its own run, so no dispatched run is ever untracked.
    def stop_both() -> None:
        # Ambiguous claim: the row may now name run_id (never dispatched) and the previous run is untracked.
        _fire(_stop_turn(call, agent_id, slot_run(call)))
        _fire(_stop_turn(call, agent_id, run_id))

    try:
        old = await _claim_turn(call_id, agent_id, run_id, seq)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            stop_both()
            raise _problem(503, code="runtime_unavailable")
        fresh = await get_call(call_id)
        if fresh and fresh.get("status") == OPEN and slot_run(fresh) == run_id:
            old = slot_run(call)  # our first attempt landed and only its response was lost
        elif not fresh or fresh.get("status") != OPEN:
            raise _problem(404, code="call_not_found")
        else:
            raise _problem(409, code="turn_conflict")  # a newer turn already holds the call
    except BotoCoreError:
        stop_both()
        raise _problem(503, code="runtime_unavailable")
    except BaseException:  # incl. CancelledError
        stop_both()
        raise
    # Never awaited by the request: a stop can take 10+ s and the turn must answer inside 30 s.
    if old:
        _fire(_stop_turn(call, agent_id, old))
    identity = _identity(call, run_id, agent_id)
    context = transcript[-_TRANSCRIPT_CHARS:].strip()
    text = (f"[Recent spoken conversation, newest last:\n{context}]\n\n" if context else "") + utterance
    body = {
        "identity": identity,
        "request": {
            "input": text,
            "instructions": TURN_NOTE,
            "session_id": identity["sessionId"],
            "frames": frames,
        },
    }
    try:
        resp = await _runtime(call, _BRIDGE_RUNS, body)
        ok = resp.status_code in (200, 202)
    except (httpx.HTTPError, ValueError):
        ok = False
    if not ok:
        # Keep the claim: the invoke may have been delivered (timeout on a cold wake), and close_call /
        # the next turn stop whatever the slot names. A stop for a run the bridge never got is a 404.
        _fire(_stop_turn(call, agent_id, run_id))
        raise _problem(503, code="runtime_unavailable")
    now = await get_call(call_id)
    if not now or now.get("status") != OPEN or slot_run(now) != run_id:
        _fire(_stop_turn(call, agent_id, run_id))  # hung up or superseded while dispatching
        if not now or now.get("status") != OPEN:
            raise _problem(404, code="call_not_found")
        raise _problem(409, code="turn_conflict")
    return {"turnId": run_id, "agentId": agent_id}


async def end_call(*, call_id: str, owner_id: str, user_id: str) -> dict:
    await _own_open_call(call_id, owner_id, user_id)
    await close_call(call_id)
    return {"ended": True}


async def wipe_owner_voice(owner_id: str) -> None:
    """Terminal wipe: end this owner's live calls without billing (the ledger is being archived)."""
    calls = [
        c for st in (OPEN, CLOSING) for c in await container_repo.get_by_status(st) if c.get("ownerId") == owner_id
    ]
    for call in calls:
        await _stop_all_turns(call)
        try:
            if call.get("sessionId"):  # a huddle summon has none
                await openai_live.close_session(call["sessionId"])
        except Exception:  # noqa: BLE001 -- best effort
            logger.warning("voice wipe session close failed call=%s", call.get("callId"), exc_info=True)
        await run_in_thread(_table().delete_item, Key=_key(call["callId"]))
        await _publish_call_changed(call, "ended")
    await run_in_thread(_table().delete_item, Key=_free_row_key(owner_id))


_SWEEP_SECONDS = 60


async def sweep_calls_once(now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    due = [c for c in await container_repo.get_by_status(OPEN) if int(c.get("deadline", 0)) <= now]
    due += [c for c in await container_repo.get_by_status(CLOSING) if int(c.get("closingAt", 0)) < now - 300]
    closed = 0
    for call in due:
        try:
            closed += bool(await close_call(call["callId"], now=now))
        except Exception:  # noqa: BLE001 -- one bad row never blocks the rest
            logger.warning("voice call close failed call=%s", call.get("callId"), exc_info=True)
    return closed


async def voice_call_sweeper(stop: asyncio.Event) -> None:
    from core.services.run_ledger import admitted

    while not stop.is_set():
        try:
            if await admitted("voice_calls", interval_s=_SWEEP_SECONDS):
                await sweep_calls_once()
        except Exception:  # noqa: BLE001
            logger.warning("voice call sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
