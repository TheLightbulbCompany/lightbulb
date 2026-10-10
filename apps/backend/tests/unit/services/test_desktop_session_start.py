import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.services import desktop_session_start as start
from core.services.connection_service import DesktopTargetForbidden
from core.services.desktop_execution import DesktopError
from core.services.desktop_pair import DesktopPermissionGrant

OWNER, USER = "org_1", "user_alice"
START_ID = "6f1c1d52-0f7b-4c1e-9d38-2a6d8f3e7b10"
BODY = {
    "startId": START_ID,
    "deviceId": "mac_1",
    "generation": "gen_1",
    "tool": "claude",
    "folder": "/Users/a/code",
    "prompt": "hi there",
}
TARGET = {
    "taskAddress": "http://self:8000",
    "ownerId": OWNER,
    "memberId": USER,
    "deviceId": "mac_1",
    "clientId": "openclaw-macos",
    "generation": "gen_1",
    "commands": list(start.COMMANDS),
}


class FakeSession:
    generation = "gen_1"

    def __init__(self, result):
        self.result, self.calls = result, []

    async def invoke(self, command, params, timeout_ms, **_):
        self.calls.append((command, params, timeout_ms))
        return self.result


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("DESKTOP_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("DESKTOP_EXECUTION_OWNER_ALLOWLIST", OWNER)
    monkeypatch.setattr("core.services.task_address.own_address", lambda: "http://self:8000")


@pytest.fixture
def relay(monkeypatch, enabled):
    session = FakeSession({"ok": True, "payload": {"started": True}})
    resolve = AsyncMock(return_value=TARGET)
    monkeypatch.setattr("core.services.connection_service.resolve_desktop_target", resolve)
    monkeypatch.setattr("routers.gateway_relay.node_sessions", {(OWNER, "mac_1"): session})
    publisher = AsyncMock()
    monkeypatch.setattr(
        "core.services.live_events.get_live_event_publisher", lambda: type("P", (), {"publish_user": publisher})()
    )
    return session, resolve, publisher


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "bash"},
        {"folder": "relative/path"},
        {"folder": "/a\x00b"},
        {"mode": "bypassPermissions"},
        {"tool": "codex", "mode": "never"},
        {"tool": "codex", "effort": "ultra"},
        {"effort": "auto"},
        {"effort": []},
        {"mode": []},
        {"tool": []},
        {"model": "--dangerously-skip-permissions"},
        {"model": "opus; rm -rf /"},
        {"prompt": "x" * (start.MAX_PROMPT_BYTES + 1)},
        {"argv": ["sh"]},
        {"deviceId": "../mac"},
        {"startId": "not-a-uuid"},
        {"prompt": " mcp "},
        {"prompt": "update"},
        {"source": "ios"},
    ],
)
def test_start_accepts_only_fixed_tools_and_validated_flags(change):
    with pytest.raises(DesktopError):
        start.validate_start(BODY | change)


def test_valid_start_fills_tool_defaults():
    assert (
        start.validate_start(BODY | {"tool": "codex", "mode": "on-request", "model": "gpt-5.5"})["mode"] == "on-request"
    )
    assert start.validate_start(BODY)["model"] is None
    assert start.validate_start(BODY)["mode"] == "plan"
    assert start.validate_start(BODY | {"tool": "codex"})["mode"] == "on-request"
    assert start.validate_start(BODY | {"tool": "codex", "mode": "auto-review", "effort": "high"})["effort"] == "high"
    assert start.validate_start(BODY)["effort"] is None
    assert start.validate_start(BODY | {"prompt": "update the readme"})
    assert start.validate_start(BODY | {"tool": "codex", "prompt": "login"})


def test_session_commands_need_the_shell_grant():
    assert "session.start.v1" in DesktopPermissionGrant(shell=True).mac_commands()
    assert "session.start.v1" not in DesktopPermissionGrant(files=True, screen=True).mac_commands()


async def test_start_is_refused_when_desktop_execution_is_off(monkeypatch):
    monkeypatch.delenv("DESKTOP_EXECUTION_ENABLED", raising=False)
    with pytest.raises(DesktopError) as exc:
        await start.start(OWNER, USER, BODY)
    assert exc.value.code == "desktop_disabled"
    assert await start.list_targets(OWNER, USER) == {"enabled": False, "devices": []}


async def test_a_teammate_cannot_start_on_someone_elses_mac(monkeypatch, enabled):
    # The ready row and the pairing name the Mac's owner; resolution for anyone else fails.
    monkeypatch.setattr(
        "core.services.connection_service.resolve_desktop_target",
        AsyncMock(side_effect=DesktopTargetForbidden("mac_1")),
    )
    with pytest.raises(DesktopError) as exc:
        await start.start(OWNER, "user_bob", BODY)
    assert exc.value.code == "desktop_forbidden"


async def test_a_mac_resolved_for_another_member_is_never_started(monkeypatch, enabled):
    # The Mac launches without a native prompt because only its own paired user gets
    # here; a resolution naming anyone else must be refused even if the lookup allowed it.
    monkeypatch.setattr(
        "core.services.connection_service.resolve_desktop_target",
        AsyncMock(return_value=TARGET | {"memberId": "user_bob"}),
    )
    with pytest.raises(DesktopError) as exc:
        await start.start(OWNER, USER, BODY)
    assert exc.value.code == "desktop_forbidden"


async def test_only_the_native_mac_client_can_be_selected(monkeypatch, enabled):
    monkeypatch.setattr(
        "core.services.connection_service.resolve_desktop_target",
        AsyncMock(return_value=TARGET | {"clientId": "node-host"}),
    )
    with pytest.raises(DesktopError):
        await start.start(OWNER, USER, BODY)


async def test_start_dispatches_to_the_owners_mac_audits_and_reports(relay, caplog):
    session, resolve, publisher = relay
    caplog.set_level("INFO")
    accepted = await start.start(OWNER, USER, BODY | {"mode": "plan", "effort": "high"})
    await asyncio.gather(*start._background)

    resolve.assert_awaited_with(OWNER, USER, "mac_1", "gen_1", "session.start.v1")
    command, params, _ = session.calls[0]
    assert command == "session.start.v1"
    assert params == {
        "tool": "claude",
        "folder": "/Users/a/code",
        "model": None,
        "mode": "plan",
        "effort": "high",
        "prompt": "hi there",
        "startId": accepted["startId"],
    }
    owner, user, event = publisher.await_args.args
    assert (owner, user) == (OWNER, USER)
    assert event["type"] == "runtime.operation.changed"
    assert event["payload"]["operationId"] == accepted["startId"]
    assert event["payload"]["state"] == "succeeded"
    audit = [r.getMessage() for r in caplog.records if "desktop.session.start" in r.getMessage()]
    assert len(audit) == 2 and '"outcome": "started"' in audit[1] and "hi there" not in audit[1]


@pytest.mark.parametrize("mac_ok", [True, False])
@pytest.mark.parametrize("source", ["ios", "web"])
async def test_start_analytics_joins_request_to_mac_outcome_without_content(relay, mac_ok, source):
    session, _, _ = relay
    session.result = {"ok": mac_ok, "error": {"code": "DENIED", "message": "private reason"}}
    with patch.object(start, "capture_product_event") as capture:
        await start.start(OWNER, USER, BODY, source=source)
        capture.assert_not_called()
        await asyncio.gather(*start._background)
    request, outcome = (call.kwargs for call in capture.call_args_list)
    assert (request["event"], outcome["event"]) == (
        "local_session_start_requested",
        "local_session_terminal_opened" if mac_ok else "local_session_start_failed",
    )
    assert (
        request["properties"]
        == outcome["properties"]
        == {
            "workspace_id": OWNER,
            "start_id": START_ID,
            "provider": "claude",
            "source": source,
        }
    )
    assert request["distinct_id"] == outcome["distinct_id"] == USER
    assert request["event_uuid"] != outcome["event_uuid"]
    assert "hi there" not in str(capture.call_args_list)
    assert "/Users/a/code" not in str(capture.call_args_list)
    assert "private reason" not in str(capture.call_args_list)


async def test_a_denied_start_reports_the_macs_reason(relay):
    session, _, publisher = relay
    session.result = {"ok": False, "error": {"code": "DENIED", "message": "Declined on the Mac."}}
    await start.start(OWNER, USER, BODY)
    await asyncio.gather(*start._background)
    payload = publisher.await_args.args[2]["payload"]
    assert payload["state"] == "failed" and payload["error"]["code"] == "DENIED"


async def test_peer_hop_rederives_authority_and_refuses_other_commands(relay):
    with pytest.raises(DesktopError):
        await start.handle_peer(
            {
                "ownerId": OWNER,
                "userId": USER,
                "deviceId": "mac_1",
                "generation": "gen_1",
                "command": "system.run",
                "params": {},
            }
        )
    session, resolve, _ = relay
    await start.handle_peer(
        {
            "ownerId": OWNER,
            "userId": USER,
            "deviceId": "mac_1",
            "generation": "gen_1",
            "command": "session.options.v1",
            "params": {},
        }
    )
    resolve.assert_awaited_with(OWNER, USER, "mac_1", "gen_1", "session.options.v1")
    assert session.calls[-1][0] == "session.options.v1"


async def test_open_in_terminal_is_the_owners_alone_and_sends_only_the_conversation(relay, monkeypatch):
    from core.auth import AuthContext

    session, _, _ = relay
    conversation = "11111111-2222-4333-8444-555555555555"
    row = {
        "humanUserId": USER,
        "deviceId": "mac_1",
        "provider": "claude",
        "nativeConversationId": conversation,
        "terminalReason": "app",
    }
    monkeypatch.setattr("core.services.local_sessions.authorize_session", AsyncMock(return_value=(row, {})))
    monkeypatch.setattr(
        start, "list_targets", AsyncMock(return_value={"devices": [{"deviceId": "mac_1", "generation": "gen_1"}]})
    )
    sid = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(DesktopError) as exc:
        await start.open_in_terminal(AuthContext(user_id="user_bob", org_id=OWNER), sid, {"startId": START_ID})
    assert exc.value.code == "desktop_forbidden"
    with pytest.raises(DesktopError):
        await start.open_in_terminal(AuthContext(user_id=USER, org_id=OWNER), sid, {"startId": START_ID, "folder": "/"})
    monkeypatch.setitem(row, "terminalReason", "plain")
    with pytest.raises(DesktopError) as exc:
        await start.open_in_terminal(AuthContext(user_id=USER, org_id=OWNER), sid, {"startId": START_ID})
    assert exc.value.code == "session_not_openable"
    monkeypatch.setitem(row, "terminalReason", "app")
    await start.open_in_terminal(AuthContext(user_id=USER, org_id=OWNER), sid, {"startId": START_ID})
    await asyncio.gather(*start._background)
    assert session.calls == [
        ("session.start.v1", {"tool": "claude", "conversationId": conversation, "startId": START_ID}, start.START_MS)
    ]


async def test_an_older_mac_rejecting_open_says_to_update_the_app(relay, monkeypatch):
    from core.auth import AuthContext

    session, _, publisher = relay
    session.result = {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "Malformed request."}}
    row = {
        "humanUserId": USER,
        "deviceId": "mac_1",
        "provider": "claude",
        "nativeConversationId": "11111111-2222-4333-8444-555555555555",
        "terminalReason": "app",
    }
    monkeypatch.setattr("core.services.local_sessions.authorize_session", AsyncMock(return_value=(row, {})))
    monkeypatch.setattr(
        start, "list_targets", AsyncMock(return_value={"devices": [{"deviceId": "mac_1", "generation": "gen_1"}]})
    )
    sid = "22222222-2222-4222-8222-222222222222"
    await start.open_in_terminal(AuthContext(user_id=USER, org_id=OWNER), sid, {"startId": START_ID})
    await asyncio.gather(*start._background)
    error = publisher.await_args.args[2]["payload"]["error"]
    assert error["message"] == "Update the Lightbulb app on this Mac, then try again."
