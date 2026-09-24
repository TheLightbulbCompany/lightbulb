"""Workspace browser control: stale writers must not publish saves or handbacks."""

import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest

from core.services import browser_control as control


@pytest.fixture
def workspace(monkeypatch):
    row = {
        "owner_id": "org_browser",
        "browser_profile_id": "profile",
        "browser_state": {
            "generation": "current",
            "mode": "human",
            "controller_id": "human",
            "session_id": "session",
            "lease_until": 9999999999,
        },
    }

    async def read(*args, **kwargs):
        return copy.deepcopy(row)

    async def cas(owner, expected, state):
        if row.get("browser_state") != expected:
            return False
        row["browser_state"] = copy.deepcopy(state)
        return True

    async def read_state(owner):
        return copy.deepcopy(row.get("browser_state"))

    monkeypatch.setattr(control.container_repo, "get_browser_state", read_state)
    monkeypatch.setattr(control.container_repo, "get_by_owner_id", read)
    monkeypatch.setattr(control.container_repo, "compare_browser_state", cas)
    monkeypatch.setattr(control.browser_sessions, "owns_session", AsyncMock(return_value={"status": "READY"}))
    monkeypatch.setattr(control, "_discard_uncommitted", AsyncMock())
    monkeypatch.setattr(control, "_delete_snapshot", AsyncMock())
    return row


@pytest.mark.asyncio
async def test_stale_human_release_never_changes_aws(workspace, monkeypatch):
    stream = AsyncMock()
    monkeypatch.setattr(control, "_stream", stream)
    with pytest.raises(control.BrowserError):
        await control.release_human("org_browser", "human", "stale")
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_cannot_acquire_human_browser(workspace, monkeypatch):
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 0)
    with pytest.raises(control.BrowserError):
        await control.acquire_agent("org_browser", "run")


@pytest.mark.asyncio
async def test_failed_save_keeps_pointer_and_browser(workspace, monkeypatch):
    workspace["browser_state"]["saved_key"] = "browser-state/org_browser/previous.json"
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("secret URL")))
    with pytest.raises(control.BrowserError, match="save"):
        await control.release_human("org_browser", "human", "current")
    state = workspace["browser_state"]
    assert state["saved_key"].endswith("previous.json")
    assert state["session_id"] == "session"
    assert "secret" not in state.get("error", "")


@pytest.mark.asyncio
async def test_stale_save_cannot_publish_or_release_new_controller(workspace, monkeypatch):
    monkeypatch.setattr(control, "_stream", AsyncMock())

    async def checkpoint(owner, state):
        workspace["browser_state"] = {"generation": "new", "mode": "human", "controller_id": "other"}
        return {"saved_key": "browser-state/org_browser/old.json", "saved_at": "now"}

    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    with pytest.raises(control.BrowserError):
        await control.release_human("org_browser", "human", "current")
    assert workspace["browser_state"] == {"generation": "new", "mode": "human", "controller_id": "other"}


@pytest.mark.asyncio
async def test_teardown_checkpoints_before_stopping_and_retains_pointer(workspace, monkeypatch):
    order = []

    async def stream(*args):
        order.append("enable" if args[1] else "disable")

    async def checkpoint(owner, state):
        order.append("save")
        return {"saved_key": "browser-state/org_browser/checkpoint.json", "saved_at": "now"}

    async def retire(*args):
        order.append("stop")

    monkeypatch.setattr(control, "_stream", stream)
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    monkeypatch.setattr(control, "_retire", retire)
    await control.teardown("org_browser", workspace)
    assert order == ["disable", "enable", "save", "stop"]
    assert workspace["browser_state"]["session_id"] is None
    assert workspace["browser_state"]["saved_key"].endswith("checkpoint.json")


@pytest.mark.asyncio
async def test_teardown_does_not_stop_after_failed_checkpoint(workspace, monkeypatch):
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("save failed")))
    stop = AsyncMock()
    monkeypatch.setattr(control, "_retire", stop)
    with pytest.raises(control.BrowserError):
        await control.teardown("org_browser", workspace)
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_session_lookup_does_not_start_replacement(workspace, monkeypatch):
    from botocore.exceptions import ClientError
    from unittest.mock import MagicMock

    lookup = AsyncMock(side_effect=ClientError({"Error": {"Code": "ThrottlingException"}}, "GetBrowserSession"))
    monkeypatch.setattr(control.browser_sessions, "owns_session", lookup)
    client = MagicMock()
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    with pytest.raises(ClientError):
        await control._claim("org_browser", "human", True)
    client.start_browser_session.assert_not_called()


@pytest.mark.asyncio
async def test_agent_starts_once_and_renews_same_generation(workspace, monkeypatch):
    from unittest.mock import MagicMock

    workspace["browser_state"] = None
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock())
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    first = await control.acquire_agent("org_browser", "run")
    second = await control.acquire_agent("org_browser", "run")
    assert first["generation"] == second["generation"]
    client.start_browser_session.assert_called_once()
    with pytest.raises(control.BrowserError):
        await control.acquire_agent("org_browser", "other-run")


@pytest.mark.asyncio
async def test_stale_start_cannot_take_a_new_agent_lease(workspace, monkeypatch):
    workspace["browser_state"].update(mode="agent", controller_id="run")
    stream = AsyncMock()
    monkeypatch.setattr(control, "_stream", stream)
    with pytest.raises(control.BrowserError, match="already running"):
        await control._claim("org_browser", "human", True, start_only=True)
    stream.assert_not_awaited()
    assert workspace["browser_state"]["controller_id"] == "run"


@pytest.mark.asyncio
async def test_agent_recovers_expired_transition_without_view_poll(workspace, monkeypatch):
    from unittest.mock import MagicMock

    workspace["browser_state"].update(mode="transition", operation_at=1)
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "replacement"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock())
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_retire", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    await control.acquire_agent("org_browser", "run")
    assert workspace["browser_state"]["mode"] == "agent"
    assert workspace["browser_state"]["session_id"] == "replacement"
    assert workspace["browser_state"].get("error_code") is None


@pytest.mark.asyncio
async def test_legacy_browser_prevents_duplicate_session(workspace, monkeypatch):
    from unittest.mock import MagicMock

    workspace["browser_state"] = None
    client = MagicMock()
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control.browser_sessions, "_ready_session_ids", lambda: ["legacy"])
    with pytest.raises(control.BrowserError, match="previous browser"):
        await control.acquire_agent("org_browser", "run")
    client.start_browser_session.assert_not_called()


@pytest.mark.asyncio
async def test_retry_reuses_start_token_after_aws_timeout(workspace, monkeypatch):
    from unittest.mock import MagicMock

    workspace["browser_state"] = None
    client = MagicMock()
    client.list_browser_sessions.return_value = {"items": []}
    client.start_browser_session.side_effect = [RuntimeError("lost response"), {"sessionId": "original"}]
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock())
    monkeypatch.setattr(control, "_stream", AsyncMock())
    with pytest.raises(control.BrowserError):
        await control._claim("org_browser", "human", True, start_only=True)
    await control._claim("org_browser", "human", True, start_only=True)
    first, retry = client.start_browser_session.call_args_list
    assert first.kwargs["clientToken"] == retry.kwargs["clientToken"]
    assert first.kwargs == retry.kwargs
    assert workspace["browser_state"]["session_id"] == "original"


@pytest.mark.asyncio
async def test_human_navigation_returns_control_after_success(workspace, monkeypatch):
    streams = AsyncMock()
    monkeypatch.setattr(control, "_stream", streams)
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    monkeypatch.setattr(control.container_rpc, "call", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(control, "view", AsyncMock(return_value={"active": True}))
    await control.navigate("org_browser", "human", "current", "https://example.com/")
    assert [call.args[1] for call in streams.await_args_list] == [True, False]
    assert workspace["browser_state"]["mode"] == "human"
    assert "current_url" not in workspace["browser_state"]


@pytest.mark.asyncio
async def test_navigation_failure_keeps_browser_and_human_control(workspace, monkeypatch):
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    monkeypatch.setattr(control.container_rpc, "call", AsyncMock(side_effect=RuntimeError("sensitive URL")))
    with pytest.raises(control.BrowserError, match="Could not open"):
        await control.navigate("org_browser", "human", "current", "https://example.com/")
    assert workspace["browser_state"]["mode"] == "human"
    assert workspace["browser_state"]["session_id"] == "session"
    assert "sensitive" not in workspace["browser_state"]["error"]


@pytest.fixture
def agent_tools(monkeypatch):
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    monkeypatch.setattr(control.browser_sessions, "session_view", AsyncMock(return_value={"active": True}))
    monkeypatch.setattr(control, "ACQUIRE_POLL_SECONDS", 0.01)


@pytest.mark.asyncio
async def test_watching_never_renews_a_human_lease_or_blocks_the_agent(workspace, agent_tools, monkeypatch):
    workspace["browser_state"]["lease_until"] = int(time.time()) + 30
    before = copy.deepcopy(workspace["browser_state"])
    for _ in range(3):  # the controller's own panel polling, and a teammate watching
        await control.view("org_browser", "human")
        await control.view("org_browser", "teammate")
    assert workspace["browser_state"] == before
    workspace["browser_state"]["lease_until"] = int(time.time()) - 1  # the idle lapse
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 5)
    started = time.monotonic()
    got = await control.acquire_agent("org_browser", "run")
    assert time.monotonic() - started < 1  # no wait on a lapsed lease
    assert workspace["browser_state"]["mode"] == "agent"
    assert got["generation"] == workspace["browser_state"]["generation"]
    out = await control.view("org_browser", "human")
    assert out["mode"] == "agent" and out["isController"] is False


@pytest.mark.asyncio
async def test_watching_while_agent_drives_keeps_its_lease(workspace, agent_tools):
    workspace["browser_state"].update(mode="agent", controller_id="run", lease_until=int(time.time()) + 100)
    before = copy.deepcopy(workspace["browser_state"])
    out = await control.view("org_browser", "human")
    assert out["mode"] == "agent" and out["canControl"] is True and out["isController"] is False
    assert workspace["browser_state"] == before


@pytest.mark.asyncio
async def test_human_lease_is_idle_bounded_and_renewed_only_by_input(workspace, agent_tools):
    workspace["browser_state"]["lease_until"] = int(time.time()) + 10
    await control.renew("org_browser", "human", "current")
    lease = workspace["browser_state"]["lease_until"]
    assert abs(lease - (time.time() + control.HUMAN_IDLE_SECONDS)) < 5
    with pytest.raises(control.BrowserError):
        await control.renew("org_browser", "teammate", "current")
    with pytest.raises(control.BrowserError):
        await control.renew("org_browser", "human", "stale")
    workspace["browser_state"]["lease_until"] = int(time.time()) - 1
    with pytest.raises(control.BrowserError):  # a lapsed lease is never revived by a heartbeat
        await control.renew("org_browser", "human", "current")
    assert (await control.view("org_browser", "human"))["isController"] is False


@pytest.mark.asyncio
async def test_agent_waits_for_done_then_acquires(workspace, agent_tools, monkeypatch):
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 5)

    async def done_soon():
        await asyncio.sleep(0.05)
        workspace["browser_state"] = {
            **workspace["browser_state"],
            "mode": "idle",
            "controller_id": None,
            "lease_until": 0,
        }

    clicker = asyncio.create_task(done_soon())
    got = await control.acquire_agent("org_browser", "run")
    await clicker
    assert workspace["browser_state"]["mode"] == "agent"
    assert got["cdpUrl"] == "wss://signed"


@pytest.mark.asyncio
async def test_agent_gives_up_after_the_wait_window(workspace, agent_tools, monkeypatch):
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(control.BrowserError, match="controlled by another"):
        await control.acquire_agent("org_browser", "run")
    assert 0.1 <= time.monotonic() - started < 2
    assert workspace["browser_state"]["mode"] == "human"


def test_acquire_wait_fits_under_the_api_gateway_timeout():
    assert control.ACQUIRE_WAIT_SECONDS + 2 < 30
