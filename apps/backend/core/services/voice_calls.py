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
    local_session_grants,
    meeting_invites,
    messaging,
    openclaw_work,
    runtime_transport,
    voice_persona,
)
from core.services.super_agent_flags import flags_for
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
# A voice answer is spoken while a person waits, so its prompt is kept small: the newest few room
# messages, each cut short, instead of the 50 a chat turn carries (Pulse's first voice turn was a
# 30k-token prompt, 3-6 s of model time before a word was spoken). The room session keeps the full
# history from its chat turns; this only bounds what a voice turn adds.
_VOICE_HISTORY_MESSAGES = 12
_VOICE_HISTORY_CHARS = 500
_background: set[asyncio.Task] = set()


def _fire(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# Static on purpose: Hermes splices `instructions` into the top of the system prompt, and the
# provider caches a byte-identical prefix. Everything per-run rides in the input.
# The script path is the image's (build-skills.sh maps /app/skills/ -> /opt/hermes-skills/); a
# run's shell has PAPERCLIP_COMPANY_ID / PAPERCLIP_AGENT_ID from the profile .env.
TURN_NOTE = """[Note: you are speaking in a Live voice call; your reply is spoken by you and nothing is posted to the room. The request below is what a person asked of you, in their own words from a voice transcript (it may contain mis-hearings, hesitations and later corrections; use the latest intent). It is usually one thing; if it holds more than one, do each and give each result. The bracket before it is what was said on the call since you last spoke, for context only. Other requests from the same breath are with teammates or queued for you: answer only what is asked here. Answer it from your own knowledge and tools, in words for speaking aloud. Do not narrate who is handling what, and do not hand the question to a teammate; name a person or a teammate only when the answer itself requires it. If you cannot answer, say what is missing.
Anything that can change (email, pull requests, deploys, payments, dates, metrics, what shipped) must be checked with a tool in this turn before you answer. Your memory and earlier messages tell you where to look, never what the answer is. Say in a few words where the answer came from ("from GitHub just now"). If you cannot check it, say so plainly; never give an old value as current.
Then pick whatever fits the request:
- Answer: after checking, reply in two or three short spoken sentences, starting with what it is about.
- Ask: if you cannot tell what is wanted, ask one short clarifying question instead of guessing.
- Hand it off: if it is work that should go on after the call (writing or building something, a job that runs for a long time), say so in one sentence and make sure it gets done by creating a Paperclip task for yourself or for the teammate who owns it:
  /opt/hermes-skills/paperclip/scripts/paperclip-api.sh create "$PAPERCLIP_COMPANY_ID" --title "<short title>" --assignee "<your or the owner's agent id; yours is $PAPERCLIP_AGENT_ID>" --status todo <<'EOF'
<what to do>
EOF
  Only promise to follow up if you actually created that task.
- Cannot: if you lack the access or the tool, say which one, in one sentence.
Your reply is spoken: no markdown, no lists, no code, no links, no ids. Never claim an action succeeded before it did.
Take as many steps as the request needs. Everything you write is spoken as you go, so while you work, before each lookup, say one short sentence with what you found so far or what you are checking next; give real content, never filler like "still checking".]"""

# Super-agent workspaces (contracts-v2 §14.1): work asked aloud starts a task through the start_work tool;
# the hand-off line above (a Paperclip task) does not apply there. One sentence, under the switch only.
SUPER_AGENT_NOTE = (
    "[In this workspace, hand work off with the start_work tool (a short title and a self-contained brief) "
    "rather than by creating a task yourself, then say in one sentence that it has started and that the "
    "result will be in the room.]"
)


# A meeting's unsaid answers ride on its META item (400 KB), for this occurrence and the newest
# three in its history: cut by UTF-8 BYTES (a CJK answer is 3 bytes a character), 4 x 20 x 2.5 KB.
_UNSAID_MAX, _UNSAID_REQUEST_BYTES, _UNSAID_ANSWER_BYTES = 20, 480, 2000


def _cut_bytes(text: str, limit: int) -> str:
    return text.encode()[:limit].decode(errors="ignore")


async def post_late_answer(owner_id: str, room_id: str, agent_id: str, run_id: str, request: str, answer: str) -> None:
    """The room gets what the call did not: the agent's own message, once per run (the event id
    is the run), carrying the question it answers so it reads on its own. A meeting's room is
    hidden, so there the answer is kept on the meeting record for the notes and the mail."""
    if ((await messaging_repo.get_room(room_id)) or {}).get("meeting"):
        await _keep_unsaid(room_id, agent_id, run_id, request, answer)
        return
    asked = f' "{request.strip()[:160]}"' if request.strip() else ""
    text = f"From the Live call{asked}:\n\n{answer.strip()}"
    await messaging.create_agent_message(
        owner_id,
        messaging.AgentMessageRequest(
            event_id=f"live-late:{run_id}",
            room_id=uuid.UUID(room_id),
            agent_id=uuid.UUID(agent_id),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )
    put_metric("voice.huddle.late_answer")


async def _keep_unsaid(room_id: str, agent_id: str, run_id: str, request: str, answer: str) -> None:
    """Once per run (a retried post adds nothing), the first ``_UNSAID_MAX`` only, and only while
    the record holds this occurrence's call: never onto a series' next one."""
    member = await messaging_repo.get_member(room_id, "agent", agent_id) or {}
    entry = {
        "runId": run_id,
        "agentId": agent_id,
        "agentName": member.get("displayName") or agent_id,
        "request": _cut_bytes(request.strip(), _UNSAID_REQUEST_BYTES),
        "answer": _cut_bytes(answer.strip(), _UNSAID_ANSWER_BYTES),
        "at": int(time.time()),
    }

    def add(meeting: dict) -> dict:
        unsaid = meeting.get("unsaid") or []
        if len(unsaid) >= _UNSAID_MAX or any(u.get("runId") == run_id for u in unsaid):
            return {}
        return {"unsaid": [*unsaid, entry]}

    if await meeting_invites._patch_meeting(room_id, add, when=lambda m: m.get("inCallAt")) is None:
        logger.info("meeting room %s: late answer of run %s came after the call's record moved on", room_id, run_id)
        return
    put_metric("meeting.unsaid")


def _voice_history(history: list[dict]) -> list[dict]:
    out = []
    for row in history[-_VOICE_HISTORY_MESSAGES:]:
        text = row.get("text") or ""
        out.append({**row, "text": text[:_VOICE_HISTORY_CHARS] + "…"} if len(text) > _VOICE_HISTORY_CHARS else row)
    return out


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


def _meet_label(speaker_name: str | None) -> str:
    return " ".join((speaker_name or "").split()) or "Someone"


def _meeting_actor(
    members: list[dict], speaker_id: str, meeting: bool, speaker_name: str | None = None
) -> tuple[str, str | None]:
    """Who a worker-supplied speaker acts as, and the label to show for them (None: a real member,
    look the name up). A Meet speaker is the bot's ``meet:<roomId>`` participant or a label ("Priya",
    "Priya + Dan"), not a user: in a meeting room it acts as the member who invited the bot (the
    room's owner). No owner member, or not a meeting -> the id passes through and 403s as before.
    ``speaker_name`` is the worker's label for the bot's line: display only, never who acts."""
    humans = [m for m in members if m.get("kind") == "human"]
    bot = speaker_id.startswith("meet:")
    if not meeting or (not bot and any(m["id"] == speaker_id for m in humans)):
        return speaker_id, None
    owner = next((m["id"] for m in humans if m.get("role") == "owner"), speaker_id)
    return owner, (_meet_label(speaker_name) if bot else speaker_id)


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


async def _runtime(
    owner_id: str, path: str, body: dict, *, timeout_s: float = _TURN_TIMEOUT_S, local_session_token: str | None = None
):
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise _problem(409, code="workspace_not_ready")
    headers = {"Authorization": f"Bearer {container.get('gateway_token') or ''}"}
    if local_session_token:
        headers["X-Lightbulb-Local-Session-Token"] = local_session_token
    return await runtime_transport.request(
        container,
        path,
        method="POST",
        json_body=body,
        headers=headers,
        timeout_s=timeout_s,
    )


def _voice_requester(members: list[dict], owner_id: str, speaker_id: str) -> str | None:
    """Who a voice run's local-session grant is FOR: the speaker, only when they are a human member of
    this workspace seated in the room (the C8 rule). A Meet speaker (``meet:<room>`` or a label) is
    never a member, so its grant names nobody (it reads only workspace-wide rooms); never the inviting
    owner."""
    member = next((m for m in members if m.get("kind") == "human" and m["id"] == speaker_id), None)
    return speaker_id if member and messaging_repo.member_home(member, owner_id) == owner_id else None


def _meeting_reader(members: list[dict], owner_id: str) -> str | None:
    """Whose access a Google Meet's turns read rooms with (owner ruling 2026-10-13): the member who
    invited the agents, the meeting room's one human, its owner. Everyone on the call hears what is
    read. Rooms only: the run's requester (the Mac, local sessions) stays nobody for a Meet speaker."""
    m = next((m for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)
    return m["id"] if m and messaging_repo.member_home(m, owner_id) == owner_id else None


def prewake_owner(owner_id: str, team: list[dict] | None = None, room_id: str | None = None) -> None:
    """Fire-and-forget wake of the workspace runtime (one health RPC), then a profile pull for every
    agent in the room (the bridge's profile.warm, off its request thread), so the first summon finds
    both warm. Pulse's first voice answer waited 23 s on its cold profile restore alone.

    With ``room_id`` the bridge also compacts each agent's session for that room if it is near
    Hermes's threshold: a spoken turn never compacts (67-103 s inside a turn, prod 10-07), so the
    start of the call is where it happens. The RPC answers at once either way."""

    async def wake():
        try:
            await container_rpc.call(owner_id, "health")
        except Exception:  # noqa: BLE001 -- best effort; the first turn wakes it otherwise
            logger.info("voice owner prewake failed", exc_info=True)
            return
        for member in team or ():
            try:
                params = {"agentId": member["id"], **({"roomId": room_id} if room_id else {})}
                await container_rpc.call(owner_id, "profile.warm", params, timeout_s=10.0)
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


class _Steps:
    """How long each step of one request took, in order, for one log line. 10-08 Meet: the first line of a
    call waited 4.7 s on /summon and /session, and nothing said on which step."""

    def __init__(self) -> None:
        self._at, self.ms = time.perf_counter(), {}

    def done(self, step: str) -> None:
        now = time.perf_counter()
        self.ms[step], self._at = round((now - self._at) * 1000), now

    def __str__(self) -> str:
        return " ".join(f"{step}={ms}" for step, ms in self.ms.items()) + f" total={sum(self.ms.values())}"


async def open_voice_window(*, owner_id: str, room_id: str, speaker_user_id: str) -> dict:
    """C1 /summon: join the room's open window, or gate once and open one. The window meters
    GPT-Live only; voice runs are ordinary room runs and the window owns none of them."""
    # A meeting's speaker is the Meet bot: the window is the inviting owner's (and draws their allowance).
    if ((await messaging_repo.get_room(room_id)) or {}).get("meeting"):
        speaker_user_id, _ = _meeting_actor(await messaging_repo.list_members(room_id), speaker_user_id, True)
    # Before the join too: _room_team 404s unless owner_id owns the room and the speaker is a member.
    steps = _Steps()
    meta, _ = await _room_team(owner_id, speaker_user_id, room_id)
    steps.done("room")
    now = int(time.time())
    # ponytail: a crashed worker's unreported GPT-Live seconds bill 0 (voice.huddle.usage_missing, as today);
    # persist running usage on the row if that metric is non-zero in prod
    existing = await _open_window(owner_id, room_id, now)
    steps.done("open_windows")
    if existing:
        prewake_owner(owner_id)
        logger.info("voice window joined room=%s ms: %s", room_id, steps)
        return {
            "callId": existing["callId"],
            "deadline": int(existing["deadline"]),
            "budgetSeconds": int(existing["deadline"]) - now,  # a restarted worker never gets the full budget back
            "joined": True,
        }
    mode, budget = await _gate(owner_id)
    steps.done("gate")
    row = await _owner_row(owner_id)
    steps.done("owner_row")
    call_id = str(uuid.uuid4())
    if mode == "free":
        await _claim_free(owner_id, call_id, now, now + budget + 180)
    hold = 0
    try:
        if mode == "credits":
            hold, budget = await _hold(owner_id)
            if not hold:
                raise _problem(402, code="voice_limit", reason="credits_low")
        steps.done("claim")
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
    steps.done("put_call")
    prewake_owner(owner_id)
    logger.info("voice window opened room=%s billing=%s ms: %s", room_id, mode, steps)
    return {"callId": call_id, "deadline": call["deadline"], "budgetSeconds": budget, "joined": False}


# ---- Option D: the worker's per-agent reads (C1 /team and /agents/{agentId}/session) ---------

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


async def agent_meet_voice(*, owner_id: str, room_id: str, agent_id: str) -> dict:
    """An agent's meeting voice and nothing else: no persona."""
    await _owned_room(owner_id, room_id)
    members, starters = await asyncio.gather(messaging_repo.list_members(room_id), _starter_ids(owner_id))
    if not any(m.get("kind") == "agent" and m["id"] == agent_id for m in members):
        raise _problem(409, code="agent_not_in_room")  # removed while its run finished: it says nothing
    try:  # the roster alone: a slow SOUL.md read must not cost a founder-roster agent its role voice
        roster = await asyncio.wait_for(messaging.load_agent_roster(owner_id), _SESSION_READ_BUDGET_S)
    except Exception:  # noqa: BLE001 -- no title: the agent's stable fallback voice
        logger.warning("meet voice roster read failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
        roster = []
    row = next((r for r in roster if r["id"] == agent_id), None)
    title = (row or {}).get("title") or (row or {}).get("role")
    return {"voice": voice_persona.agent_meet_voice(agent_id, starters, title)}


async def agent_session(*, owner_id: str, room_id: str, agent_id: str) -> dict:
    """One agent's voice session: its own voice and persona. Falls back to the bare persona
    (no role, SOUL or notes) when the reads fail or overrun."""
    steps = _Steps()
    meta = await _owned_room(owner_id, room_id)
    steps.done("room")
    members, starters = await asyncio.gather(messaging_repo.list_members(room_id), _starter_ids(owner_id))
    steps.done("members")
    me = next((m for m in members if m.get("kind") == "agent" and m["id"] == agent_id), None)
    if not me:
        raise _problem(409, code="agent_not_in_room")
    row, soul, notes_md = await _session_reads(owner_id, agent_id)
    steps.done("profile_reads")  # the roster, SOUL.md and the voice notes, side by side, _SESSION_READ_BUDGET_S at most
    name = (row or {}).get("name") or me.get("displayName") or "Agent"
    logger.info("voice session room=%s agent=%s ms: %s", room_id, agent_id, steps)
    title = (row or {}).get("title") or (row or {}).get("role")
    persona = voice_persona.build_agent_persona(
        name=name,
        role=title,
        soul_excerpt=hermes_profiles.strip_generated_marker(soul) if soul else None,
        voice_notes=hermes_profiles.parse_voice_notes_md(notes_md)[1],
        room_name=meta.get("name"),
        people=[m.get("displayName") or m["id"] for m in members if m.get("kind") == "human"],
        agents=[m.get("displayName") or "Agent" for m in members if m.get("kind") == "agent" and m["id"] != agent_id],
    )
    return {
        "agentId": agent_id,
        "name": name,
        "voice": voice_persona.agent_voice(agent_id, starters, title),
        "meetVoice": voice_persona.agent_meet_voice(agent_id, starters, title),
        "persona": persona,
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
    *,
    call: dict,
    run_id: str,
    agent_id: str,
    speaker_id: str,
    utterance: str,
    transcript: str,
    speaker_name: str | None = None,
    requests: list[str] | None = None,
) -> dict:
    """One voice request = one run for agent:{agent}:lightbulb:channel:{room}, which the bridge records
    in the call's own transcript (identity.callId). run_id is the worker's, reused on every retry, so a
    delivered-but-timed-out invoke is replayed, never re-run."""
    owner_id, room_id = call["ownerId"], call["roomId"]
    super_agent = await flags_for(owner_id)  # lane A: start_work is offered, the Paperclip hand-off is not
    meta, members = await asyncio.gather(messaging_repo.get_room(room_id), messaging_repo.list_members(room_id))
    if not meta or not any(m.get("kind") == "agent" and m["id"] == agent_id for m in members):
        raise _problem(409, code="agent_not_in_room")
    # The label only: the VOICERUN row keeps the worker's id, so a Meet guest never gets the owner's C8 rights.
    name = _meeting_actor(members, speaker_id, bool(meta.get("meeting")), speaker_name)[1] or await _speaker_name(
        speaker_id
    )
    spoken = transcript[-_TRANSCRIPT_CHARS:].strip()
    session_id = f"agent:{agent_id}:lightbulb:channel:{room_id}"  # == the bridge's turn.lightbulb_session_key
    # Conversation engine §3.4: the other asks of the same spoken turn, so this agent answers only its own.
    also = "\n".join((requests or [])[:8])
    asked = f"[Asked in the same breath; teammates are on these:\n{also}]\n\n" if also else ""
    requester = _voice_requester(members, owner_id, speaker_id)
    reader = _meeting_reader(members, owner_id) if meta.get("meeting") else None
    # What a typed turn here gets (openclaw_work.build_ingress_body): the room block, the roster with
    # human handles, and the live local sessions (withheld for a room shared with another org).
    from core.services import local_sessions

    handled, sessions = await asyncio.gather(
        messaging.human_emails(members), local_sessions.live_sessions_for_room(owner_id, room_id, members)
    )
    body = {
        "identity": {
            "runId": run_id,
            "companyId": call["companyId"],
            "agentId": agent_id,
            "roomId": room_id,
            "sessionId": session_id,
            "callId": call["callId"],  # the bridge keeps one transcript per call, apart from the room's
            "family": "voice",
        },
        "request": {
            "input": f"[Live voice in #{meta.get('name') or 'this room'}. Spoken since you last spoke, newest last:\n"
            f"{spoken}]\n\n{asked}{name} (voice): {utterance}",
            "instructions": TURN_NOTE + ("\n" + SUPER_AGENT_NOTE if super_agent else ""),
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
            "recentHistory": _voice_history(await openclaw_work.room_history(meta, int(meta.get("lastSeq") or 0) + 1)),
            "participants": openclaw_work.ingress_participants(messaging.with_human_handles(members, handled)),
            "room": {"id": room_id, "name": meta.get("name"), "kind": meta["kind"], "revision": int(meta["revision"])},
            "sender": {"id": speaker_id, "kind": "human" if requester else "guest", "displayName": name},
            **({"sessions": sessions} if sessions else {}),
            **({"superAgent": True} if super_agent else {}),
        },
    }
    history = body["request"]["recentHistory"]
    while history and messaging.json_body_size(body) > openclaw_work._INGRESS_MAX_BYTES:
        history.pop(0)
    # Before the POST: the voice-profile route (C8) resolves the speaker from this row, and
    # requesterUserId is the run's verified requester (the Mac tool's authority; None = nobody).
    fields = {
        "roomId": room_id,
        "agentId": agent_id,
        "speakerUserId": speaker_id,
        "speakerName": name,
        "requesterUserId": requester,
        "readerUserId": reader,
    }
    # An atomic SET of these fields: a /turns retry cannot drop a "don't post that" mark, whenever it lands.
    # The requester is written once: a retry after another speaker's steer lowered it keeps it lowered,
    # and the grant below is minted for what the row holds, never for a requester the steer took away.
    row = await messaging_repo.put_voice_run(owner_id, run_id, fields, once=frozenset({"requesterUserId"}))
    requester = row.get("requesterUserId") if "requesterUserId" in row else requester
    # The room grant a typed message here would get (openclaw_work), keyed on the voice run.
    try:
        grant = local_session_grants.mint(owner_id, room_id, agent_id, run_id, requester, reader)
    except (RuntimeError, messaging.ApiProblem):
        grant = None
        logger.warning("Voice local session invocation unavailable")
    try:
        resp = await _runtime(owner_id, _BRIDGE_RUNS, body, local_session_token=grant)
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


async def _run_control(
    owner_id: str, run_id: str, action: str, body: dict, local_session_token: str | None = None
) -> int | None:
    try:
        resp = await _runtime(
            owner_id,
            f"/plugins/lightbulb-channel/runs/{run_id}/{action}",
            body,
            timeout_s=_STEER_TIMEOUT_S,
            local_session_token=local_session_token,
        )
        return resp.status_code
    except (httpx.HTTPError, ValueError):
        logger.info("voice run %s failed run=%s", action, run_id, exc_info=True)
        return None


async def steer_voice_run(
    *, call: dict, run_id: str, agent_id: str, speaker_id: str, utterance: str, speaker_name: str | None = None
) -> dict | None:
    """A follow-up steers the agent's live voice run. None (dispatch a new run instead) when the run
    is not this room's and agent's, or the bridge no longer has it."""
    row = await messaging_repo.get_voice_run(call["ownerId"], run_id)
    if not row or row.get("roomId") != call["roomId"] or row.get("agentId") != agent_id:
        return None
    name = _meet_label(speaker_name) if speaker_id.startswith("meet:") else await _speaker_name(speaker_id)
    # The run's local-session grant must not outrank its least-privileged speaker: a steer by anyone
    # but the run's latest speaker drops it to no requester (no rooms read as them) for the rest of the
    # run. Only ever lowered: a steer never carries a grant naming anyone, and the bridge swaps it in.
    grant = None
    if speaker_id != row.get("speakerUserId"):
        try:
            grant = local_session_grants.mint(
                call["ownerId"], call["roomId"], agent_id, run_id, None, row.get("readerUserId")
            )
        except (RuntimeError, messaging.ApiProblem):
            return None  # the old grant cannot be lowered: the steerer gets a run (and grant) of its own
    text = {"text": f"{name} (voice): {utterance}"}
    if await _run_control(call["ownerId"], run_id, "steer", text, local_session_token=grant) != 202:
        return None
    # The latest requester is who C8 checks: a guest's steer must not borrow the member's rights.
    # An atomic SET, not a whole-row put: a noPost mark landing after our read must survive.
    lowered = {"requesterUserId": None} if speaker_id != row.get("speakerUserId") else {}
    await messaging_repo.update_voice_run(
        call["ownerId"], run_id, {"speakerUserId": speaker_id, "speakerName": name, **lowered}
    )
    return {"runId": run_id, "agentId": agent_id, "steered": True}


async def abort_voice_run(owner_id: str, room_id: str, run_id: str) -> bool:
    """Only an explicit "stop" aborts a voice run, and only one of this room's."""
    row = await messaging_repo.get_voice_run(owner_id, run_id)
    if not row or row.get("roomId") != room_id:
        return False
    return await _run_control(owner_id, run_id, "abort", {}) == 202


async def mark_no_post(owner_id: str, room_id: str, run_id: str) -> bool:
    """ "Don't post that" (conversation engine §3.6): this room's voice run is never posted late, by the
    worker or the bridge (messaging.create_agent_message reads the flag). The worker re-sends it after a
    /turns retry: every writer of this row uses an atomic SET."""
    row = await messaging_repo.get_voice_run(owner_id, run_id)
    if not row or row.get("roomId") != room_id:
        return False
    if not await messaging_repo.update_voice_run(owner_id, run_id, {"noPost": True}):
        return False
    put_metric("voice.huddle.no_post")
    return True


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


def _open_voice_lines(floor: dict | None, open_requests: list[dict], members: list[dict]) -> list[str]:
    """Live's ``state.open`` for the room judge, from the worker's memory: the requests being worked on
    and the floor."""
    names = {m["id"]: m.get("displayName") or m["id"] for m in members}
    lines = [
        f'{names.get(r.get("agentId"), r.get("agentId"))} is answering "{str(r.get("task") or "")[:120]}" (voice)'
        for r in open_requests
        if r.get("status") in _OPEN_STATUS
    ]
    if floor and floor.get("agent"):
        lines.append(f"{names.get(floor['agent'], floor['agent'])} has the floor, talking with {floor.get('speaker')}")
    return lines[:5]


def _routed(*agent_ids: str) -> dict:
    """/route's answer: ``agentId`` (the first; what the shipped worker reads) and ``agentIds`` (every
    agent the line addressed or relayed to, in the order named, for a worker that queues them)."""
    return {"agentId": agent_ids[0] if agent_ids else None, "agentIds": list(agent_ids)}


#: Conversation understanding §4.3: a part with no name goes to the teammate whose job it is only when the
#: pick is this sure and this far ahead of the next; else to the floor agent or the chief of staff.
#: Fitted on scripts/speak_gate_bench/understand.py.
FIT_P = 0.35
FIT_MARGIN = 0.0
#: The latest message continues or calls off an earlier request at this probability.
AMEND_P = 0.5
#: A part is a request only when Jev reads it as one at least this surely; a weaker read, or none, is small
#: talk the voice answers itself (no run). 10-04 prod calls: side talk to someone off the call ("Ubera
#: business, right"), a complaint at the voice ("Can you not talk on me") and nervous fragments each became
#: a run. Fitted on scripts/speak_gate_bench/understand.py: the weakest true request reads 0.54 (the two-word
#: follow-up "and last month?"), so 0.6 loses it; the new KINDS wording is what clears the side talk.
REQUEST_P = 0.5
#: A part is background for a request ("context") only when Jev reads it so at least this surely: a weaker
#: read is not for the agents. "Ubera business, right" read 0.42-0.47 and was added to a working request
#: as a correction; the weakest true correction reads 0.75 ("only the ones on the main branch").
CONTEXT_P = 0.6
#: A stop withdraws a request only when Jev reads it as a withdrawal at least this surely. A weaker read
#: silences the agent and leaves its work running: silence can be undone, an aborted run cannot.
WITHDRAW_P = 0.5
_OPEN_STATUS = ("queued", "working")
#: How far the worker has said a ready answer when nobody can have heard any of it (absent: an older worker).
_UNHEARD = (None, "none", "pending")


def _same_ask(a: str, b: str) -> bool:
    """The same request said again, word for word but for case, punctuation and filler."""

    def words(text: str) -> list[str]:
        return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _FILLER_WORDS]

    return bool(words(a)) and words(a) == words(b)


# never a word that can be the ask itself ("like and share the launch post")
_FILLER_WORDS = frozenset({"um", "uh", "and", "also", "then", "so", "ok", "okay", "please"})


def _understood(
    addressing: dict,
    utterance: str,
    *,
    implied: bool,
    open_requests: list[dict],
    agents: set[str],
) -> dict:
    """The requests on a line (conversation understanding §4.2-§4.4), from what the one Jev call said about
    each part. ``implied``: the line can only be for the agents (a lone speaker, the wake word, the floor
    conversation, a named agent, a line among people the room judge picked an agent for). Not implied, an
    unnamed part is no request. Owners marked ``None`` here are filled by the caller (the floor agent, the
    judge's agent, the chief of staff).
    Returns ``{requests: [{agentId|None, task, by}], talk, more, nudge, stop, cancel, hush, quiet, steer}``.
    ``nudge``: a part only calls on an agent or tells it to go on ("Friday, go ahead"); that is never a
    request. ``stop``: a part is a stop, whether or not a request was found for it. What a stop does is its
    scope, read from the part's kind and never from whether it names a request: ``cancel`` lists the request
    a withdrawal names ("never mind the deck"), ``hush`` the request a speech-only stop names ("stop talking
    about the deck"). Each stop keeps its own request, so one line can do both to different requests.
    ``quiet``: the agents of a stop whose request could not be told (``None``: the caller's floor agent).
    They are told to stop talking and no more, also when the stop was a withdrawal."""
    parts, pieces = addressing["parts"], addressing["pieces"]
    named = [(name, agent) for name, agent, _ in addressing["named"]]
    by_name = dict(named)
    # a person the line speaks to ends an agent's task like a named agent does ("what's our MRR, and Aden, ...")
    asked_names = set(by_name) | set(addressing.get("called") or ())
    rows = {r["id"]: r for r in open_requests}

    def target(answer) -> dict | None:
        choice, p, _ = answer or (None, 0.0, 0.0)
        return rows.get(choice) if p >= AMEND_P else None

    where = {ref: start for k, start, _, ref, _ in pieces if k == "part"}
    name_at = next((start for k, start, _, ref, _ in pieces if k == "name" and named and ref == named[0][0]), -1)

    # Said TO one agent: the classifier read its name as addressed (`route_live_line` writes the same label
    # for a name that passed `_asked_by_name`, the clause-opening rule). A name it read as relayed ("Can you
    # ask Friday what the plan costs?") is an agent the line talks about to someone else, and one it read as
    # mentioned never gets here: the part after such a name stays what the classifier said it is. (With no
    # classifier there are no parts to read: `route_live_line` never calls this on the fallback path.)
    said_to_one = len(named) == 1 and addressing["named"][0][2] == "addressed"

    def kind_of(part: dict) -> str:
        # a weak or missing read is no request (REQUEST_P): an unsure classifier never starts a run
        choice, p, _ = part["kind"]
        if said_to_one and where[part["index"]] > name_at:
            # ...but not of words said to one agent by name. The name says whom they are for, so the
            # classifier's doubt is about nothing else: no read at all, a request read weakly, or words
            # that open the way a request does read as talk between people, is that agent's request.
            # 10-08 Meet: "Friday, what does the Workspace plan cost per month?" came back with no agent
            # and nothing to do, and nobody answered. A stop, thanks, a greeting, "go ahead" are reads of
            # their own and stay what they are.
            if not choice or choice == "request" or (choice == "people" and part["cue"]):
                return "request"
        if not choice or (choice == "request" and p < REQUEST_P):
            return "social"
        if choice == "context" and p < CONTEXT_P:
            return "people"  # an unsure "about the work" read: not for the agents, so it steers nothing
        if choice == "cancel" and p < WITHDRAW_P:
            return "hush"  # an unsure withdrawal stops the talk only
        return choice

    kind = {p["index"]: kind_of(p) for p in parts}
    asks = [p for p in parts if kind[p["index"]] == "request"]
    if not asks:
        # Jev read every part as background, yet one opens the way a request does ("we just hired two
        # engineers in Berlin, | set up their GitHub access"): that part is the request. Only a part Jev
        # called context: a weak request is social by now, and stays so.
        asks = [p for p in parts if kind[p["index"]] == "context" and p["cue"]]
        kind |= {p["index"]: "request" for p in asks}
    if (
        not asks
        and named
        and not any(kind[p["index"]] in ("social", "people", "more", "cancel", "hush") for p in parts)
    ):
        asks = [p for p in parts if kind[p["index"]] == "context"]  # "Forge, the deploy looks broken"
        kind |= {p["index"]: "request" for p in asks}
    # Words that run straight into a name and are asked of nobody ("Can you get | Forge, no actually Gauge,
    # | to look at ...") are the start of the request beside them, never a request of their own.
    lead = [p for p in asks if p["into"] and not any(n in by_name for n in p["asked"] or ())]
    if len(lead) < len(asks):
        kind |= {p["index"]: "context" for p in lead}
        asks = [p for p in asks if p not in lead]
    # a context part belongs to the request beside it: the one before, else the one after
    mine: dict[int, set[int]] = {p["index"]: {p["index"]} for p in asks}
    for p in parts:
        if kind[p["index"]] == "context" and asks:
            before = [a["index"] for a in asks if a["index"] < p["index"]]
            mine[before[-1] if before else asks[0]["index"]].add(p["index"])

    def task(indexes: set[int]) -> str:
        return messaging._task(utterance, pieces, indexes, asked_names) or " ".join(
            parts[i]["text"] for i in sorted(indexes)
        )

    after_name = not any(where[p["index"]] > name_at for p in asks)  # nothing is asked after the name
    requests: list[dict] = []
    for p in asks:
        text = task(mine[p["index"]])
        owners = [a for n, a in by_name.items() if n in (p["asked"] or ())]  # named order; a set has none
        if not owners and not messaging._ANYONE.search(p["text"]):
            if len(named) == 1 and (after_name or where[p["index"]] > name_at):
                # One agent is addressed: what follows its name is asked of it, and so is what comes before
                # when nothing follows ("Can you check the deploy, Forge?"). In "check the API, and Ledger,
                # what's our cash" the first ask is not Ledger's.
                owners = [named[0][1]]
            elif len(named) > 1 and p["asked"] is None:
                # several named and nothing says whose part this is (a line too long to cut): each of
                # them gets it, as before the cut existed
                owners = [agent for _, agent in named]
        if owners:
            requests += [{"agentId": a, "task": text, "by": "name"} for a in owners]
            continue
        if not implied:
            continue  # people talking among themselves, and the room judge picked nobody
        who, prob, margin = p["who"]
        fit = who in agents and prob >= FIT_P and margin >= FIT_MARGIN
        requests.append({"agentId": who if fit else None, "task": text, "by": "fit" if fit else ""})
    out = {
        "requests": requests,
        "talk": any(kind[p["index"]] in ("social", "more", "context") for p in parts) or not parts,
        "people": bool(parts) and all(kind[p["index"]] == "people" for p in parts),
        "more": any(kind[p["index"]] == "more" for p in parts),
        # a name and nothing else ("Friday?") calls on the agent too
        "nudge": any(kind[p["index"]] == "nudge" for p in parts) or bool(named and not parts),
        "stop": any(kind[p["index"]] in ("cancel", "hush") for p in parts),
        "cancel": [],
        "hush": [],
        "quiet": [],
        "steer": [],
    }
    stops = [p for p in parts if kind[p["index"]] in ("cancel", "hush")]
    for p in stops:
        # The request this stop is about: the one its own part names. A line with one stop has the line's.
        # 10-07 review: one request was picked for the whole line, and a withdrawal anywhere withdrew it.
        # "Stop talking about the deck. Cancel the invoice task." aborted the deck.
        row = target(p.get("drops")) or (target(addressing.get("drops")) if len(stops) == 1 else None)
        if row and row["status"] != "delivered":
            out[kind[p["index"]]].append(row["id"])
        else:
            # No request can be told: its agent stops talking. A task nobody identified is never aborted.
            out["quiet"] += (
                [a for n, a in by_name.items() if n in (p["asked"] or ())] or [a for _, a in named] or [None]
            )
    out["cancel"] = list(dict.fromkeys(out["cancel"]))
    out["hush"] = [i for i in dict.fromkeys(out["hush"]) if i not in out["cancel"]]  # withdrawn outright
    amended = target(addressing.get("amends"))
    if amended and not out["cancel"] and (asks or any(k == "context" for k in kind.values())):
        # The message continues an earlier request (Jev answers for the whole message): what it asks,
        # unless asked of another agent by name, goes to that request. Still being worked on: its run is
        # steered ("make it shorter and drop the emoji" is one correction). Already answered: its owner
        # gets the follow-up.
        theirs = [r for r in requests if r["by"] != "name" or r["agentId"] == amended["agentId"]]
        about = {p["index"] for p in parts if kind[p["index"]] in ("request", "context")}
        if len(requests) > 1 and any(r["by"] == "fit" and r["agentId"] != amended["agentId"] for r in theirs):
            # Several asks and one is plainly another teammate's work: these are new requests that only
            # touch the same subject (the founder's "check Aden's email ... what shipped ... what PRs are
            # open" after asking about the same application), not a correction.
            pass
        elif amended["status"] in _OPEN_STATUS and (theirs or not requests):
            said = ". ".join(r["task"] for r in theirs) or task(about)
            out["requests"] = [r for r in requests if r not in theirs]
            out["steer"] = [{"requestId": amended["id"], "text": said}]
        elif amended["status"] == "ready" and amended.get("delivery") in _UNHEARD and (theirs or not requests):
            # Its answer is ready and nobody has heard it: the message cannot be about that answer, so it
            # corrects the request. The stale answer is dropped unsaid and the request is asked again
            # with the correction ("what are signups last week" ... "actually, last month"), also when the
            # correction asks nothing of its own ("actually last month, not last week"). An answer the
            # worker says is being said, or was talked over part way (``delivery``), has been heard in
            # part: a message about it is a follow-up for its agent, below.
            out["cancel"] = [amended["id"]]
            if not theirs:
                theirs = [{"task": task(about)}]
                requests += theirs
            for r in theirs:
                r |= {"agentId": amended["agentId"], "task": f"{amended['task']}. {r['task']}", "by": "follow"}
        elif amended["status"] not in _OPEN_STATUS:
            for r in theirs:
                r |= {"agentId": amended["agentId"], "by": "follow"}
    return out


def _asked_by_name(addressing: dict, utterance: str, members: list[dict]) -> tuple[str, str] | None:
    """The one agent a line asks by name when the classifier said the name addresses nobody: (name, agent),
    or None. The words decide, and only when they leave no doubt: one name on the whole line, an agent's
    alone; it stands as a name where a clause opens ("Friday, ...", "Great. Friday, ...", "hey Friday"; never "by
    Friday", "Friday's notes", "thanks Friday"); and the words right after it open the way a request does
    (a question word, "can you", an instruction). "Friday, we ship the beta" asks nothing and stays the day."""
    cands = messaging.address_candidates(utterance, members, live=True)
    if len(cands) != 1 or len(cands[0]["agents"]) != 1 or cands[0]["humans"]:
        return None
    cand, pieces = cands[0], addressing["pieces"]
    if not any(messaging._CLAUSE_START.search(utterance[:start]) for start, _ in messaging._named_at(utterance, cand)):
        return None
    cue = {p["index"]: p["cue"] for p in addressing["parts"]}
    for (kind, _, _, ref, _), after in zip(pieces, pieces[1:]):
        if kind == "name" and ref == cand["name"] and after[0] == "part" and cue.get(after[3]):
            return cand["name"], cand["agents"][0]
    return None


def _labels(addressing: dict) -> str:
    """What the classifier said about a line, in one log line: enough to explain a route after the call."""
    read = lambda a: f"{a[0]}:{a[1]:.2f}" if a and a[0] else "-"  # noqa: E731
    parts = " | ".join(
        f"{p['text'][:40]!r} kind={read(p['kind'])} who={read(p['who'])}{' cue' if p['cue'] else ''}"
        for p in addressing.get("parts") or []
    )
    named = ",".join(f"{name}={label}" for name, _, label in addressing.get("named") or []) or "-"
    return (
        f"path={addressing['path']} named={named} people={len(addressing['humans'])} hold={addressing['hold']} "
        f"amends={read(addressing.get('amends'))} drops={read(addressing.get('drops'))} parts=[{parts}]"
    )


def _engaged(addressing: dict, engagements: list[dict], open_requests: list[dict]) -> str | None:
    """The agent an unnamed meeting line is for, or None: talk between people. Decided from what the worker
    knows of the speaker and each agent (``engagements``) and what the one Jev call said about the line, with
    no probability bar on "is this a request": that a line asks something does not say whom it asks.

    A line that addresses a person is for that person. A line that continues or calls off a request is for
    that request's agent, whoever the speaker has turned to since: that reference is the evidence a follow-up
    is one ("And what are they called?" after the answer), and the worker lists the answered requests for it.
    Otherwise it is for the agent the speaker is talking with (``state`` active) when something is still open
    between the two: the agent asked them something (``expects`` reply), or they talked over what it was
    saying (``last`` paused, ``lastBy`` speaker). Having just heard its answer (``last`` delivered) opens
    nothing by itself: 10-08 Meet, 25 s after the answer its asker said "Yeah, I think we should move the
    stand-up to eight" to a colleague, and it became the agent's request. Recency alone is no evidence:
    people in a meeting go on talking to each other."""
    parts = addressing["parts"]
    if addressing["humans"] or (parts and all(p["kind"][0] == "people" for p in parts)):
        return None
    rows = {r["id"]: r["agentId"] for r in open_requests}
    for key in ("amends", "drops"):
        choice, p, _ = addressing.get(key) or (None, 0.0, 0.0)
        if choice in rows and p >= AMEND_P:
            return rows[choice]
    for e in engagements:
        open_between = e.get("expects") == "reply" or (e.get("last") == "paused" and e.get("lastBy") == "speaker")
        if e.get("state") == "active" and open_between:
            return e["agentId"]
    return None


def _said_to_people(addressing: dict, engagements: list[dict]) -> bool:
    """A meeting line the worker sent as part of someone's talk with an agent, and that is not the agent's
    (``_engaged``): is it talk between people, which ends that talk? Yes, unless its speaker has only just
    heard the agent out (``state`` active, ``last`` delivered) and nothing reads the line as said to a
    person: "Okay, thanks." after an answer turns to nobody. It starts nothing and ends nothing."""
    parts = addressing["parts"]
    if addressing["humans"] or (parts and all(p["kind"][0] == "people" for p in parts)):
        return True
    return not any(e.get("state") == "active" and e.get("last") == "delivered" for e in engagements)


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
    setting: str = "live",
    unclaimed: bool = False,
    open_requests: list[dict] | None = None,
    engagements: list[dict] | None = None,
) -> dict:
    """The one Live router (conversation understanding §4.2-§4.4): every final line the worker hears.
    One Jev call (``messaging.address``) cuts the line into parts and says what each is (a request,
    context, small talk, talk between people, an announcement, a cancellation) and whose job a request is.
    A request's owner is the agent the line names, else the teammate it fits, else the floor agent the
    speaker is talking with, else the chief of staff. Live ignores listen modes (#3591).
    Who is implied: a lone speaker, the wake word ``wake`` ("Lightbulb, ..."), the floor conversation
    (``floor.mine``) and a line that names an agent can only be for the agents. With two or more people
    and no name, chat's room judge (``jev._decide_for_message``: would a good teammate answer this?) says
    whether an agent answers at all, as it did before understanding; when it picks one, the line is
    understood like any other and that agent owns what fits nobody better. ``setting="meeting"`` (a Google
    Meet): nobody is implied but the named, the wake word and an ``unclaimed`` question to the room (the worker
    waited 3.5 s). ``floor.mine`` implies nobody there: the worker sends who the speaker is talking with
    (``engagements``: per agent its request, ``state``, what is expected next, how the agent's last words to
    them ended) and the open requests with their askers and delivery, and ``_engaged`` decides from that.
    With no such evidence, the line is talk between people.
    Jev down: the whole line is one request for the named agents, else (wake word, unclaimed) one agent;
    a line only the floor or a lone speaker implies is no request.
    Returns ``{agentId, agentIds, requests?, hold?, ops}``: ``ops`` is ``{requests: [{agentId, task, by}],
    social, more, cancel, hush, called, steer, people, say}``; ``say`` is ``{agentId, kind, task, text}`` or
    None: the one agent that speaks now (``kind``: ack, steer, status, social). A stop is one of two things,
    and Jev says which (the part's kind), apart from which request it is about: ``cancel`` lists the requests
    the speaker withdrew (their runs are aborted). ``hush`` is ``[{agentId, requestIds}]``: the agents told to
    stop talking with nothing withdrawn (their speech stops, their work goes on), each with the requests the
    stop was about, or none for everything the agent has to say. ``called``: the agent the
    line turned to and asked nothing new of ("go ahead"). The other fields are what a worker before ``ops`` reads."""
    members = [
        {**m, "listenMode": "active"} if m.get("kind") == "agent" else m
        for m in await messaging_repo.list_members(room_id)
    ]
    caller, label = _meeting_actor(members, speaker_id, setting == "meeting")
    meta, _ = await _room_team(owner_id, caller, room_id)
    if wake:
        utterance = _WAKE_WORD.sub("", utterance, count=1) or utterance
    name = label or await messaging.human_display_name(speaker_id)
    author = {"kind": "human", "id": speaker_id, "displayName": name}
    history = _transcript_history(transcript, members, utterance, at)
    team = {m["id"]: m.get("displayName") or m["id"] for m in members if m.get("kind") == "agent"}
    floor_agent = (floor or {}).get("agent") if (floor or {}).get("agent") in team else None
    mine = bool(floor_agent and (floor or {}).get("mine"))
    earlier = [
        {**r, "agent": team[r["agentId"]]}
        for r in open_requests or []
        if r.get("agentId") in team and r.get("id") and r.get("task")
    ]
    # A worker before ``open`` sends its running work as ``voiceRuns`` ({agent, request}, no ids): the room
    # judge still sees it ("X is answering ..."); nothing can be steered or called off by id.
    running = [
        {"agentId": r.get("agent"), "task": r.get("request"), "status": "working"}
        for r in voice_runs or []
        if r.get("agent") in team
    ]
    try:
        cards = dict(
            zip(
                team,
                await asyncio.wait_for(
                    asyncio.gather(*(jev.agent_card(owner_id, a, n) for a, n in team.items())), _ROUTING_TIMEOUT_S
                ),
            )
        )
    except Exception:  # noqa: BLE001 -- a card read never stops a line: names alone are thinner, not wrong
        cards = dict(team)
    meeting = setting == "meeting"
    talking = [e for e in engagements or [] if e.get("agentId") in team]
    floor_line = f"{team[floor_agent]} is talking with {floor.get('speaker')}" if floor_agent else None
    if meeting and talking:
        # the understanding call is told the same thing the decision below rests on
        with_agent = team[talking[0]["agentId"]]
        floor_line = (
            f"{with_agent} is talking with {name}"
            if talking[0].get("state") == "active"
            else f"{name} was talking with {with_agent} and has turned to other people"
        )
    message = {"author": author, "text": utterance}
    candidates = sorted(team)

    async def someone() -> str | None:
        """The agent already in this conversation, else the chief of staff (openclaw_work's fallback)."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return await openclaw_work._fallback_single(owner_id, candidates, message, history)

    # People talking among themselves (two or more, no wake word, not the floor conversation, no meeting
    # rule): the room judge decides whether an agent answers. With no name on the line it runs beside the
    # understanding call; a line that names someone waits for the addressing labels.
    crowd = humans > 1 and not (wake or mine or unclaimed or meeting)
    targets = messaging.select_dispatch_targets(members, [], None, room_kind=meta.get("kind"))

    async def judge(people: list[str], to_people: bool, stage: dict | None = None) -> str | None:
        """The one agent a good teammate would be here, or None (nobody). Jev down: chat's fallback.
        ``stage``: what the addressing labels said (people named, something also asked of the room), as
        chat stores it on a message, so "what's our MRR, and Sam where did you put the deck?" reads as both."""
        asked = {**message, "mentions": [{"kind": "human", "id": p} for p in people], "dispatchTargets": targets}
        if stage:
            asked["addressing"] = stage
        try:
            decision = await asyncio.wait_for(
                jev._decide_for_message(
                    meta,
                    asked,
                    asked,
                    members,
                    history,
                    to_people=to_people,
                    placement=False,
                    open_lines=_open_voice_lines(floor, earlier or running, members),
                    setting=setting,
                ),
                _ROUTING_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 -- a timeout or a card read: Jev is down for this line
            put_metric("voice.routing.timeout")
            decision = {"fallback": True}
        who, forced = decision.get("who"), decision.get("forced")
        if not forced and who in team:
            return who
        if not forced and who == "none":
            return None
        if forced:
            put_metric("messaging.jev.forced", dimensions={"reason": forced})
        return None if to_people else await someone()

    unnamed = not any(c["agents"] or c["humans"] for c in messaging.address_candidates(utterance, members, live=True))
    early = asyncio.ensure_future(judge([], False)) if crowd and unnamed and team else None
    try:
        addressing = await messaging.address(
            message,
            members,
            history=history,
            live=True,
            understand={"cards": cards, "open": earlier, "floor": floor_line, "meeting": meeting},
        )
    except BaseException:
        if early:
            early.cancel()
        raise
    hold = {"hold": True} if addressing["hold"] else {}
    if hold:
        put_metric("voice.routing.jev", dimensions={"outcome": "hold", "setting": setting})
    logger.info("voice route labels room=%s setting=%s %s", room_id, setting, _labels(addressing))
    if addressing["path"] == "jev" and not addressing["named"] and not addressing["humans"]:
        # A named request never comes back as nothing: the classifier called the name no name (ours are
        # weekdays) or only talked about, and the line asks that agent something in so many words.
        if (asked := _asked_by_name(addressing, utterance, members)) is not None:
            addressing = {**addressing, "named": [(*asked, "addressed")]}
            put_metric("voice.routing.named_by_words", dimensions={"setting": setting})
    named = [agent for _, agent, _ in addressing["named"]]
    to_people = bool(addressing["humans"]) and not named and not addressing["anyone"] and not wake
    if meeting:
        # A meeting is people talking to each other with the agents in the room. A line with no name on it
        # is an agent's only on evidence that its speaker is talking with that agent (``_engaged``).
        # (Jev down: the line was not read at all, so nothing says it is not for a person)
        unread = addressing["path"] == "fallback"
        engaged = None if named or wake or unclaimed or unread else _engaged(addressing, talking, earlier)
        claimed = mine or bool(talking)  # the worker sent it as someone's conversation with an agent
        mine, floor_agent = engaged is not None, engaged or floor_agent
        if claimed and not (mine or named or wake or unclaimed):
            to_people = unread or _said_to_people(addressing, talking)
    implied = bool(named) or wake or mine or unclaimed or (humans <= 1 and not meeting)
    picked = None  # the room judge's agent for a line among people
    if early and named:
        early.cancel()  # a fuzzy name the candidates missed: the name rules decide
    elif crowd and not named and team:
        picked = await (early or judge(addressing["humans"], to_people, messaging._stored_addressing(addressing)))
        implied, to_people = picked is not None, picked is None

    if addressing["path"] == "fallback":
        # Jev down: nothing was cut or typed. A line said to the agents (a name, the wake word, a question to
        # the room nobody took) is one whole-line request, as before understanding. A line only the floor or a
        # lone speaker implies is not (10-04 prod: side talk became runs): the floor voice answers it itself,
        # and may still delegate it.
        u = {"requests": [], "talk": mine, "people": False, "more": False, "cancel": [], "steer": []}
        if named:
            u["requests"] = [{"agentId": a, "task": utterance, "by": "name"} for a in named]
        elif (wake or unclaimed) and not to_people and candidates:
            u["requests"] = [{"agentId": None, "task": utterance, "by": ""}]
    elif to_people:
        u = {"requests": [], "talk": False, "people": True, "more": False, "cancel": [], "steer": []}
    else:
        u = _understood(
            addressing,
            utterance,
            implied=implied and bool(candidates),
            open_requests=earlier,
            agents=set(team),
        )
        if (unclaimed or wake) and u["people"] and not u["requests"] and candidates:
            # a question to the room nobody took (the worker waited), or a wake-word line, that Jev heard as
            # people talk: it was said to the agents, so one agent answers it. Small talk stays small talk.
            u["requests"] = [{"agentId": None, "task": utterance, "by": ""}]
    stop = bool(u.get("stop")) and not u["cancel"]  # called off, and nothing open to call off
    if picked and not u["requests"] and not u["cancel"] and not u["steer"] and not u.get("nudge") and not stop:
        # the judge heard something to answer where the parts saw talk between people: the line is its request
        u["requests"] = [{"agentId": None, "task": utterance, "by": ""}]
    elif picked and len(u["requests"]) == 1 and u["requests"][0]["by"] == "fit":
        # One ask among people: the judge read the whole conversation (a follow-up goes to the teammate
        # already in the exchange), the fit only the part. Several asks keep their own best fit.
        u["requests"][0] |= {"agentId": None, "by": ""}
    # The worker lists what is already being worked on: a request that says the same thing again (a late
    # final repeating what the voice already delegated) is not made twice, whichever path made it. An answer
    # that is ready and not yet heard counts: the repeat would run it again beside it.
    # Asked by name of another agent than the one already on it ("Gauge, what are the open PRs" while Forge
    # has that list) is a request of its own.
    pending = [(r["agentId"], r["task"]) for r in earlier if r["status"] in (*_OPEN_STATUS, "ready")]
    u["requests"] = [
        r
        for r in u["requests"]
        if not any(_same_ask(r["task"], t) and not (r["by"] == "name" and r["agentId"] != a) for a, t in pending)
    ]
    chief = None
    for r in u["requests"]:
        if r["agentId"] is None:
            if mine:
                r |= {"agentId": floor_agent, "by": "floor"}
            elif picked:
                r |= {"agentId": picked, "by": "room"}
            else:
                chief = chief or await someone()
                r |= {"agentId": chief, "by": "chief"}
    requests = [r for r in u["requests"] if r["agentId"]]
    social = None
    if not requests and not u["cancel"] and not u["steer"] and u["talk"] and not u["people"] and not stop:
        # Nothing was asked: one agent may say a sentence, when the speech can only be for the agents.
        if named:
            social = named[0]
        elif mine and not meeting:  # a meeting: an agent does not answer chatter it was not named in
            social = floor_agent
        elif implied and not meeting:
            social = await someone()
    steered = [r["agentId"] for r in earlier if r["id"] in {s["requestId"] for s in u["steer"]}]
    # Driven voice: the voice is a mouth, so the router says who speaks NOW and about what. Jev down: nobody.
    # ``text`` is None: Jev returns choices and probabilities, never words, so no line is written in this call.
    say = None
    # Called on, nothing new asked ("Friday, go ahead"): the agent reports on what it has open.
    who = named[0] if named else floor_agent if mine else None
    called = bool(u.get("nudge")) and implied
    theirs = [r for r in earlier if r["status"] in (*_OPEN_STATUS, "ready") and who in (None, r["agentId"])]
    if addressing["path"] == "fallback":
        pass
    elif requests:  # acknowledge the ask (also a stale answer asked again with its correction)
        say = {"agentId": requests[0]["agentId"], "kind": "ack", "task": requests[0]["task"]}
    elif u["cancel"]:
        pass  # a stop is obeyed in silence
    elif steered:
        say = {"agentId": steered[0], "kind": "steer", "task": u["steer"][0]["text"]}
    elif called and theirs:
        # A ready answer needs no line (the worker speaks the answer itself); a working one is named.
        if not any(r["status"] == "ready" for r in theirs):
            say = {"agentId": theirs[0]["agentId"], "kind": "status", "task": theirs[0]["task"]}
    elif stop:
        # Also with nothing open: "Perfect. Friday, stop." after the answer read as small talk, and the
        # agent said "Got it; I will stop now."
        pass
    elif social or (called and who):
        say = {"agentId": social or who, "kind": "social", "task": None}  # small talk; nothing open is nothing new
    ops = {
        "requests": requests,
        "social": social,
        "more": u["more"],
        "cancel": u["cancel"],
        # A stop that withdrew no request tells its agent to stop talking, no more: the agent of the request
        # it is about, else the named ones, else the one the speaker is talking with. The worker stops that
        # speech and leaves the work running. Each stop on the line has its own entry.
        "hush": [{"agentId": r["agentId"], "requestIds": [r["id"]]} for r in earlier if r["id"] in u.get("hush", ())]
        + [
            {"agentId": a, "requestIds": []}
            for a in dict.fromkeys(a or (floor_agent if mine else None) for a in u.get("quiet", ()))
            if a
        ],
        "called": who if called and who and not requests else None,
        "steer": u["steer"],
        # the line is talk between people: a meeting voice pinned to its speaker gives the floor back
        "people": bool(u["people"]) and not requests,
        "say": say and {**say, "text": None},
    }
    # What a worker before ``ops`` reads. It hands the WHOLE line to every agent in ``agentIds`` unless
    # ``requests`` splits it, and it reads ``requests`` only for a line that names two or more agents. So
    # it is told what the router told it before understanding: the agents the line names, each with its
    # part; else one agent (the first owner, the steered request's agent, or whoever replies to small talk).
    by_name = list(dict.fromkeys(r["agentId"] for r in requests if r["by"] == "name"))
    order = by_name or ([r["agentId"] for r in requests] or steered or ([social] if social else []))[:1]
    outcome = "routed" if requests else "social" if social else "none"
    put_metric("voice.routing.jev", dimensions={"outcome": outcome, "setting": setting})
    logger.info(
        "voice route decision room=%s outcome=%s requests=%s steer=%s cancel=%s hush=%s say=%s people=%s",
        room_id,
        outcome,
        [(r["agentId"], r["by"]) for r in requests],
        [x["requestId"] for x in u["steer"]],
        u["cancel"],
        [x["agentId"] for x in ops["hush"]],
        say and (say["agentId"], say["kind"]),
        ops["people"],
    )
    routed = {**_routed(*order), **hold, "ops": ops}
    if len(by_name) >= 2:
        routed["requests"] = [
            {"agentId": a, "task": ". ".join(r["task"] for r in requests if r["agentId"] == a and r["by"] == "name")}
            for a in by_name
        ]
    return routed


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
