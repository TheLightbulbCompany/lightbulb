# apps/backend/tests/unit/services/test_session_presence.py
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import local_sessions as sessions, session_presence as presence

NOW = 1_000_000


def _row(**over):
    row = {
        "ownerId": "org_a",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "generation": "g1",
        "humanUserId": "user_owner",
        "deviceId": "mac_a",
        "connectionId": "c1",
        "deviceFeatures": ["sessionPresence"],
        "allowedSenders": ["user_aden"],
    }
    return {**row, **over}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(presence, "_clock", lambda: NOW)
    monkeypatch.setattr(
        presence, "human_display_name", AsyncMock(side_effect=lambda u: u.removeprefix("user_").title())
    )


def _rows(viewers=(), typed=()):
    async def query(pk, begins_with):
        assert begins_with == "VIEWER#", "presence reads only the VIEWER rows, never the typing history"
        # A typist typed 2 s ago; everyone else last typed a minute ago.
        return [{"userId": u, "expiresAt": NOW + 30, "typedAt": NOW - (2 if u in typed else 60)} for u in viewers] + [
            {"userId": "user_old", "expiresAt": NOW - 1, "typedAt": NOW}
        ]

    return query


@pytest.mark.asyncio
async def test_entry_names_viewers_typists_and_pending_requests(monkeypatch):
    monkeypatch.setattr(
        presence.store, "_query", _rows(viewers=["user_aden", "user_mira", "user_owner"], typed=["user_aden"])
    )
    row = _row(
        joinRequests={
            "user_zoe": {"state": "pending", "requestId": "r1"},
            "user_bo": {"state": "declined", "requestId": "r0"},
        }
    )
    out = await presence.entry(row, 123)
    assert out["paused"] is False and out["at"] == 123
    assert out["viewers"] == [
        {"userId": "user_aden", "name": "Aden", "canType": True, "typing": True},
        {"userId": "user_mira", "name": "Mira", "canType": False, "typing": False},
    ]
    assert out["joinRequests"] == [{"userId": "user_zoe", "name": "Zoe", "requestId": "r1"}]


@pytest.mark.asyncio
async def test_ack_lists_only_sessions_with_something_to_show_and_stays_in_the_envelope(monkeypatch):
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=["user_aden"]))
    rows = {f"s{i}": _row(sessionId=f"s{i}", generation="g" * 100) for i in range(24)}
    rows["quiet"] = _row(sessionId="quiet")
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=lambda owner, sid: rows[sid]))
    monkeypatch.setattr(presence.human_display_name, "side_effect", lambda u: "N" * 120)
    quiet_query = _rows()

    async def query(pk, begins_with):
        return await (quiet_query if pk.endswith("quiet") else _rows(viewers=["user_aden"]))(pk, begins_with)

    monkeypatch.setattr(presence.store, "_query", query)
    ack = await presence.ack_with_presence({"type": "local.device.heartbeat.ack"}, "org_a", list(rows))
    assert "quiet" not in [e["sessionId"] for e in ack["presence"]]
    assert ack["presenceAt"] == NOW * 1000 and {e["at"] for e in ack["presence"]} == {NOW * 1000}
    assert len(json.dumps(ack, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES


@pytest.mark.asyncio
async def test_ack_never_trims_an_entry_that_holds_a_join_request(monkeypatch):
    big = {f"user_v{i}": None for i in range(30)}
    rows = {f"q{i}": _row(sessionId=f"q{i}", generation="g" * 100) for i in range(20)}
    rows |= {
        f"r{i}": _row(sessionId=f"r{i}", joinRequests={"user_zoe": {"state": "pending", "requestId": f"req{i}"}})
        for i in range(4)
    }
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=lambda owner, sid: rows[sid]))
    monkeypatch.setattr(presence.human_display_name, "side_effect", lambda u: "N" * 120)
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=list(big)))
    ack = await presence.ack_with_presence({"type": "local.device.heartbeat.ack"}, "org_a", list(rows))
    listed = [e["sessionId"] for e in ack["presence"]]
    assert {f"r{i}" for i in range(4)} <= set(listed)
    assert len(json.dumps(ack, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES


@pytest.mark.asyncio
async def test_ack_drops_viewers_then_presence_rather_than_a_join_request(monkeypatch):
    one = {"user_zoe": {"state": "pending", "requestId": "req"}}
    many = {f"user_p{i}": {"state": "pending", "requestId": f"req{i}"} for i in range(8)}
    rows = {f"r{i}": _row(sessionId=f"r{i}", joinRequests=one) for i in range(24)}
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=lambda owner, sid: rows[sid]))
    monkeypatch.setattr(presence.human_display_name, "side_effect", lambda u: "N" * 120)
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=[f"user_v{i}" for i in range(30)]))
    ack = await presence.ack_with_presence({"type": "local.device.heartbeat.ack"}, "org_a", list(rows))
    assert len(ack["presence"]) == 24 and all(e["viewers"] == [] and e["joinRequests"] for e in ack["presence"])
    rows = {f"r{i}": _row(sessionId=f"r{i}", joinRequests=many) for i in range(24)}
    ack = await presence.ack_with_presence({"type": "local.device.heartbeat.ack"}, "org_a", list(rows))
    assert "presence" not in ack, "requests alone overflow: send none, so the Mac keeps every freeze"


@pytest.mark.asyncio
async def test_ack_survives_a_read_failure(monkeypatch):
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=RuntimeError("ddb")))
    ack = {"type": "local.device.heartbeat.ack"}
    assert await presence.ack_with_presence(ack, "org_a", ["s1"]) == ack


@pytest.mark.asyncio
async def test_push_sends_an_empty_entry_once_the_request_is_answered(monkeypatch):
    monkeypatch.setattr(presence.store, "_query", _rows())
    send = AsyncMock(return_value=True)
    from core.services import local_session_relay

    monkeypatch.setattr(
        local_session_relay,
        "get_local_session_relay",
        lambda: SimpleNamespace(management=SimpleNamespace(send_message_async=send)),
    )
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(return_value=_row(joinRequests={})))
    stale = _row(joinRequests={"user_zoe": {"state": "pending", "requestId": "r1"}})
    await presence.push(stale)  # the caller's copy is older than the row: push reads it again
    frame = send.await_args.args[1]
    assert frame["type"] == "local.session.presence" and frame["joinRequests"] == [] and frame["viewers"] == []
    assert frame["at"] == NOW * 1000


@pytest.mark.asyncio
async def test_push_skips_a_mac_without_the_feature(monkeypatch):
    send = AsyncMock()
    from core.services import local_session_relay

    monkeypatch.setattr(
        local_session_relay,
        "get_local_session_relay",
        lambda: SimpleNamespace(management=SimpleNamespace(send_message_async=send)),
    )
    await presence.push(_row(deviceFeatures=["liveTerminal"]))
    send.assert_not_awaited()


def test_input_paused_is_public_only_with_a_pending_request_on_a_presence_mac(monkeypatch):
    monkeypatch.setattr(sessions, "session_terminal", lambda row: "held")
    base = _row(
        onlineUntil=9_999_999_999,
        capabilitiesVerified=True,
        provider="claude",
        status="idle",
        eventSeq=1,
        providerVersion="1",
        activeTurnId=None,
        capabilities={},
    )
    pending = {"joinRequests": {"user_zoe": {"state": "pending", "requestId": "r1"}}}
    assert sessions._session_public({**base, **pending}, "user_aden").input_paused is True
    assert sessions._session_public(base, "user_aden").input_paused is False
    assert sessions._session_public({**base, **pending, "deviceFeatures": []}, "user_aden").input_paused is False
