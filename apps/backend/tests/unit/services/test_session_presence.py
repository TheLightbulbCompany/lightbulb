# apps/backend/tests/unit/services/test_session_presence.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import local_sessions as sessions, session_links, session_presence as presence

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
    async def query(pk, begins_with, limit=None):
        assert begins_with == "VIEWER#", "presence reads only the VIEWER rows, never the typing history"
        if pk.startswith("PUBWATCH#"):  # an "Anyone" link's seats: one bounded read
            assert limit == session_links.MAX_LINK_VIEWERS + 1  # the cap differs by environment (T18)
            return [{"expiresAt": NOW + 30}, {"expiresAt": NOW - 1}]
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
        },
        pickupRequests={
            "user_zoe": {"state": "pending", "requestId": "p1"},
            "user_al": {"state": "pending", "requestId": "p2"},
            "user_bo": {"state": "declined", "requestId": "p0"},
        },
        deviceFeatures=["pickupRequests", "sessionPresence"],
    )
    out = await presence.entry(row, 123)
    assert out["paused"] is False and out["at"] == 123
    assert out["viewers"] == [
        {"userId": "user_aden", "name": "Aden", "canType": True, "typing": True},
        {"userId": "user_mira", "name": "Mira", "canType": False, "typing": False},
    ]
    assert out["joinRequests"] == [
        {"userId": "user_al", "name": "Al", "requestId": "p2", "kind": "pickup"},
        {"userId": "user_zoe", "name": "Zoe", "requestId": "r1", "kind": "join"},
        {"userId": "user_zoe", "name": "Zoe", "requestId": "p1", "kind": "pickup"},
    ]
    # Pick-up turned on answers every pick-up request.
    assert [r["kind"] for r in (await presence.entry({**row, "pickupAllowed": True}, 123))["joinRequests"]] == ["join"]
    # A Mac that does not advertise pickupRequests would call a pick-up "asked to join": it gets none.
    older = {**row, "deviceFeatures": ["sessionPresence"]}
    assert [r["kind"] for r in (await presence.entry(older, 123))["joinRequests"]] == ["join"]


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


def test_input_paused_only_for_a_presence_mac_without_band_owner_only(monkeypatch):
    """An older presence Mac drops remote typing while a request waits; one that advertises
    bandOwnerOnly strips band keys from remote input instead and never pauses."""
    monkeypatch.setattr(sessions, "session_terminal", lambda row: "held")
    base = _row(
        onlineUntil=9_999_999_999,
        capabilitiesVerified=True,
        provider="claude",
        status="idle",
        lastEventAt=1,
        providerVersion="1",
        activeTurnId=None,
        capabilities={},
    )
    pending = {"joinRequests": {"user_zoe": {"state": "pending", "requestId": "r1"}}}
    assert sessions._session_public({**base, **pending}, "user_aden").input_paused is True
    assert sessions._session_public(base, "user_aden").input_paused is False
    assert sessions._session_public({**base, **pending, "deviceFeatures": []}, "user_aden").input_paused is False
    pickup = {"pickupRequests": {"user_zoe": {"state": "pending", "requestId": "p1"}}}
    # An older Mac never shows the pick-up request, so nothing freezes there for it.
    assert sessions._session_public({**base, **pickup}, "user_aden").input_paused is False
    older = {"deviceFeatures": ["pickupRequests", "sessionPresence"]}
    assert sessions._session_public({**base, **pickup, **older}, "user_aden").input_paused is True
    newer = {"deviceFeatures": ["bandOwnerOnly", "pickupRequests", "sessionPresence"]}
    assert sessions._session_public({**base, **pending, **pickup, **newer}, "user_aden").input_paused is False


def test_terminal_resize_only_for_a_live_terminal_on_a_mac_that_advertised_remote_resize(monkeypatch):
    """The web sends its panel size only when the session says so (owner ruling 2026-10-14)."""
    terminal = "held"
    monkeypatch.setattr(sessions, "session_terminal", lambda row: terminal)
    base = _row(
        onlineUntil=9_999_999_999,
        capabilitiesVerified=True,
        provider="claude",
        status="idle",
        lastEventAt=1,
        providerVersion="1",
        activeTurnId=None,
        capabilities={},
    )
    resizable = {"deviceFeatures": ["liveTerminal", "remoteInput", "remoteResize"], "terminalResize": True}
    assert sessions._session_public({**base, **resizable}, "user_aden").terminal_resize is True
    # A holder an older build started (the heartbeat did not say terminalResize) on an updated Mac.
    old_holder = {**resizable, "terminalResize": False}
    assert sessions._session_public({**base, **old_holder}, "user_aden").terminal_resize is False
    assert sessions._session_public({**base, "deviceFeatures": ["remoteInput"]}, "user_aden").terminal_resize is False
    terminal = None
    assert sessions._session_public({**base, **resizable}, "user_aden").terminal_resize is False


@pytest.mark.asyncio
async def test_an_anyone_link_session_still_names_its_watchers_and_counts_link_viewers(monkeypatch):
    """Owner ruling: names show as ever; the link's viewers are only a count beside them."""
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=["user_aden", "user_mira"], typed=["user_aden"]))
    public = await presence.entry(_row(shareLink={"audience": "anyone", "publicId": "pub-1"}), 1)
    assert [(v["userId"], v["name"], v["typing"]) for v in public["viewers"]] == [
        ("user_aden", "Aden", True),
        ("user_mira", "Mira", False),
    ]
    assert public["linkViewers"] == 1, "live seats only"
    workspace = await presence.entry(_row(shareLink={"audience": "workspace"}), 1)
    assert workspace["linkViewers"] == 0
    asked = _row(
        shareLink={"audience": "anyone", "publicId": "pub-1"},
        joinRequests={"user_zoe": {"state": "pending", "requestId": "r1"}},
        deviceFeatures=["sessionPresence", "agentMessageAsks"],
    )
    # An agent never asks to message a session any more: only people's asks are listed.
    assert [(r["name"], r.get("text")) for r in (await presence.entry(asked, 1))["joinRequests"]] == [("Zoe", None)]
