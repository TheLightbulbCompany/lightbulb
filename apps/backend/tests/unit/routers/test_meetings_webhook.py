"""Recall's status webhook drives the meeting room; realtime speaker events reach LiveKit; the
worker's idle leave pulls the bot."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from main import app  # noqa: E402
from routers import meetings as mod  # noqa: E402

ROOM = "11111111-1111-4111-8111-111111111111"
SECRET = "whsec_" + base64.b64encode(b"k" * 24).decode()
WORKER = "worker-secret"
TEAM = [{"id": "agent-ceo-1", "name": "Ada"}]


def _signed(payload: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return body, {"webhook-id": "msg_1", "webhook-timestamp": stamp, "webhook-signature": f"v1,{sig}"}


def _status(code: str, bot_id: str = "bot-1", sub_code: str | None = None, room: str = ROOM, nonce: str = "n") -> dict:
    return {
        "event": f"bot.{code}",
        "data": {
            "data": {"code": code, "sub_code": sub_code, "updated_at": "2026-10-03T00:00:00Z"},
            "bot": {"id": bot_id, "metadata": {"roomId": room, "nonce": nonce}},
        },
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    minutes = AsyncMock()  # the meeting's minutes onto the month's counter
    monkeypatch.setattr(mod.voice_billing, "add_month_seconds", minutes)
    reported_minutes = AsyncMock()
    monkeypatch.setattr(mod.voice_billing, "report_minutes", reported_minutes)
    meta = {
        "roomId": ROOM,
        "ownerId": "org_a",
        "meeting": {"uid": "u1", "title": "Sync", "status": "bot_created", "recallBotId": "bot-1", "ticketNonce": "n"},
    }

    async def patch_meeting(room_id, fields, drop=(), when=None):
        if when is not None and not when(meta["meeting"]):
            return None
        fields = fields(meta["meeting"]) if callable(fields) else fields
        meta["meeting"] = {k: v for k, v in {**meta["meeting"], **fields}.items() if k not in drop}
        return meta["meeting"]

    async def noop():
        return None

    def notes_done(_room_id):  # the real on_ended marks its own completion
        meta["meeting"]["notesMailedAt"] = 1
        return noop()

    publisher = MagicMock(publish_room=AsyncMock(return_value=True))
    bot_leave, audio_mode = AsyncMock(), AsyncMock(return_value="separate")
    repo = mod.meeting_invites.container_repo
    lk = mod.livekit_huddles
    with (
        patch.object(mod.meeting_notes, "on_ended", MagicMock(side_effect=notes_done)) as notes,
        patch.object(mod.meeting_invites, "advance", MagicMock(side_effect=lambda _r: noop())) as advance,
        patch.object(repo, "try_claim_owner_lifecycle", AsyncMock(return_value="tok")) as claim,
        patch.object(repo, "release_owner_lifecycle_claim", AsyncMock()) as release,
        patch.object(mod.voice_calls, "room_agents", AsyncMock(return_value=TEAM)),
        patch.object(mod.settings, "LIVEKIT_URL", "wss://lk.test"),
        patch.object(mod.settings, "RECALL_API_KEY", "rk"),
        patch.object(mod.meeting_bots.settings, "RECALL_WEBHOOK_SECRET", SECRET),
        patch.object(mod.settings, "HUDDLE_WORKER_SECRET", WORKER),
        patch.object(mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta)),
        patch.object(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting)),
        patch.object(mod.meeting_invites, "_post_line", AsyncMock()) as line,
        patch.object(mod.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"})),
        patch.object(lk, "dispatch_meet", AsyncMock()) as dispatch,
        patch.multiple(mod.meeting_bots, leave=bot_leave, audio_mode=audio_mode),
        patch.object(lk, "close_room", AsyncMock()) as close,
        patch.object(lk, "send_meet_speaker", AsyncMock()) as speaker,
        patch.object(mod.voice_calls, "prewake_owner", MagicMock()) as wake,
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
        patch.object(mod, "put_metric") as metric,
    ):
        yield {
            "client": TestClient(app),
            "meta": meta,
            "dispatch": dispatch,
            "close": close,
            "speaker": speaker,
            "wake": wake,
            "line": line,
            "publisher": publisher,
            "metric": metric,
            "minutes": minutes,
            "reported_minutes": reported_minutes,
            "notes": notes,
            "release": release,
            "claim": claim,
            "advance": advance,
            "bot_leave": bot_leave,
            "audio_mode": audio_mode,
        }


def _post(client, payload, path="/api/v1/webhooks/recall", secret=SECRET):
    body, headers = _signed(payload, secret)
    return client.post(path, content=body, headers={**headers, "content-type": "application/json"})


def test_in_call_dispatches_the_worker_once_and_prewakes(env):
    assert _post(env["client"], _status("in_call_recording")).status_code == 200
    env["dispatch"].assert_awaited_once()
    room_id, metadata = env["dispatch"].await_args.args
    assert room_id == ROOM
    assert metadata == {
        "roomId": ROOM,
        "ownerId": "org_a",
        "roomToken": mod.livekit_huddles.room_token(ROOM, "org_a"),
        "source": "meet",
        "company": "Acme",
    }
    env["wake"].assert_called_once_with("org_a", team=TEAM, room_id=ROOM)
    assert env["meta"]["meeting"]["status"] == "in_call"
    event = env["publisher"].publish_room.await_args.args[1]
    assert event["type"] == "room.meeting.changed" and event["payload"] == {"roomId": ROOM, "status": "in_call"}

    # A replay, and Meet's second in-call status, dispatch nothing more.
    assert _post(env["client"], _status("in_call_recording")).status_code == 200
    assert _post(env["client"], _status("in_call_not_recording")).status_code == 200
    env["dispatch"].assert_awaited_once()
    env["wake"].assert_called_once()


def test_unpaid_opted_in_voice_plan_leaves_meet_without_starting_a_worker_or_legacy_debit(env):
    with patch.object(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"metronome_voice_contract_id": "voice", "voice_plan_status": "pending_payment"}),
    ):
        assert _post(env["client"], _status("in_call_recording")).status_code == 200
    assert env["meta"]["meeting"]["botError"] == "voice_payment_required"
    env["bot_leave"].assert_awaited_once_with("bot-1")
    env["dispatch"].assert_not_awaited()
    env["minutes"].assert_not_awaited()


def test_call_ended_closes_the_room_once(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    _post(env["client"], _status("call_ended", sub_code="call_ended_by_host"))
    _post(env["client"], _status("done"))
    assert env["meta"]["meeting"]["status"] == "ended"
    env["close"].assert_awaited_once_with(ROOM, meet=True)
    env["metric"].assert_any_call("meeting.ended", dimensions={"sub_code": "call_ended_by_host"})
    env["notes"].assert_called_once_with(ROOM)
    env["minutes"].assert_awaited_once()  # the meeting's time goes on the month's voice minutes
    owner, seconds, _at = env["minutes"].await_args.args
    assert owner == "org_a" and seconds > 0


def test_meeting_retries_reuse_original_usage_event_after_terminal_write(env):
    start = 1790980000
    env["meta"]["meeting"].update(status="in_call", inCallAt=start)
    stamp = datetime.fromtimestamp(start + 160, timezone.utc).isoformat()
    assert _post(env["client"], _at_time(_status("call_ended"), stamp)).status_code == 200
    assert _post(env["client"], _at_time(_status("done"), None)).status_code == 200
    assert _post(env["client"], _at_time(_status("call_ended"), None)).status_code == 200
    expected = (
        "org_a",
        f"meeting:{ROOM}:{start}",
        datetime.fromtimestamp(start, timezone.utc).isoformat(),
        160,
        "meeting",
    )
    assert [call.args for call in env["reported_minutes"].await_args_list] == [expected, expected, expected]


@pytest.mark.asyncio
async def test_failed_first_meeting_meter_admission_is_retried_from_frozen_ended_fields(env):
    start = 1790980000
    env["meta"]["meeting"].update(status="in_call", inCallAt=start)
    env["reported_minutes"].side_effect = [RuntimeError("messaging table unavailable"), None]

    with pytest.raises(RuntimeError, match="not durably admitted"):
        await mod._on_status("bot-1", ROOM, "call_ended", "call_ended_by_host")
    assert env["meta"]["meeting"]["status"] == "ended"
    await mod._on_status("bot-1", ROOM, "done", "call_ended_by_host")

    expected = (
        "org_a",
        f"meeting:{ROOM}:{start}",
        datetime.fromtimestamp(start, timezone.utc).isoformat(),
        max(0, int(env["meta"]["meeting"]["endedAt"]) - start),
        "meeting",
    )
    assert [call.args for call in env["reported_minutes"].await_args_list] == [expected, expected]


@pytest.mark.asyncio
async def test_failed_meeting_admission_cannot_advance_series_before_terminal_retry(env):
    start = 1790980000
    env["meta"]["meeting"].update(status="in_call", inCallAt=start, recurrence={"frequency": "weekly"})
    env["reported_minutes"].side_effect = [RuntimeError("outbox unavailable"), None]
    notes_finished = asyncio.Event()
    notes_calls = 0

    async def notes_then_advance(_room_id):
        nonlocal notes_calls
        notes_calls += 1
        await env["advance"](ROOM)
        notes_finished.set()

    env["notes"].side_effect = notes_then_advance

    with pytest.raises(RuntimeError, match="not durably admitted"):
        await mod._on_status("bot-1", ROOM, "call_ended", "call_ended_by_host")
    await asyncio.sleep(0)  # let any incorrectly queued notes work run
    assert env["advance"].call_count == 0
    assert notes_calls == 0

    # Recall's terminal redelivery retries the same frozen occurrence before releasing notes.
    await mod._on_status("bot-1", ROOM, "done", "call_ended_by_host")
    await asyncio.wait_for(notes_finished.wait(), timeout=1)
    assert notes_calls == 1
    env["advance"].assert_called_once_with(ROOM)
    expected = (
        "org_a",
        f"meeting:{ROOM}:{start}",
        datetime.fromtimestamp(start, timezone.utc).isoformat(),
        max(0, int(env["meta"]["meeting"]["endedAt"]) - start),
        "meeting",
    )
    assert [call.args for call in env["reported_minutes"].await_args_list] == [expected, expected]


@pytest.mark.asyncio
async def test_meeting_metering_id_is_stable_per_occurrence_and_distinct_for_a_reused_room(env):
    start = 1790980000
    await mod._count_minutes(ROOM, 160, start)
    await mod._count_minutes(ROOM, 160, start)  # the same occurrence gets Metronome's same dedupe key
    await mod._count_minutes(ROOM, 120, start + 604800)  # the next week's meeting uses the same room
    reports = env["reported_minutes"].await_args_list
    assert reports[0] == reports[1]
    assert reports[0].args[1] != reports[2].args[1]
    assert reports[2].args[3:] == (120, "meeting")


def test_allowance_counter_failure_does_not_lose_meeting_metering_or_cleanup(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    env["minutes"].side_effect = RuntimeError("ddb unavailable")
    assert _post(env["client"], _status("call_ended")).status_code == 200
    env["reported_minutes"].assert_awaited_once()
    env["close"].assert_awaited_once_with(ROOM, meet=True)


def _at_time(payload: dict, stamp) -> dict:
    payload["data"]["data"]["updated_at"] = stamp
    return payload


def test_the_end_time_is_the_events_own_and_written_once(env):
    """A late or retried delivery records when the call ended, not when the webhook landed."""
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    hour_ago = int(time.time()) - 3600
    stamp = datetime.fromtimestamp(hour_ago, timezone.utc).isoformat().replace("+00:00", "Z")
    _post(env["client"], _at_time(_status("call_ended"), stamp))
    _post(env["client"], _at_time(_status("done"), None))
    assert env["meta"]["meeting"]["endedAt"] == hour_ago


@pytest.mark.parametrize("stamp", [None, "not a time"])
def test_an_end_without_a_readable_event_time_falls_back_to_now(env, stamp):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    with patch.object(mod.time, "time", return_value=1790990000.0):
        _post(env["client"], _at_time(_status("fatal", sub_code="meeting_not_found"), stamp))
    assert env["meta"]["meeting"]["endedAt"] == 1790990000


def test_a_fatal_and_an_idle_leave_record_the_end_time_too(env):
    _post(env["client"], _status("fatal", sub_code="meeting_not_found"))
    assert env["meta"]["meeting"]["endedAt"]
    env["meta"]["meeting"] = {"status": "in_call", "inCallAt": 1, "recallBotId": "bot-1"}
    assert _leave(env["client"]).status_code == 200
    assert env["meta"]["meeting"]["status"] == "ended" and env["meta"]["meeting"]["endedAt"]


def test_fatal_knocking_disabled_posts_a_plain_line(env):
    _post(env["client"], _status("fatal", sub_code="google_meet_knocking_disabled"))
    meeting = env["meta"]["meeting"]
    assert meeting["status"] == "bot_failed" and meeting["botError"] == "google_meet_knocking_disabled"
    env["line"].assert_awaited_once()
    assert env["line"].await_args.args[1:] == (
        ROOM,
        "meeting:bot-1:fatal",
        "The host didn't let the agents into **Sync**.",
    )
    _post(env["client"], _status("done"))  # Recall follows a fatal with done: nothing more
    env["line"].assert_awaited_once()
    env["close"].assert_awaited_once()


def test_bad_signature_is_401(env):
    resp = _post(env["client"], _status("in_call_recording"), secret="whsec_" + base64.b64encode(b"x" * 24).decode())
    assert resp.status_code == 401
    env["dispatch"].assert_not_awaited()


@pytest.mark.parametrize(
    "payload", [_status("in_call_recording", bot_id="bot-other"), _status("in_call_recording", room="nope")]
)
def test_unknown_or_mismatched_bot_is_a_noop(env, payload):
    assert _post(env["client"], payload).status_code == 200
    env["dispatch"].assert_not_awaited()
    assert env["meta"]["meeting"]["status"] == "bot_created"


def test_meetings_unavailable_is_a_noop_even_unsigned(env):
    with patch.object(mod.settings, "LIVEKIT_URL", ""):
        assert _post(env["client"], _status("in_call_recording")).status_code == 200
        assert env["client"].post("/api/v1/webhooks/recall", json=_status("done")).status_code == 200
    env["dispatch"].assert_not_awaited()


def test_realtime_speech_on_is_one_speaker_packet(env):
    payload = {
        "event": "participant_events.speech_on",
        "data": {
            "data": {
                "participant": {"id": 7, "name": "Pat"},
                "timestamp": {"absolute": "2026-10-03T00:00:01Z", "relative": 1.0},
            },
            "bot": {"id": "bot-1", "metadata": {"roomId": ROOM}},
        },
    }
    assert _post(env["client"], payload, "/api/v1/webhooks/recall/realtime").status_code == 200
    env["speaker"].assert_awaited_once_with(
        ROOM,
        {"kind": "speech_on", "participantId": "7", "name": "Pat", "speaking": True, "at": 1790985601.0, "roster": []},
    )


def _event(kind: str, pid, name: str) -> dict:
    return {
        "event": f"participant_events.{kind}",
        "data": {
            "data": {"participant": {"id": pid, "name": name}},
            "bot": {"id": "bot-1", "metadata": {"roomId": ROOM}},
        },
    }


def _realtime(env, kind, pid, name):
    assert _post(env["client"], _event(kind, pid, name), "/api/v1/webhooks/recall/realtime").status_code == 200
    return env["speaker"].await_args.args[1]


def test_realtime_relays_the_kind_and_keeps_the_roster_on_the_meeting(env):
    assert _realtime(env, "join", 1, "Pat")["roster"] == ["Pat"]
    assert _realtime(env, "join", 2, "Dan")["roster"] == ["Dan", "Pat"]
    # The bot's own join (Meet lists it as a participant) is never a person.
    assert _realtime(env, "join", 3, "Acme" + mod.meeting_bots._BOT_SUFFIX)["roster"] == ["Dan", "Pat"]
    got = _realtime(env, "leave", 1, "Pat")
    assert got["kind"] == "leave" and got["speaking"] is False and got["roster"] == ["Dan"]
    # Kept on META, so a worker that came in after the joins hears the roster on any later event.
    assert mod._names(env["meta"]["meeting"]["roster"]) == ["Dan"]
    got = _realtime(env, "speech_on", 2, "Dan")
    assert got["kind"] == "speech_on" and got["roster"] == ["Dan"]


def test_an_older_join_that_lands_after_a_newer_leave_does_not_bring_the_person_back():
    """Realtime events are background tasks: a join's write can commit after the leave that followed it."""
    assert (
        mod._rostered({}, "join", "1", "Pat", 1000, host=True)["1"]["host"] is True
    )  # the meeting agent's member check
    left = mod._rostered({}, "leave", "1", "Pat", 2000)
    assert mod._names(mod._rostered(left, "join", "1", "Pat", 1000)) == []
    assert mod._names(mod._rostered(left, "join", "1", "Pat", 3000)) == ["Pat"]
    joined = mod._rostered({}, "join", "1", "Pat", 2000)
    assert mod._names(mod._rostered(joined, "leave", "1", "Pat", 1000)) == ["Pat"]


def test_the_roster_in_a_packet_is_bounded():
    """A 300-person Meet with long names must not push the speaker packet past LiveKit's size limit."""
    roster = {str(i): {"name": f"{i:03d} " + "x" * 200, "at": 1, "in": True} for i in range(300)}
    names = mod._names(roster)
    assert len(names) == 50 and max(map(len, names)) == 80 and len(json.dumps(names)) < 8_000


def test_the_roster_kept_on_the_meeting_is_bounded():
    """A long, large Meet: names are cut and the oldest leavers are dropped before anyone present."""
    roster = {}
    for i in range(150):
        roster = mod._rostered(roster, "join", str(i), "x" * 300, i)
        roster = mod._rostered(roster, "leave", str(i), "", 1000 + i)
    for i in range(150, 300):
        roster = mod._rostered(roster, "join", str(i), f"p{i}", 2000 + i)
    assert len(roster) == 200 and all(len(p.get("name", "")) <= 80 for p in roster.values())
    assert sum(1 for p in roster.values() if p["in"]) == 150 and "0" not in roster and "149" in roster


def test_a_roster_write_refused_for_a_moved_occurrence_relays_nothing(env):
    """The bot id is re-checked inside the write: a series that advanced between the read and the
    write never takes an old bot's join into its next occurrence's roster."""
    with patch.object(mod.meeting_invites, "_patch_meeting", AsyncMock(return_value=None)) as patched:
        assert _post(env["client"], _event("join", 1, "Pat"), "/api/v1/webhooks/recall/realtime").status_code == 200
    env["speaker"].assert_not_awaited()
    when = patched.await_args.kwargs["when"]
    assert when({"recallBotId": "bot-1"}) and not when({"recallBotId": "bot-2"})


def test_realtime_still_relays_when_the_roster_write_fails(env):
    """Best effort: the event goes out with the roster it would have written; the next join retries."""
    with patch.object(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=RuntimeError("ddb"))):
        assert _realtime(env, "join", 1, "Pat")["roster"] == ["Pat"]
    env["metric"].assert_any_call("meeting.roster.write_failed")


def _leave(client):
    return client.request(
        "DELETE",
        f"/api/v1/internal/meetings/{ROOM}",
        json={"ownerId": "org_a", "roomToken": mod.livekit_huddles.room_token(ROOM, "org_a")},
        headers={"X-Lightbulb-Huddle-Secret": WORKER},
    )


def test_worker_idle_leave_ends_once_even_when_call_ended_lands_mid_leave(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)

    async def leave_provokes_call_ended(bot_id):
        assert env["meta"]["meeting"]["status"] == "ended"  # written before the bot is pulled
        assert _post(env["client"], _status("call_ended")).status_code == 200

    with patch.object(mod.meeting_bots, "leave", AsyncMock(side_effect=leave_provokes_call_ended)) as leave:
        assert _leave(env["client"]).status_code == 200
    leave.assert_awaited_once_with("bot-1")
    assert env["meta"]["meeting"]["status"] == "ended"
    env["close"].assert_awaited_once_with(ROOM, meet=True)
    assert env["reported_minutes"].await_count == 2
    assert env["reported_minutes"].await_args_list[0] == env["reported_minutes"].await_args_list[1]
    # The call_ended found the notes not yet done and asked too; on_ended's own claim makes it one run.
    assert env["notes"].call_args_list == [((ROOM,),), ((ROOM,),)]


def test_a_failed_leave_still_ends_the_meeting(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    with (
        patch.object(mod.meeting_bots, "leave", AsyncMock(side_effect=mod.meeting_bots.RecallError("500"))) as leave,
        patch.object(mod.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        assert _leave(env["client"]).status_code == 200
    assert leave.await_count == 3 and [c.args for c in sleep.await_args_list] == [(2,), (2,)]
    assert env["meta"]["meeting"]["status"] == "ended"
    env["close"].assert_awaited_once_with(ROOM, meet=True)
    env["notes"].assert_called_once_with(ROOM)
    assert env["metric"].call_args_list.count((("meeting.leave_failed",),)) == 1


def test_a_leave_that_succeeds_on_a_retry_counts_no_failure(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    flaky = AsyncMock(side_effect=[mod.meeting_bots.RecallError("500"), None])
    with patch.object(mod.meeting_bots, "leave", flaky), patch.object(mod.asyncio, "sleep", AsyncMock()):
        assert _leave(env["client"]).status_code == 200
    assert flaky.await_count == 2
    assert (("meeting.leave_failed",),) not in env["metric"].call_args_list
    env["close"].assert_awaited_once_with(ROOM, meet=True)


def test_the_done_webhook_retries_notes_that_failed_on_call_ended(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)

    async def noop():
        return None

    env["notes"].side_effect = lambda _room: noop()  # the first notes attempt fails: no notesMailedAt
    assert _post(env["client"], _status("call_ended")).status_code == 200
    assert mod.meeting_notes.owed(env["meta"]["meeting"])

    def done(_room):
        env["meta"]["meeting"]["notesMailedAt"] = 1
        return noop()

    env["notes"].side_effect = done
    assert _post(env["client"], _status("done")).status_code == 200  # Recall's `done` follows call_ended
    assert env["notes"].call_count == 2 and not mod.meeting_notes.owed(env["meta"]["meeting"])
    assert _post(env["client"], _status("done")).status_code == 200  # a redelivery: nothing left to do
    assert env["notes"].call_count == 2


def test_a_failed_close_room_loses_no_other_end_step(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    env["close"].side_effect = RuntimeError("twirp")
    assert _post(env["client"], _status("call_ended", sub_code="x")).status_code == 200
    env["notes"].assert_called_once_with(ROOM)
    env["metric"].assert_any_call("meeting.ended", dimensions={"sub_code": "x"})
    env["metric"].assert_any_call("meeting.finish_step_failed", dimensions={"step": "close_room"})
    env["publisher"].publish_room.assert_awaited_once()


def test_fatal_mid_call_runs_the_notes(env):
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    _post(env["client"], _status("fatal", sub_code="bot_kicked_from_call"))
    assert env["meta"]["meeting"]["status"] == "bot_failed"
    env["notes"].assert_called_once_with(ROOM)
    env["metric"].assert_any_call("meeting.ended", dimensions={"sub_code": "bot_kicked_from_call"})
    env["line"].assert_awaited_once()
    env["reported_minutes"].assert_awaited_once()


def test_a_failed_transition_is_5xx_and_releases_the_lease(env):
    with patch.object(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=RuntimeError("ddb"))):
        assert _post(env["client"], _status("in_call_recording")).status_code == 500
    env["release"].assert_awaited_once_with(f"meeting#{ROOM}", token="tok")
    assert env["meta"]["meeting"]["status"] == "bot_created"


def test_a_failed_dispatch_is_5xx_with_in_call_unwritten_so_the_retry_dispatches(env):
    env["dispatch"].side_effect = [RuntimeError("twirp"), None]
    assert _post(env["client"], _status("in_call_recording")).status_code == 500
    assert env["meta"]["meeting"]["status"] == "bot_created"
    env["wake"].assert_not_called()
    assert _post(env["client"], _status("in_call_recording")).status_code == 200  # Recall's retry
    assert env["dispatch"].await_count == 2 and env["meta"]["meeting"]["status"] == "in_call"


def test_in_call_after_ended_revives_nothing(env):
    env["meta"]["meeting"].update(status="ended", notesMailedAt=1)
    assert _post(env["client"], _status("in_call_recording")).status_code == 200
    env["dispatch"].assert_not_awaited()
    env["notes"].assert_not_called()
    assert env["meta"]["meeting"]["status"] == "ended"


def test_a_busy_lease_is_5xx_so_recall_retries(env):
    env["claim"].return_value = None
    assert _post(env["client"], _status("in_call_recording")).status_code == 503
    env["dispatch"].assert_not_awaited()
    env["meta"]["meeting"].update(status="in_call", inCallAt=1)
    assert _leave(env["client"]).status_code == 503
    assert env["meta"]["meeting"]["status"] == "in_call"


def test_a_redelivery_to_an_ended_meeting_retries_unfinished_notes(env):
    env["meta"]["meeting"].update(status="ended", inCallAt=1)
    assert _post(env["client"], _status("done")).status_code == 200
    env["notes"].assert_called_once_with(ROOM)
    env["close"].assert_not_awaited()


def test_a_clerk_blip_still_dispatches(env):
    with patch.object(mod.clerk_admin, "get_organization", AsyncMock(side_effect=RuntimeError("clerk"))):
        assert _post(env["client"], _status("in_call_recording")).status_code == 200
    assert env["dispatch"].await_args.args[1]["company"] == "Your team"


def test_realtime_from_a_stale_bot_is_dropped(env):
    payload = {
        "event": "participant_events.speech_on",
        "data": {
            "data": {"participant": {"id": 7, "name": "Pat"}},
            "bot": {"id": "bot-old", "metadata": {"roomId": ROOM}},
        },
    }
    assert _post(env["client"], payload, "/api/v1/webhooks/recall/realtime").status_code == 200
    env["speaker"].assert_not_awaited()


def test_worker_leave_needs_the_room_token(env):
    resp = env["client"].request(
        "DELETE",
        f"/api/v1/internal/meetings/{ROOM}",
        json={"ownerId": "org_a", "roomToken": "wrong"},
        headers={"X-Lightbulb-Huddle-Secret": WORKER},
    )
    assert resp.status_code == 401


def test_an_ended_call_the_bot_never_joined_runs_no_notes_and_advances(env):
    env["meta"]["meeting"]["status"] = "bot_created"
    assert _post(env["client"], _status("call_ended", sub_code="bot_kicked_from_waiting_room")).status_code == 200
    assert env["meta"]["meeting"]["status"] == "ended"
    env["notes"].assert_not_called()
    env["advance"].assert_called_once_with(ROOM)
    env["reported_minutes"].assert_not_awaited()


def test_a_terminal_redelivery_retries_a_series_advance(env):
    env["meta"]["meeting"].update(status="ended", inCallAt=1, notesMailedAt=1, recurrence={"rule": "FREQ=WEEKLY"})
    assert _post(env["client"], _status("done")).status_code == 200
    env["notes"].assert_not_called()
    env["advance"].assert_called_once_with(ROOM)
    env["meta"]["meeting"].pop("recurrence")
    assert _post(env["client"], _status("done")).status_code == 200  # a one-off: nothing to advance
    env["advance"].assert_called_once()


def test_an_untracked_bot_of_this_booking_is_adopted_then_processed(env):
    meeting = env["meta"]["meeting"]
    del meeting["recallBotId"]
    meeting["status"] = "scheduled"
    assert _post(env["client"], _status("in_call_recording", bot_id="bot-2")).status_code == 200
    meeting = env["meta"]["meeting"]
    assert meeting["recallBotId"] == "bot-2" and meeting["status"] == "in_call"
    env["dispatch"].assert_awaited_once()
    env["bot_leave"].assert_not_awaited()


@pytest.mark.parametrize(
    "mode, stored",
    [("separate", "separate"), (None, None), (RuntimeError("recall down"), None)],
    ids=["per-person", "older-mixed-bot", "unknown"],
)
def test_an_adopted_bot_records_how_it_sends_audio(env, mode, stored):
    """The audio socket and the page both read ``meeting.audio``: an adoption that left it out sent a
    per-person bot back to the mixed stream, and a stale value would open the socket for a mixed bot."""
    meeting = env["meta"]["meeting"]
    meeting.update(status="bot_failed", audio="separate")  # the failed bot's value must not survive
    env["audio_mode"].side_effect = [mode]
    assert _post(env["client"], _status("in_call_recording", bot_id="bot-2")).status_code == 200
    env["audio_mode"].assert_awaited_once_with("bot-2")
    assert env["meta"]["meeting"]["recallBotId"] == "bot-2"
    assert env["meta"]["meeting"].get("audio") == stored


def test_another_environments_bot_is_ignored_not_ejected(env):
    """Recall's status webhook is account-wide: dev's bots report to prod too. Prod must leave them be."""
    payload = _status("in_call_recording", bot_id="bot-dev")
    payload["data"]["bot"]["metadata"]["env"] = "some-other-env"
    assert _post(env["client"], payload).status_code == 200
    env["bot_leave"].assert_not_awaited()
    env["dispatch"].assert_not_awaited()


def test_a_bot_for_a_room_this_environment_lacks_is_ignored_not_ejected(env):
    """An untagged bot (booked before the env tag) whose room is not here belongs to another environment."""
    with patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=None)):
        assert _post(env["client"], _status("in_call_recording", bot_id="bot-dev")).status_code == 200
    env["bot_leave"].assert_not_awaited()
    env["dispatch"].assert_not_awaited()


def test_an_extra_bot_is_told_to_leave(env):
    env["bot_leave"].side_effect = RuntimeError("already gone")
    assert _post(env["client"], _status("in_call_recording", bot_id="bot-2")).status_code == 200
    env["bot_leave"].assert_awaited_once_with("bot-2")
    env["dispatch"].assert_not_awaited()
    assert env["meta"]["meeting"]["recallBotId"] == "bot-1"


@pytest.mark.parametrize("booked", [False, True], ids=["before-the-new-booking", "after-it"])
def test_the_previous_occurrences_bot_cannot_touch_the_next_one(env, booked):
    """advance() re-dated the series and dropped the old nonce: the old bot's late webhooks are
    neither adopted nor able to change the new occurrence."""
    meeting = {"uid": "u1", "title": "Sync", "status": "scheduled", "startAt": 2, "recurrence": {"rule": "FREQ=DAILY"}}
    if booked:
        meeting.update(status="bot_created", recallBotId="bot-2", ticketNonce="n2")
    env["meta"]["meeting"] = dict(meeting)
    for code in ("in_call_recording", "call_ended", "done"):
        assert _post(env["client"], _status(code, bot_id="bot-1", nonce="n")).status_code == 200
    assert env["meta"]["meeting"] == meeting
    env["bot_leave"].assert_awaited_once_with("bot-1")  # the in_call: an extra bot is told to leave
    env["dispatch"].assert_not_awaited()
    env["advance"].assert_not_called()
    env["notes"].assert_not_called()


def test_a_late_redelivery_counts_the_meetings_own_minutes_not_the_delay(env):
    """Recall redelivers for up to 24 h: the minutes are endedAt (the event's time) minus inCallAt."""
    in_call = int(time.time()) - 7200
    env["meta"]["meeting"].update(status="in_call", inCallAt=in_call)
    stamp = datetime.fromtimestamp(in_call + 600, timezone.utc).isoformat().replace("+00:00", "Z")
    _post(env["client"], _at_time(_status("call_ended"), stamp))  # lands two hours after a 10-minute call
    assert env["minutes"].await_args.args[1] == 600


def test_joining_and_lobby_are_observed_without_dispatch_or_billing(env):
    assert _post(env["client"], _status("joining_call")).status_code == 200
    assert env["meta"]["meeting"]["lastBotStatus"] == "joining_call"
    assert _post(env["client"], _status("in_waiting_room")).status_code == 200
    assert env["meta"]["meeting"]["lastBotStatus"] == "in_waiting_room"
    assert env["meta"]["meeting"]["status"] == "bot_created"
    env["dispatch"].assert_not_awaited()
    env["reported_minutes"].assert_not_awaited()
    env["wake"].assert_not_called()


def test_late_joining_event_cannot_regress_confirmed_live_status(env):
    _post(env["client"], _status("in_call_recording"))
    _post(env["client"], _status("in_waiting_room"))
    assert env["meta"]["meeting"]["lastBotStatus"] == "in_call_recording"
    assert env["meta"]["meeting"]["status"] == "in_call"
    env["dispatch"].assert_awaited_once()


def test_older_joining_callback_cannot_overwrite_lobby_presence(env):
    _post(env["client"], _at_time(_status("in_waiting_room"), "2026-10-03T00:00:02Z"))
    _post(env["client"], _at_time(_status("joining_call"), "2026-10-03T00:00:01Z"))
    assert env["meta"]["meeting"]["lastBotStatus"] == "in_waiting_room"
    env["dispatch"].assert_not_awaited()
