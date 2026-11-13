"""Voice calls: one containers-table row per live call, and the ONE close seam.

A call leaves nothing behind: ``close_call`` bills and deletes the row in one transaction.
The row sits on the sparse ``status-index`` while ``call_open`` / ``call_closing`` so the
sweeper finds it with one query.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.billing import metronome_client, voice_billing
from core.billing import metronome_fast_gate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services import (
    container_rpc,
    hermes_profiles,
    livekit_huddles,
    messaging,
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


def _free_row_key(owner_id: str) -> dict:
    return {"owner_id": f"{voice_billing.FREE_KEY_PREFIX}{owner_id}"}


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
    if receipt := call.get("settlementReceipt"):
        # Owner epochs already have a durable usage receipt, not a live-call workflow.
        # Atomically mark it settled alongside the same counter/credit debit used by calls.
        delete = {
            "Update": {
                "TableName": messaging_repo._table().name,
                "Key": receipt,
                "UpdateExpression": "SET billingSettled = :yes",
                "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(billingSettled)",
                "ExpressionAttributeValues": {":yes": True},
            }
        }
    if call["billing"] == "plan":
        # Plan minutes are metered at the phone/meeting lifecycle boundary. Never also debit
        # provider-cost credits or the free daily counter for a window opened on that plan.
        await run_in_thread(_table().meta.client.transact_write_items, TransactItems=[delete])
        put_metric("voice.call.closed", dimensions={"billing": "plan"})
        return
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
        if call.get("settlementReceipt"):
            first["Update"]["UpdateExpression"] = "ADD remaining_microcents :debit"
            first["Update"]["ConditionExpression"] = "attribute_exists(owner_id)"
            first["Update"]["ExpressionAttributeValues"].pop(":hold")
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


_background: set[asyncio.Task] = set()


def _fire(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _problem(status: int, **detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


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


def _voice_requester(members: list[dict], owner_id: str, speaker_id: str) -> str | None:
    """Who a voice run's local-session grant is FOR: the speaker, only when they are a human member of
    this workspace seated in the room (the C8 rule). A Meet speaker (``meet:<room>`` or a label) is
    never a member, so its grant names nobody (it reads only workspace-wide rooms); never the inviting
    owner."""
    member = next((m for m in members if m.get("kind") == "human" and m["id"] == speaker_id), None)
    return speaker_id if member and messaging_repo.member_home(member, owner_id) == owner_id else None


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
        # ponytail: counted before the rename, so a failed write costs one of the 3; counting after
        # would let a burst rename past the limit. A refund on failure if that ever matters.
        if not await messaging_repo.count_voice_edit(owner_id, agent_id, "rename", RENAME_DAILY_LIMIT):
            raise _problem(429, code="rate_limited")
        from core.services import agent_registry

        await agent_registry.update(owner_id, agent_id, {"name": new})
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


PHONE_STALE_S = 25 * 60  # the worker's 20 min cap plus grace: a call still live past this lost its `end`


async def sweep_phone_calls(now: int) -> int:
    """Finish phone calls whose worker never got `end` through, by running the same end path with the capped length."""
    from boto3.dynamodb.conditions import Key

    from routers import voice_phone

    table = messaging_repo._table()
    rows, kw = [], {"KeyConditionExpression": Key("pk").eq(voice_phone.OPEN_CALLS_PK)}
    while True:
        page = await run_in_thread(table.query, **kw)
        rows += page["Items"]
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    done = 0
    for row in rows:
        started = int(row.get("startedAtEpoch", now))
        if now - started < PHONE_STALE_S:
            continue
        try:
            # duration unknown (the worker died): bill 0 rather than guess
            note = "Closed by cleanup: the call's worker never reported its end"
            await voice_phone.end_call(row["sk"], voice_phone.EndIn(durationSec=0, summary=note))
            done += 1
        except HTTPException as exc:
            if exc.status_code != 404:
                logger.warning("stale phone call end failed call=%s", row["sk"], exc_info=True)
            else:  # no such call any more: drop the index row
                await run_in_thread(table.delete_item, Key={"pk": row["pk"], "sk": row["sk"]})
        except Exception:  # noqa: BLE001 -- one bad row never blocks the rest; the next sweep retries
            logger.warning("stale phone call end failed call=%s", row["sk"], exc_info=True)
    return done


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
                await sweep_phone_calls(int(time.time()))
            if await admitted("voice_number_release", interval_s=24 * 3600):
                from core.services import voice_onboarding

                await voice_onboarding.release_lapsed_numbers()
        except Exception:  # noqa: BLE001
            logger.warning("voice call sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
