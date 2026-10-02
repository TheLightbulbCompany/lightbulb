"""Voice calls: one containers-table row per live call, and the ONE close seam.

A call leaves nothing behind: ``close_call`` bills and deletes the row in one transaction.
The row sits on the sparse ``status-index`` while ``call_open`` / ``call_closing`` so the
sweeper finds it with one query.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.billing import metronome_client, voice_billing
from core.billing import metronome_fast_gate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import (
    container_rpc,
    hermes_profiles,
    jev,
    livekit_huddles,
    messaging,
    openclaw_work,
    runtime_transport,
    voice_persona,
)
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


async def release_hold(owner_id: str, hold: int) -> None:
    """Used only when a call never started."""
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


async def close_call(call_id: str, *, now: int | None = None, usage_seconds: int | None = None) -> bool:
    """usage_seconds: the huddle worker's GPT-Live seconds, capped at the call's budget."""
    if usage_seconds is not None:
        usage_seconds = max(0, int(usage_seconds))  # before the claim: a bad value must not strand a CLOSING row
    now = int(time.time()) if now is None else now
    call = await _claim(call_id, now)
    if call is None:
        return False
    seconds = call.get("usageSeconds")
    if seconds is None:
        if usage_seconds is not None:
            seconds = min(usage_seconds, int(call["deadline"]) - int(call["startedAt"]))
        else:
            # Only the worker knows GPT-Live seconds; none reported (crash, deadline sweep) bills nothing.
            put_metric("voice.huddle.usage_missing")
            seconds = 0
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


# Static on purpose: Hermes splices `instructions` into the top of the system prompt, and the
# provider caches a byte-identical prefix. Everything per-run rides in the input.
# The script path is the image's (build-skills.sh maps /app/skills/ -> /opt/hermes-skills/); a
# run's shell has PAPERCLIP_COMPANY_ID / PAPERCLIP_AGENT_ID from the profile .env.
TURN_NOTE = """[Note: you are speaking in a Live voice call; your reply is spoken by you and nothing is posted to the room. A person is waiting to hear your answer, so speed matters more than completeness. The request is a voice transcript (it may contain mis-hearings, hesitations and later corrections; use the latest intent). The bracket before it is what was said on the call since you last spoke.
You decide how to handle it; pick whatever fits the request:
- Answer now: if you can answer from what you already know or with a couple of quick tool steps, do it and reply in two or three short spoken sentences.
- Ask: if the request is ambiguous, ask one short clarifying question instead of guessing.
- Hand it off: if it is real work (research, several lookups, writing or building something) that cannot fit in this turn, say so in one sentence and make sure it gets done by creating a Paperclip task for yourself or for the teammate who owns it:
  printf '%s' "<what to do>" | /opt/hermes-skills/paperclip/scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" --title "<short title>" --assignee "<your or the owner's agent id; yours is $PAPERCLIP_AGENT_ID>" --status todo
  Only promise to follow up if you actually created that task.
Your reply is spoken: no markdown, no lists, no code, no links, no ids. Never claim an action succeeded before it did.
This turn has a hard limit of a few model steps; when it runs out you must answer with what you have.]"""


def _problem(status: int, **detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def _owner_row(owner_id: str):
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    if row is None:
        raise _problem(409, code="workspace_not_ready")
    return row


async def room_agents(room_id: str) -> list[dict]:
    """Every agent member. Listen modes are a chat-traffic setting; Live ignores them (owner ruling 2026-10-02)."""
    return [
        {"id": m["id"], "name": m.get("displayName") or "Agent"}
        for m in await messaging_repo.list_members(room_id)
        if m.get("kind") == "agent"
    ]


async def _room_team(owner_id: str, user_id: str, room_id: str, *, need_team: bool = True) -> tuple[dict, list[dict]]:
    """The room's agents, any listen mode. Any room kind; a DM with an agent is a one-agent team.
    need_team=False (Live join) returns an empty team for a people-only room instead of 409."""
    meta, _ = await messaging.require_room_member(owner_id, user_id, room_id)
    if meta.get("ownerId") != owner_id:  # a shared-channel guest: the team is another workspace's agents
        raise _problem(404, code="room_not_found")
    if meta.get("archivedAt"):
        raise _problem(409, code="room_archived")
    team = await room_agents(room_id)
    if need_team and not team:
        raise _problem(409, code="no_agents")
    return meta, team


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


async def _runtime(owner_id: str, path: str, body: dict, *, timeout_s: float = _TURN_TIMEOUT_S):
    container = await container_repo.get_by_owner_id(owner_id)
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


def prewake_owner(owner_id: str, team: list[dict] | None = None) -> None:
    """Fire-and-forget wake of the workspace runtime (one health RPC), then a profile pull for every
    agent in the room (the bridge's profile.warm, off its request thread), so the first summon finds
    both warm. Pulse's first voice answer waited 23 s on its cold profile restore alone."""

    async def wake():
        try:
            await container_rpc.call(owner_id, "health")
        except Exception:  # noqa: BLE001 -- best effort; the first turn wakes it otherwise
            logger.info("voice owner prewake failed", exc_info=True)
            return
        for member in team or ():
            try:
                await container_rpc.call(owner_id, "profile.warm", {"agentId": member["id"]}, timeout_s=10.0)
            except Exception:  # noqa: BLE001 -- an old image answers "unavailable"; the summon restores
                logger.info("voice profile prewarm failed agent=%s", member["id"], exc_info=True)

    _fire(wake())


def prewarm_cards(owner_id: str, team: list[dict]) -> None:
    """Fire-and-forget: build every agent's Jev card (Paperclip + S3 reads) before the first /route."""

    async def warm():
        await asyncio.gather(*(jev.agent_card(owner_id, m["id"], m["name"]) for m in team), return_exceptions=True)

    _fire(warm())


async def _undo_start(owner_id: str, call_id: str, mode: str, hold: int) -> None:
    """Every cleanup step is independent: one failing must not skip the rest (or replace the caller's error)."""
    steps = [("hold release", release_hold(owner_id, hold))]
    if mode == "free":
        steps.append(("free-claim release", _release_free(owner_id, call_id)))
    for what, step in steps:
        try:
            await step
        except Exception:  # noqa: BLE001
            logger.warning("voice start cleanup: %s failed", what, exc_info=True)


def _live_configured() -> bool:
    # CDK seeds the secret with a random non-sk- value; that counts as unset (voice stays dark).
    return settings.OPENAI_API_KEY.startswith("sk-")


async def _gate(owner_id: str) -> tuple[str, int]:
    """Plan + free-budget gates shared by every call start. Returns (billing mode, free budget seconds)."""
    if not _live_configured():
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


async def _open_windows(owner_id: str, now: int) -> list[dict]:
    return [
        r
        for r in await container_repo.get_by_status(OPEN)
        if r.get("ownerId") == owner_id and r.get("kind") == "huddle" and int(r.get("deadline", 0)) > now
    ]


async def _open_window(owner_id: str, room_id: str, now: int) -> dict | None:
    # ponytail: one worker per room (dispatch dedupe); a per-room pointer row if summon can race
    return next((r for r in await _open_windows(owner_id, now) if r.get("roomId") == room_id), None)


async def open_voice_window(*, owner_id: str, room_id: str, speaker_user_id: str) -> dict:
    """C1 /summon: join the room's open window, or gate once and open one. The window meters
    GPT-Live only; voice runs are ordinary room runs and the window owns none of them."""
    # Before the join too: _room_team 404s unless owner_id owns the room and the speaker is a member.
    meta, _ = await _room_team(owner_id, speaker_user_id, room_id)
    now = int(time.time())
    # ponytail: a crashed worker's unreported GPT-Live seconds bill 0 (voice.huddle.usage_missing, as today);
    # persist running usage on the row if that metric is non-zero in prod
    existing = await _open_window(owner_id, room_id, now)
    if existing:
        prewake_owner(owner_id)
        return {
            "callId": existing["callId"],
            "deadline": int(existing["deadline"]),
            "budgetSeconds": int(existing["deadline"]) - now,  # a restarted worker never gets the full budget back
            "joined": True,
        }
    mode, budget = await _gate(owner_id)
    row = await _owner_row(owner_id)
    call_id = str(uuid.uuid4())
    if mode == "free":
        await _claim_free(owner_id, call_id, now, now + budget + 180)
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
            "kind": "huddle",
            "roomId": room_id,
            "roomName": meta.get("name"),
            "companyId": row.company_id,
            "ceoId": (getattr(row, "starter_agent_ids", None) or {}).get("ceo"),
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
    prewake_owner(owner_id)
    return {"callId": call_id, "deadline": call["deadline"], "budgetSeconds": budget, "joined": False}


# ---- Option D: the worker's per-agent reads (C1 /team and /agents/{agentId}/session) ---------

VOICE_ASKED_PREFIX = "__voiceasked__#"
_SESSION_READ_BUDGET_S = 2.0


async def _owned_room(owner_id: str, room_id: str) -> dict:
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("deletedAt") or meta.get("archivedAt") or meta.get("ownerId") != owner_id:
        raise _problem(404, code="room_not_found")  # a guest-owned (shared) room is another workspace's
    return meta


async def _starter_ids(owner_id: str) -> dict:
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    return dict(getattr(row, "starter_agent_ids", None) or {})


async def huddle_team(*, owner_id: str, room_id: str) -> dict:
    await _owned_room(owner_id, room_id)
    members, starters = await asyncio.gather(messaging_repo.list_members(room_id), _starter_ids(owner_id))
    agents = [
        {
            "id": m["id"],
            "name": m.get("displayName") or "Agent",
            "listenMode": m.get("listenMode") or "active",
            "defaultName": voice_persona.is_default_name(m["id"], m.get("displayName"), starters),
        }
        for m in members
        if m.get("kind") == "agent"
    ]
    people = [{"userId": m["id"], "name": m.get("displayName") or m["id"]} for m in members if m.get("kind") == "human"]
    return {"agents": agents, "people": people, "ceoId": starters.get("ceo")}


async def _claim_voice_asked(owner_id: str, agent_id: str) -> bool:
    # ponytail: one row per agent, never removed on agent delete or wipe (scan_all skips the __ prefix);
    # delete it in delete_agent_everywhere if rows pile up.
    try:
        await run_in_thread(
            _table().put_item,
            Item={"owner_id": f"{VOICE_ASKED_PREFIX}{owner_id}#{agent_id}", "askedAt": int(time.time())},
            ConditionExpression="attribute_not_exists(owner_id)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("voice asked-name claim failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
        return False  # fails closed: not asking for a name is harmless


async def _session_reads(owner_id: str, agent_id: str) -> tuple[dict | None, str | None, str | None]:
    """(roster row, SOUL.md, VOICE_NOTES.md); any read that fails or overruns the budget is None."""

    async def roster_row():
        return next((r for r in await messaging.load_agent_roster(owner_id) if r["id"] == agent_id), None)

    reads = asyncio.gather(
        roster_row(),
        run_in_thread(hermes_profiles.read_soul_md, owner_id, agent_id),
        run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id),
        return_exceptions=True,
    )
    try:
        results = await asyncio.wait_for(reads, _SESSION_READ_BUDGET_S)
    except asyncio.TimeoutError:
        logger.warning("voice session reads overran owner=%s agent=%s", owner_id, agent_id)
        return None, None, None
    row, soul, notes = (None if isinstance(r, BaseException) else r for r in results)
    return row, soul, notes


async def agent_session(*, owner_id: str, room_id: str, agent_id: str) -> dict:
    """One agent's GPT-Live session: its own voice and persona. Falls back to the bare persona
    (no role, SOUL or notes) when the reads fail or overrun."""
    meta = await _owned_room(owner_id, room_id)
    members, starters = await asyncio.gather(messaging_repo.list_members(room_id), _starter_ids(owner_id))
    me = next((m for m in members if m.get("kind") == "agent" and m["id"] == agent_id), None)
    if not me:
        raise _problem(409, code="agent_not_in_room")
    row, soul, notes_md = await _session_reads(owner_id, agent_id)
    name = (row or {}).get("name") or me.get("displayName") or "Agent"
    ask_name = voice_persona.is_default_name(agent_id, name, starters) and await _claim_voice_asked(owner_id, agent_id)
    persona = voice_persona.build_agent_persona(
        name=name,
        role=(row or {}).get("title") or (row or {}).get("role"),
        soul_excerpt=hermes_profiles.strip_generated_marker(soul) if soul else None,
        voice_notes=hermes_profiles.parse_voice_notes_md(notes_md)[1],
        room_name=meta.get("name"),
        people=[m.get("displayName") or m["id"] for m in members if m.get("kind") == "human"],
        agents=[m.get("displayName") or "Agent" for m in members if m.get("kind") == "agent" and m["id"] != agent_id],
        ask_name=ask_name,
    )
    return {
        "agentId": agent_id,
        "name": name,
        "voice": voice_persona.agent_voice(agent_id, starters),
        "persona": persona,
        "askName": ask_name,
    }


# ---- Option D: voice requests are runs on the agent's room session key (C1 /turns, /route, abort) ----

_STEER_TIMEOUT_S = 10.0


async def _speaker_name(speaker_id: str) -> str:
    try:
        return await messaging.human_display_name(speaker_id)
    except Exception:  # noqa: BLE001 -- a name is cosmetic; the run must still go out
        logger.info("voice speaker name unavailable", exc_info=True)
        return speaker_id


async def dispatch_voice_run(
    *, call: dict, run_id: str, agent_id: str, speaker_id: str, utterance: str, transcript: str
) -> dict:
    """One voice request = one ordinary run on agent:{agent}:lightbulb:channel:{room}. run_id is the
    worker's, reused on every retry, so a delivered-but-timed-out invoke is replayed, never re-run."""
    owner_id, room_id = call["ownerId"], call["roomId"]
    meta, members = await asyncio.gather(messaging_repo.get_room(room_id), messaging_repo.list_members(room_id))
    if not meta or not any(m.get("kind") == "agent" and m["id"] == agent_id for m in members):
        raise _problem(409, code="agent_not_in_room")
    name = await _speaker_name(speaker_id)
    spoken = transcript[-_TRANSCRIPT_CHARS:].strip()
    session_id = f"agent:{agent_id}:lightbulb:channel:{room_id}"  # == the bridge's turn.lightbulb_session_key
    body = {
        "identity": {
            "runId": run_id,
            "companyId": call["companyId"],
            "agentId": agent_id,
            "roomId": room_id,
            "sessionId": session_id,
            "family": "voice",
        },
        "request": {
            "input": f"[Live voice in #{meta.get('name') or 'this room'}. Spoken since you last spoke, newest last:\n"
            f"{spoken}]\n\n{name} (voice): {utterance}",
            "instructions": TURN_NOTE,
            "session_id": session_id,
            "frames": {
                "sink": "backend",
                "endpoint": f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/internal/huddles/{owner_id}/rooms/{room_id}/frames",
            },
            "voice": {
                "roomId": room_id,
                "roomKind": meta["kind"],
                "speaker": {"id": speaker_id, "name": name},
                "utterance": utterance,
            },
            "recentHistory": await openclaw_work.room_history(meta, int(meta.get("lastSeq") or 0) + 1),
            "participants": openclaw_work.ingress_participants(members),
        },
    }
    history = body["request"]["recentHistory"]
    while history and messaging.json_body_size(body) > openclaw_work._INGRESS_MAX_BYTES:
        history.pop(0)
    # Before the POST: the voice-profile route (C8) resolves the speaker from this row.
    await messaging_repo.put_voice_run(
        owner_id, run_id, {"roomId": room_id, "agentId": agent_id, "speakerUserId": speaker_id, "speakerName": name}
    )
    try:
        resp = await _runtime(owner_id, _BRIDGE_RUNS, body)
        ok = resp.status_code in (200, 202)  # 200 = replayed: a retry of a run the bridge already has
        if not ok:
            logger.warning("voice run refused run=%s status=%s body=%s", run_id, resp.status_code, resp.text[:500])
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("voice run dispatch failed run=%s: %s", run_id, type(exc).__name__)
        ok = False
    if not ok:
        # Nothing is stopped: a timed-out invoke may have been delivered, and the worker's retry with
        # this run_id is replayed by the bridge.
        raise _problem(503, code="runtime_unavailable")
    return {"runId": run_id, "agentId": agent_id, "steered": False}


async def _run_control(owner_id: str, run_id: str, action: str, body: dict) -> int | None:
    try:
        resp = await _runtime(
            owner_id, f"/plugins/lightbulb-channel/runs/{run_id}/{action}", body, timeout_s=_STEER_TIMEOUT_S
        )
        return resp.status_code
    except (httpx.HTTPError, ValueError):
        logger.info("voice run %s failed run=%s", action, run_id, exc_info=True)
        return None


async def steer_voice_run(*, call: dict, run_id: str, agent_id: str, speaker_id: str, utterance: str) -> dict | None:
    """A follow-up steers the agent's live voice run. None (dispatch a new run instead) when the run
    is not this room's and agent's, or the bridge no longer has it."""
    row = await messaging_repo.get_voice_run(call["ownerId"], run_id)
    if not row or row.get("roomId") != call["roomId"] or row.get("agentId") != agent_id:
        return None
    name = await _speaker_name(speaker_id)
    if await _run_control(call["ownerId"], run_id, "steer", {"text": f"{name} (voice): {utterance}"}) != 202:
        return None
    # The latest requester is who C8 checks: a guest's steer must not borrow the member's rights.
    await messaging_repo.put_voice_run(
        call["ownerId"], run_id, {**row, "speakerUserId": speaker_id, "speakerName": name}
    )
    return {"runId": run_id, "agentId": agent_id, "steered": True}


async def abort_voice_run(owner_id: str, room_id: str, run_id: str) -> bool:
    """Only an explicit "stop" aborts a voice run, and only one of this room's."""
    row = await messaging_repo.get_voice_run(owner_id, run_id)
    if not row or row.get("roomId") != room_id:
        return False
    return await _run_control(owner_id, run_id, "abort", {}) == 202


def _transcript_history(
    transcript: str, members: list[dict], utterance: str, at: list[int] | None = None
) -> list[dict]:
    """The Live transcript ("Name: text", or "Agent → Person: text" for a voice's answer) as chat
    history rows, ending before the line itself. An agent's line carries its id, so chat's fallback
    can find who was already in the conversation. ``to``: the arrow's person, else everyone the line
    names (the addressing stage's candidates -- Live's stand-in for stored @mentions; no call per row).
    ``at``: epoch seconds of the transcript's last lines, aligned to its end; each becomes that row's
    ``createdAt``, so Jev's reask rule can age an earlier ask (no ``at``: rows carry no time, as before)."""
    agents = {(m.get("displayName") or "").lower(): m["id"] for m in members if m.get("kind") == "agent"}
    names = {m["id"]: m.get("displayName") or m["id"] for m in members}
    rows = []
    lines = transcript.splitlines()[-30:]
    times = list(at or [])[-len(lines) :] if lines else []
    for line, when in zip(lines, [None] * (len(lines) - len(times)) + times):
        name, sep, text = line.partition(": ")
        if sep:
            name, arrow, target = name.partition(" → ")
            agent_id = agents.get(name.lower())
            author = {"kind": "agent" if agent_id else "human", "id": agent_id or name, "displayName": name}
            to = (
                [target]
                if arrow
                else [
                    names[i]
                    for c in messaging.address_candidates(text, members, live=True)
                    for i in c["agents"] + c["humans"]
                ]
            )
            row = {"author": author, "text": text, "to": to}
            if when is not None:
                row["createdAt"] = datetime.fromtimestamp(when, timezone.utc).isoformat()
            rows.append(row)
    if rows and rows[-1]["text"] == utterance:
        rows.pop()
    return rows


#: The wake word as STT writes it ("Lightbulb", "light bulb", "light-bulb"), with a lead and a comma.
_WAKE_WORD = re.compile(r"^\s*(?:(?:hey|ok|okay)[\s,]+)?light[\s-]?bulb\b[\s,.!?]*", re.I)


def _open_voice_lines(floor: dict | None, voice_runs: list[dict] | None, members: list[dict]) -> list[str]:
    """Live's ``state.open``, from the worker's memory: the floor and the driven voice runs."""
    names = {m["id"]: m.get("displayName") or m["id"] for m in members}
    lines = [
        f'{names.get(r.get("agent"), r.get("agent"))} is answering "{str(r.get("request") or "")[:120]}" (voice)'
        for r in voice_runs or []
    ]
    if floor and floor.get("agent"):
        lines.append(f"{names.get(floor['agent'], floor['agent'])} has the floor, talking with {floor.get('speaker')}")
    return lines[:5]


def _routed(*agent_ids: str) -> dict:
    """/route's answer: ``agentId`` (the first; what the shipped worker reads) and ``agentIds`` (every
    agent the line addressed or relayed to, in the order named, for a worker that queues them)."""
    return {"agentId": agent_ids[0] if agent_ids else None, "agentIds": list(agent_ids)}


async def route_live_line(
    *,
    owner_id: str,
    room_id: str,
    speaker_id: str,
    utterance: str,
    transcript: str,
    humans: int,
    wake: bool = False,
    floor: dict | None = None,
    voice_runs: list[dict] | None = None,
    at: list[int] | None = None,
) -> dict:
    """The one Live router (routing v2 §6), every line the worker's position-1 name did not settle,
    routed exactly as if the speaker had typed it in the room: the shared addressing stage (a name
    anywhere: addressed and relayed agents answer), chat's select_dispatch_targets, else Jev picks one
    listener or nobody, else (Jev down) chat's single-responder fallback. Live ignores listen modes
    (#3591), so every agent listens. Live's own rules: an unfinished line is marked ``hold`` (wait for
    the speaker's next final) but still names the agent it routes to, for a worker that does not hold;
    alone in the call, or after the wake word ``wake`` ("Lightbulb, ..."), Jev's "nobody" counts as Jev
    down -- the speech can only be for the agents.
    Returns ``_routed(...)``, plus ``"hold": True`` for an unfinished line."""
    meta, _ = await _room_team(owner_id, speaker_id, room_id)
    members = [
        {**m, "listenMode": "active"} if m.get("kind") == "agent" else m
        for m in await messaging_repo.list_members(room_id)
    ]
    if wake:
        utterance = _WAKE_WORD.sub("", utterance, count=1) or utterance
    author = {"kind": "human", "id": speaker_id, "displayName": await messaging.human_display_name(speaker_id)}
    history = _transcript_history(transcript, members, utterance, at)
    addressing = await messaging.address({"author": author, "text": utterance}, members, history=history, live=True)
    if addressing["hold"]:
        put_metric("voice.routing.jev", dimensions={"outcome": "hold"})

    def done(*agent_ids: str) -> dict:
        # An unfinished line still answers what it routes to today: the shipped worker reads only
        # agentId (a null would drop the line); a worker that honours "hold" waits for the next final.
        return {**_routed(*agent_ids), **({"hold": True} if addressing["hold"] else {})}

    named_ids = addressing["agents"]
    named = [{"kind": "agent", "id": a} for a in named_ids]
    targets = messaging.select_dispatch_targets(members, named, None, room_kind=meta.get("kind"))
    if addressed := [t["agentId"] for t in targets if t["eventKind"] == "user_request"]:
        # In the order named: the worker drives one voice at a time and can queue the rest.
        return done(*[a for a in addressing["agents"] if a in addressed], *sorted(set(addressed) - set(named_ids)))
    candidates = [t["agentId"] for t in targets]
    if not candidates:
        return done()
    people = addressing["humans"]
    # A relayed request ("Leo, Finance has those numbers") is not to people, nor a wake word.
    to_people = bool(people) and not named and not addressing["anyone"] and not wake
    message = {
        "author": author,
        "text": utterance,
        "mentions": [{"kind": "human", "id": p} for p in people],
        "dispatchTargets": targets,
    }
    try:
        decision = await asyncio.wait_for(
            jev._decide_for_message(
                meta,
                message,
                message,
                members,
                history,
                to_people=to_people,
                placement=False,
                open_lines=_open_voice_lines(floor, voice_runs, members),
            ),
            _ROUTING_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 -- a timeout or a card read: Jev is down for this line
        put_metric("voice.routing.timeout")
        decision = {"fallback": True}
    who, forced = decision.get("who"), decision.get("forced")
    if not forced and (who in candidates or (who == "none" and humans > 1 and not wake)):
        put_metric("voice.routing.jev", dimensions={"outcome": "routed" if who in candidates else "none"})
        return done(*([who] if who in candidates else []))
    # Jev down or undecided, a "nobody" a lone speaker or the wake word can't mean, or a "none" the
    # policy refused (a re-ask, a question to the room): openclaw_work's fallback for a human message.
    put_metric("voice.routing.jev", dimensions={"outcome": "forced" if forced else "fallback"})
    if forced:
        put_metric("messaging.jev.forced", dimensions={"reason": forced})
    if to_people:
        return done()  # spoken to a person: silence is the default
    if len(candidates) == 1:
        return done(candidates[0])
    return done(await openclaw_work._fallback_single(owner_id, candidates, message, history))


# ---- Option D: rename and voice notes (C3 notices, C8 voice-profile) -------------------------

RENAME_DAILY_LIMIT = 3
NOTE_DAILY_LIMIT = 10
NOTE_TEXT_MAX = 300


async def send_voice_notices(owner_id: str, notices: dict[str, dict]) -> None:
    """Best effort: one C3 ``voice.notice`` per room in ``notices`` that has an open Live window."""
    try:
        open_rooms = {r.get("roomId") for r in await _open_windows(owner_id, int(time.time()))}
        for room_id, payload in notices.items():
            if room_id in open_rooms:
                await livekit_huddles.send_frames(room_id, [{"type": "voice.notice", "payload": payload}])
    except Exception:  # noqa: BLE001 -- the change already landed; a notice is courtesy
        logger.warning("voice notice not sent owner=%s", owner_id, exc_info=True)


async def send_note_saved(owner_id: str, agent_id: str, name: str | None, text: str, by: str | None) -> None:
    try:
        rooms = await messaging_repo.list_rooms_for_member(owner_id, "agent", agent_id)
    except Exception:  # noqa: BLE001
        logger.warning("voice note notice: rooms unavailable owner=%s", owner_id, exc_info=True)
        return
    payload = {"kind": "voice_note_saved", "agentId": agent_id, "name": name, "text": text, "by": by}
    await send_voice_notices(owner_id, {e["meta"]["roomId"]: payload for e in rooms})


async def send_renamed_notices(
    owner_id: str, agent_id: str, to: str, by: str | None, old_names: dict[str, str | None]
) -> None:
    """``old_names`` = {roomId: displayName before the rename}. ``collides``: a person in the
    workspace answers to the new name (full name or first word)."""
    from core.services.clerk_admin import list_organization_members

    try:
        open_rooms = {r.get("roomId") for r in await _open_windows(owner_id, int(time.time()))}
    except Exception:  # noqa: BLE001
        logger.warning("rename notice: windows unavailable owner=%s", owner_id, exc_info=True)
        return
    old_names = {room: old for room, old in old_names.items() if room in open_rooms}
    if not old_names:
        return  # almost always: no Live window open, so skip the Clerk call
    wanted = to.casefold()
    try:
        people = await list_organization_members(owner_id)
    except Exception:  # noqa: BLE001 -- unknown means "no collision"; the notice still goes out
        people = []
    collides = False
    for p in people:
        full = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).casefold()
        if wanted in {full, (full.split() or [""])[0]}:
            collides = True
    notices = {
        room: {"kind": "renamed", "agentId": agent_id, "from": old, "to": to, "by": by, "collides": collides}
        for room, old in old_names.items()
    }
    await send_voice_notices(owner_id, notices)


async def _rename_agent(agent_id: str, name: str) -> None:
    """Paperclip directly, as the Teams PATCH does: a failure raises, so nothing else moves.
    (Not runtime_rpc._mirror_agent_update: it swallows Paperclip errors.)"""
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_admin_session import get_admin_session_cookie

    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    await admin.patch_agent(session_cookie=cookie, agent_id=agent_id, body={"name": name})


async def edit_voice_profile(
    owner_id: str, *, agent_id: str, run_id: str, action: str, name: str | None, text: str | None
) -> dict:
    """C8: an agent renames itself or edits its voice notes because a person asked it to on Live.
    Who asked comes from the VOICERUN row the backend wrote, never from the agent."""
    run = await messaging_repo.get_voice_run(owner_id, run_id)
    if not run or run.get("agentId") != agent_id:
        raise _problem(404, code="voice_run_not_found")
    speaker = run.get("speakerUserId")
    member = await messaging_repo.get_member(run["roomId"], "human", speaker) if speaker else None
    if not member or messaging_repo.member_home(member, owner_id) != owner_id:
        raise _problem(403, code="not_a_member")  # a guest of a shared room cannot rename our agents
    roster = await messaging.load_agent_roster(owner_id)
    row = next((r for r in roster if r["id"] == agent_id), None)
    if row is None:
        raise _problem(404, code="voice_run_not_found")
    if action == "rename":
        new = (name or "").strip()
        if not 1 <= len(new) <= 40 or "\n" in new or "\r" in new:
            raise _problem(422, code="invalid_name")
        if any(r["id"] != agent_id and str(r.get("name") or "").casefold() == new.casefold() for r in roster):
            raise _problem(409, code="name_taken")
        # ponytail: counted before the rename, so a Paperclip 5xx costs one of the 3; counting after
        # would let a burst rename past the limit. A refund on failure if that ever matters.
        if not await messaging_repo.count_voice_edit(owner_id, agent_id, "rename", RENAME_DAILY_LIMIT):
            raise _problem(429, code="rate_limited")
        await _rename_agent(agent_id, new)
        from core.services.hermes_provisioning import refresh_agent_soul

        await refresh_agent_soul(owner_id, agent_id, by=speaker)  # also sends C3 `renamed`
        _, notes = hermes_profiles.parse_voice_notes_md(
            await run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id)
        )
        return {"ok": True, "agentId": agent_id, "name": new, "notes": notes}
    said = (text or "").strip()
    if action != "clear_notes" and not 1 <= len(said) <= NOTE_TEXT_MAX:
        raise _problem(422, code="invalid_text")
    if not await messaging_repo.count_voice_edit(owner_id, agent_id, "notes", NOTE_DAILY_LIMIT):
        raise _problem(429, code="rate_limited")
    _, current = hermes_profiles.parse_voice_notes_md(
        await run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id)
    )
    line = f"{run.get('speakerName') or speaker} asked: {said}"
    lines = {"add_note": [*current.splitlines(), line], "replace_notes": [line]}.get(action, [])
    lines = [ln for ln in lines if ln.strip()]
    while len("\n".join(lines)) > hermes_profiles.VOICE_NOTES_MAX:
        lines.pop(0)  # the oldest note goes first
    notes = "\n".join(lines)
    await run_in_thread(
        hermes_profiles.upload_voice_notes, owner_id, agent_id, hermes_profiles.build_voice_notes_md(row["name"], notes)
    )
    await send_note_saved(owner_id, agent_id, row["name"], said, speaker)
    return {"ok": True, "agentId": agent_id, "name": row["name"], "notes": notes}


async def wipe_owner_voice(owner_id: str) -> None:
    """Terminal wipe: end this owner's live calls without billing (the ledger is being archived)."""
    calls = [
        c for st in (OPEN, CLOSING) for c in await container_repo.get_by_status(st) if c.get("ownerId") == owner_id
    ]
    for call in calls:
        await run_in_thread(_table().delete_item, Key=_key(call["callId"]))
    await run_in_thread(_table().delete_item, Key=_free_row_key(owner_id))


_SWEEP_SECONDS = 60
VOICE_SESSION_TIER = 25  # concurrent GPT-Live sessions our tier allows; W84 warns at 70%
_HUDDLE_GRACE_S = 60  # the worker ends its own exchange at the deadline with real usage; the sweep bills 0
_HUDDLE_ORPHAN_S = 90  # a huddle row this old whose room has no worker lost it: release the claim now


def _sweep_due(call: dict) -> int:
    return int(call.get("deadline", 0)) + _HUDDLE_GRACE_S


async def _worker_gone(call: dict, now: int) -> bool:
    """A huddle row whose worker crashed holds the free claim or credit hold until its deadline otherwise."""
    if now - int(call.get("startedAt", now)) < _HUDDLE_ORPHAN_S:
        return False
    if not livekit_huddles.configured():
        return False
    try:
        if await livekit_huddles.has_ceo(call["roomId"]):
            return False
    except Exception:  # noqa: BLE001 -- LiveKit unreachable: leave it to the deadline
        logger.info("huddle sweep: LiveKit check failed call=%s", call.get("callId"), exc_info=True)
        return False
    await _publish_huddle_presence(call["roomId"])  # only the worker publishes it; it is gone
    return True


async def _publish_huddle_presence(room_id: str) -> None:
    try:
        user_ids = await livekit_huddles.participants(room_id)
        await get_live_event_publisher().publish_room(
            room_id,
            messaging.make_live_event("room.huddle.changed", {"roomId": room_id, "userIds": user_ids}, room_id=room_id),
        )
    except Exception:  # noqa: BLE001 -- the marker is cosmetic
        logger.info("huddle presence publish failed room=%s", room_id, exc_info=True)


async def sweep_calls_once(now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    open_rows = await container_repo.get_by_status(OPEN)
    # ponytail: window count ≈ open GPT-Live sessions; worker's huddle.gptlive.open_seconds (Task 1) is the exact sum
    put_metric("voice.calls.open", value=len(open_rows))
    due = [c for c in open_rows if _sweep_due(c) <= now or await _worker_gone(c, now)]
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
