"""Workspace browser, shared like a screen: people never lock it, agent runs fence each
other, and stale writers must not publish saves or stops."""

import asyncio
import copy
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import browser_control as control


@pytest.fixture
def workspace(monkeypatch):
    row = {
        "owner_id": "org_browser",
        "browser_profile_id": "profile",
        "human_input_at": None,
        "browser_state": {
            "generation": "current",
            "mode": "idle",
            "session_id": "session",
            "operation_at": int(time.time()),
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

    async def touch(owner, at, field="human_input_at"):
        row[field] = at

    async def input_at(owner, field="human_input_at"):
        return row.get(field)

    monkeypatch.setattr(control.container_repo, "get_browser_state", read_state)
    monkeypatch.setattr(control.container_repo, "get_by_owner_id", read)
    monkeypatch.setattr(control.container_repo, "compare_browser_state", cas)
    monkeypatch.setattr(control.container_repo, "touch_browser_input", touch)
    monkeypatch.setattr(control.container_repo, "get_browser_input_at", input_at)

    async def set_viewport(owner, width, height):
        row["viewport"] = {"width": width, "height": height}

    async def get_viewport(owner):
        return row.get("viewport")

    monkeypatch.setattr(control.container_repo, "set_browser_viewport", set_viewport)
    monkeypatch.setattr(control.container_repo, "get_browser_viewport", get_viewport)
    monkeypatch.setattr(
        control.container_repo, "get_browser_extensions", AsyncMock(side_effect=lambda o: row.get("browser_extensions"))
    )
    monkeypatch.setattr(control.browser_sessions, "owns_session", AsyncMock(return_value={"status": "READY"}))
    monkeypatch.setattr(control, "_discard_uncommitted", AsyncMock())
    monkeypatch.setattr(control, "_delete_snapshot", AsyncMock())
    monkeypatch.setattr(control.browser_extensions, "_s3", MagicMock)  # the helper + policy objects

    async def set_helper(owner, stamp):
        row["browser_helper"] = stamp

    monkeypatch.setattr(control.container_repo, "set_browser_helper", set_helper)
    return row


@pytest.fixture
def agent_tools(monkeypatch):
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    monkeypatch.setattr(control.browser_sessions, "session_view", AsyncMock(return_value={"active": True}))
    monkeypatch.setattr(control, "ACQUIRE_POLL_SECONDS", 0.01)


def _agent(workspace, run="run", lease=100):
    workspace["browser_state"].update(mode="agent", controller_id=run, lease_until=int(time.time()) + lease)


@pytest.fixture
def running(workspace, monkeypatch):
    monkeypatch.setattr(
        control.container_repo,
        "get_by_status",
        AsyncMock(return_value=[{"owner_id": "org_browser", "browser_profile_id": "profile"}]),
    )


# --- a person never locks the browser -------------------------------------------------


@pytest.mark.asyncio
async def test_a_person_using_the_stream_never_blocks_the_agent(workspace, agent_tools, monkeypatch):
    # Including a row written by the old model, where a person held a live lease.
    workspace["browser_state"].update(mode="human", controller_id="human", lease_until=int(time.time()) + 200)
    await control.record_input("org_browser")
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 5)
    started = time.monotonic()
    got = await control.acquire_agent("org_browser", "run", "agent-friday")
    assert time.monotonic() - started < 1
    state = workspace["browser_state"]
    assert state["mode"] == "agent" and state["controller_id"] == "run" and state["agent_id"] == "agent-friday"
    # The agent learns when the person last acted, so its hook can yield.
    assert got["humanInputAt"] == workspace["human_input_at"]
    control._stream.assert_awaited_with("session", True, state["generation"])


@pytest.mark.asyncio
async def test_input_is_recorded_beside_the_fenced_state(workspace, agent_tools):
    _agent(workspace)
    before = copy.deepcopy(workspace["browser_state"])
    assert await control.record_input("org_browser") == {"ok": True}
    assert workspace["browser_state"] == before  # an agent's in-flight CAS still lands
    assert workspace["human_input_at"] == pytest.approx(time.time(), abs=5)


@pytest.mark.asyncio
async def test_view_names_the_agent_run_and_reads_old_human_rows_as_idle(workspace, agent_tools):
    _agent(workspace, run="run-1")
    workspace["browser_state"]["agent_id"] = "agent-friday"
    out = await control.view("org_browser", "human")
    assert out["mode"] == "agent" and out["active"] is True
    assert out["agentId"] == "agent-friday"
    assert "isController" not in out and "canControl" not in out
    workspace["browser_state"].update(mode="human", controller_id="human", lease_until=int(time.time()) + 200)
    out = await control.view("org_browser", "human")
    assert out["mode"] == "idle" and out["agentId"] is None and out["active"] is True
    workspace["browser_state"].update(mode="agent", controller_id="run-1", lease_until=int(time.time()) - 1)
    assert (await control.view("org_browser", "human"))["mode"] == "idle"  # a lapsed run lease


# --- agent runs fence each other ------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_starts_once_and_renews_same_generation(workspace, monkeypatch):
    workspace["browser_state"] = None
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(control, "ACQUIRE_POLL_SECONDS", 0.01)
    first = await control.acquire_agent("org_browser", "run")
    second = await control.acquire_agent("org_browser", "run")
    assert first["generation"] == second["generation"]
    client.start_browser_session.assert_called_once()
    control._stream.assert_awaited_once()  # a renewal before expiry never re-streams (no revoke)
    with pytest.raises(control.BrowserError, match="Another agent run"):
        await control.acquire_agent("org_browser", "other-run")


@pytest.mark.asyncio
async def test_session_starts_with_the_helper_policies_and_workspace_extensions(workspace, monkeypatch):
    """Extensions and policies are fixed at session start; the helper always comes first."""
    monkeypatch.setattr(control.settings, "OWNER_STATE_BUCKET", "bucket")
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    for name in ("_restore", "_stream"):
        monkeypatch.setattr(control, name, AsyncMock(return_value=None))
    loc = lambda key: {"location": {"s3": {"bucket": "bucket", "prefix": key}}}  # noqa: E731
    workspace["browser_state"] = None
    workspace["browser_extensions"] = [{"slug": "supademo", "key": "browser-state/org_browser/extensions/supademo.zip"}]
    await control._claim("org_browser", None, start_only=True)
    kwargs = client.start_browser_session.call_args.kwargs
    assert kwargs["enterprisePolicies"] == [
        {"type": "RECOMMENDED", **loc("browser-state/org_browser/chrome-policy.json")}
    ]
    assert kwargs["extensions"] == [
        loc("browser-state/org_browser/browser-helper.zip"),
        loc("browser-state/org_browser/extensions/supademo.zip"),
    ]
    # The helper's token is bound to this session (a retried start with the same clientToken agrees).
    assert workspace["browser_state"]["helper_nonce"] == kwargs["clientToken"][16:]


@pytest.mark.asyncio
async def test_restart_soon_only_restarts_a_running_idle_browser(workspace, monkeypatch):
    """An extension loads at session start: an idle running browser saves, stops and starts
    again in the background; a stopped one (next start loads it) or one an agent run is
    using (its turn is not cut off) is left alone."""
    save, claim = AsyncMock(), AsyncMock()
    monkeypatch.setattr(control, "_save", save)
    monkeypatch.setattr(control, "_claim", claim)
    assert await control.restart_soon("org_browser")
    await asyncio.gather(*control._restarts)
    save.assert_awaited_once()
    assert save.await_args.kwargs == {"stop": True, "reason": "restart"}
    claim.assert_awaited_once_with("org_browser", None, start_only=True)

    _agent(workspace)
    assert not await control.restart_soon("org_browser")
    workspace["browser_state"].update(mode="idle", session_id=None)
    assert not await control.restart_soon("org_browser")
    assert save.await_count == 1


@pytest.mark.asyncio
async def test_agent_waits_for_another_run_then_acquires(workspace, agent_tools, monkeypatch):
    _agent(workspace, run="other")
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 5)

    async def released_soon():
        await asyncio.sleep(0.05)
        workspace["browser_state"] = {**workspace["browser_state"], "mode": "idle"}

    other = asyncio.create_task(released_soon())
    got = await control.acquire_agent("org_browser", "run")
    await other
    assert workspace["browser_state"]["controller_id"] == "run"
    assert got["cdpUrl"] == "wss://signed"


@pytest.mark.asyncio
async def test_agent_gives_up_after_the_wait_window(workspace, agent_tools, monkeypatch):
    _agent(workspace, run="other")
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(control.BrowserError, match="Another agent run"):
        await control.acquire_agent("org_browser", "run")
    assert 0.1 <= time.monotonic() - started < 2
    assert workspace["browser_state"]["controller_id"] == "other"


def test_acquire_wait_fits_under_the_api_gateway_timeout():
    # The wait plus the claim wait must fit the bridge hook's 20 s budget (and so the HTTP
    # API's hard 30 s); a cold start runs on in the background.
    assert control.ACQUIRE_WAIT_SECONDS + control.CLAIM_WAIT_SECONDS < 20


@pytest.mark.asyncio
async def test_stale_agent_release_never_changes_aws(workspace, monkeypatch):
    _agent(workspace)
    stream = AsyncMock()
    monkeypatch.setattr(control, "_stream", stream)
    with pytest.raises(control.BrowserError):
        await control.release_agent("org_browser", "run", "stale")
    with pytest.raises(control.BrowserError):
        await control.release_agent("org_browser", "other-run", "current")
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_release_saves_and_frees_the_browser(workspace, monkeypatch):
    _agent(workspace)
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(
        control,
        "_checkpoint",
        AsyncMock(return_value={"saved_key": "browser-state/org_browser/k.json", "saved_at": "t"}),
    )
    await control.release_agent("org_browser", "run", "current")
    state = workspace["browser_state"]
    assert state["mode"] == "idle" and state["session_id"] == "session"
    assert state["saved_key"].endswith("k.json")
    assert "controller_id" not in state and "lease_until" not in state


@pytest.mark.asyncio
async def test_task_runs_share_one_browser_lease_across_sessions(workspace, agent_tools, monkeypatch):
    """§14.11: two task VMs' runs, one workspace browser. The second gets the busy answer, not a
    second session; once the first VM is stopped (`release_for_run`), the second acquires."""
    monkeypatch.setattr(control, "ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        control,
        "_checkpoint",
        AsyncMock(return_value={"saved_key": "browser-state/org_browser/k.json", "saved_at": "t"}),
    )
    first = await control.acquire_agent("org_browser", "run-task-1", "a1")
    with pytest.raises(control.BrowserError, match="Another agent run"):
        await control.acquire_agent("org_browser", "run-task-2", "a2")
    assert workspace["browser_state"]["session_id"] == "session"  # still the one session

    assert await control.release_for_run("org_browser", "run-task-2") is False  # not the controller
    assert workspace["browser_state"]["controller_id"] == "run-task-1"
    assert await control.release_for_run("org_browser", "run-task-1") is True
    assert await control.release_for_run("org_browser", "run-task-1") is False  # already free
    state = workspace["browser_state"]
    assert control._mode(state) == "idle" and "controller_id" not in state and state["session_id"] == "session"
    assert first["generation"]  # the next run's acquire is the ordinary idle claim from here


@pytest.mark.asyncio
async def test_failed_save_keeps_pointer_and_browser(workspace, monkeypatch):
    _agent(workspace)
    workspace["browser_state"]["saved_key"] = "browser-state/org_browser/previous.json"
    before = copy.deepcopy(workspace["browser_state"])
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("secret URL")))
    with pytest.raises(control.BrowserError, match="save") as exc:
        await control.release_agent("org_browser", "run", "current")
    assert "secret" not in str(exc.value)
    assert workspace["browser_state"] == before


@pytest.mark.asyncio
async def test_stale_save_cannot_publish_over_a_newer_state(workspace, monkeypatch):
    _agent(workspace)
    monkeypatch.setattr(control, "_stream", AsyncMock())

    async def checkpoint(owner, state):
        workspace["browser_state"] = {"generation": "new", "mode": "agent", "controller_id": "other"}
        return {"saved_key": "browser-state/org_browser/old.json", "saved_at": "now"}

    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    with pytest.raises(control.BrowserError):
        await control.release_agent("org_browser", "run", "current")
    assert workspace["browser_state"] == {"generation": "new", "mode": "agent", "controller_id": "other"}


# --- Stop and teardown ----------------------------------------------------------------


def _record_order(monkeypatch, key):
    order = []

    async def stream(*args):
        order.append("enable" if args[1] else "disable")

    async def checkpoint(owner, state):
        order.append("save")
        return {"saved_key": f"browser-state/org_browser/{key}.json", "saved_at": "now"}

    async def retire(*args):
        order.append("stop")

    monkeypatch.setattr(control, "_stream", stream)
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    monkeypatch.setattr(control, "_retire", retire)
    return order


@pytest.mark.asyncio
async def test_teardown_checkpoints_before_stopping_and_retains_pointer(workspace, monkeypatch):
    order = _record_order(monkeypatch, "checkpoint")
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


# --- autosave -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autosave_waits_for_quiet_then_saves_once(workspace, agent_tools, running, monkeypatch):
    checkpoint = AsyncMock(
        return_value={"saved_key": "browser-state/org_browser/auto.json", "saved_at": "2000-01-01T00:00:00+00:00"}
    )
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    now = time.time()
    assert await control.autosave_once(now=now) == 0  # nobody used it
    workspace["human_input_at"] = now - 3
    assert await control.autosave_once(now=now) == 0  # still typing: quiet for only 3 s
    assert await control.autosave_once(now=now + 8) == 1  # quiet for 11 s
    state = workspace["browser_state"]
    assert state["saved_key"].endswith("auto.json") and state["mode"] == "idle" and state["session_id"] == "session"
    # Input older than the last save is already saved.
    workspace["browser_state"]["saved_at"] = "2099-01-01T00:00:00+00:00"
    assert await control.autosave_once(now=now + 60) == 0
    checkpoint.assert_awaited_once()


@pytest.mark.asyncio
async def test_autosave_leaves_a_driving_agent_alone(workspace, agent_tools, running, monkeypatch):
    _agent(workspace)
    workspace["human_input_at"] = time.time() - 60
    checkpoint = AsyncMock()
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    assert await control.autosave_once() == 0  # the run's turn-end release saves
    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_autosave_does_not_retry_a_failed_save_until_new_input(workspace, agent_tools, running, monkeypatch):
    workspace["human_input_at"] = time.time() - 60
    checkpoint = AsyncMock(side_effect=RuntimeError("signed URL"))
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    assert await control.autosave_once() == 0
    assert await control.autosave_once() == 0
    checkpoint.assert_awaited_once()
    assert workspace["browser_state"]["mode"] == "idle"  # still usable by agents
    # boto3 refuses Python floats: the marker must be a Decimal or the write raises.
    assert not isinstance(workspace["browser_state"]["autosave_failed_at"], float)
    workspace["human_input_at"] = time.time() - 20
    await control.autosave_once()
    assert checkpoint.await_count == 2


# --- sessions -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_session_lookup_does_not_start_replacement(workspace, monkeypatch):
    from botocore.exceptions import ClientError

    lookup = AsyncMock(side_effect=ClientError({"Error": {"Code": "ThrottlingException"}}, "GetBrowserSession"))
    monkeypatch.setattr(control.browser_sessions, "owns_session", lookup)
    client = MagicMock()
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    with pytest.raises(ClientError):
        await control._claim("org_browser", None)
    client.start_browser_session.assert_not_called()


@pytest.mark.asyncio
async def test_start_on_a_running_browser_changes_nothing(workspace, monkeypatch):
    _agent(workspace)
    before = copy.deepcopy(workspace["browser_state"])
    stream = AsyncMock()
    monkeypatch.setattr(control, "_stream", stream)
    await control._claim("org_browser", None, start_only=True)
    stream.assert_not_awaited()
    assert workspace["browser_state"] == before


@pytest.mark.asyncio
async def test_agent_recovers_expired_transition_without_view_poll(workspace, monkeypatch):
    workspace["browser_state"].update(mode="transition", operation_at=1)
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "replacement"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_retire", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    await control.acquire_agent("org_browser", "run")
    assert workspace["browser_state"]["mode"] == "agent"
    assert workspace["browser_state"]["session_id"] == "replacement"
    assert workspace["browser_state"].get("error_code") is None


@pytest.mark.asyncio
async def test_retry_reuses_start_token_after_aws_timeout(workspace, monkeypatch):
    workspace["browser_state"] = None
    client = MagicMock()
    client.start_browser_session.side_effect = [RuntimeError("lost response"), {"sessionId": "original"}]
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "_stream", AsyncMock())
    with pytest.raises(control.BrowserError):
        await control._claim("org_browser", None, start_only=True)
    await control._claim("org_browser", None, start_only=True)
    first, retry = client.start_browser_session.call_args_list
    assert first.kwargs["clientToken"] == retry.kwargs["clientToken"]
    assert workspace["browser_state"]["mode"] == "idle"


# --- the panel sizes the remote window --------------------------------------------------


class _FakeCdp:
    """A CDP socket with two page targets in one window."""

    def __init__(self):
        self.sent = []
        self._replies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        import json

        message = json.loads(raw)
        self.sent.append(message)
        result = {
            "Target.getTargets": {
                "targetInfos": [
                    {"targetId": "a", "type": "page"},
                    {"targetId": "b", "type": "page"},
                    {"targetId": "w", "type": "service_worker"},
                ]
            },
            "Browser.getWindowForTarget": {"windowId": 7},
        }.get(message["method"], {})
        self._replies.append({"id": message["id"], "result": result})

    async def recv(self):
        import json

        return json.dumps(self._replies.pop(0))


@pytest.mark.asyncio
async def test_viewport_sizes_each_window_once_while_idle(workspace, agent_tools, monkeypatch):
    cdp = _FakeCdp()
    monkeypatch.setattr(control.websockets, "connect", MagicMock(return_value=cdp))
    assert await control.set_viewport("org_browser", 1136, 648) == {"resized": True}
    bounds = [m["params"] for m in cdp.sent if m["method"] == "Browser.setWindowBounds"]
    assert bounds == [{"windowId": 7, "bounds": {"left": 0, "top": 0, "width": 1137, "height": 649}}]


@pytest.mark.asyncio
async def test_viewport_never_touches_a_browser_an_agent_is_driving(workspace, agent_tools, monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(control.websockets, "connect", connect)
    _agent(workspace)
    assert await control.set_viewport("org_browser", 1136, 648) == {"resized": False}
    workspace["browser_state"].update(mode="transition", operation_at=int(time.time()))
    assert await control.set_viewport("org_browser", 1136, 648) == {"resized": False}
    connect.assert_not_called()
    # Remembered for the next session, even though this one was not resized.
    assert workspace["viewport"] == {"width": 1136, "height": 648}


@pytest.mark.asyncio
async def test_viewport_refuses_an_unsupported_size(workspace, agent_tools):
    with pytest.raises(control.BrowserError):
        await control.set_viewport("org_browser", 4000, 648)


@pytest.mark.asyncio
async def test_a_new_session_starts_at_the_last_panel_size(workspace, monkeypatch):
    workspace["browser_state"] = None
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "_stream", AsyncMock())
    monkeypatch.setattr(control, "_cdp", AsyncMock(return_value="wss://signed"))
    workspace["viewport"] = {"width": 1136, "height": 648}
    await control.acquire_agent("org_browser", "run")
    assert client.start_browser_session.call_args.kwargs["viewPort"] == {"width": 1136, "height": 648}


# --- lifetime: AWS's TTL is a ceiling, our idle stop ends sessions --------------------


@pytest.mark.asyncio
async def test_sessions_start_at_the_aws_maximum(workspace, monkeypatch):
    workspace["browser_state"] = None
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    monkeypatch.setattr(control, "_restore", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "_stream", AsyncMock())
    await control._claim("org_browser", None, start_only=True)
    # AWS: a TTL "regardless of ongoing activity" -- 3600 killed an in-use session in prod.
    assert client.start_browser_session.call_args.kwargs["sessionTimeoutSeconds"] == 28800


@pytest.mark.asyncio
async def test_idle_stop_saves_then_stops_only_after_nobody_used_it(workspace, agent_tools, running, monkeypatch):
    checkpoint = AsyncMock(
        return_value={"saved_key": "browser-state/org_browser/idle.json", "saved_at": "2000-01-01T00:00:00+00:00"}
    )
    retire = AsyncMock()
    monkeypatch.setattr(control, "_checkpoint", checkpoint)
    monkeypatch.setattr(control, "_retire", retire)
    now = time.time()
    workspace["browser_state"]["operation_at"] = now - 10  # an agent turn just released
    workspace["human_input_at"] = now - control.IDLE_STOP_SECONDS - 60
    workspace["browser_state"]["saved_at"] = "2099-01-01T00:00:00+00:00"  # input already saved
    await control.autosave_once(now=now)
    retire.assert_not_awaited()
    workspace["human_input_at"] = now - 60  # a person acted a minute ago
    workspace["browser_state"]["operation_at"] = now - control.IDLE_STOP_SECONDS - 60
    await control.autosave_once(now=now)
    retire.assert_not_awaited()
    _agent(workspace)  # an agent run is mid-turn: never
    await control.autosave_once(now=now + control.IDLE_STOP_SECONDS)
    retire.assert_not_awaited()
    workspace["browser_state"].update(mode="idle", lease_until=None)
    await control.autosave_once(now=now + control.IDLE_STOP_SECONDS)
    checkpoint.assert_awaited_once()  # saved before the stop
    retire.assert_awaited_once_with("session")
    state = workspace["browser_state"]
    assert state["session_id"] is None and state["ended_reason"] == "idle" and state["saved_key"].endswith("idle.json")
    assert (await control.view("org_browser", "user"))["endedReason"] == "idle"


@pytest.mark.asyncio
async def test_idle_stop_still_stops_when_the_save_fails(workspace, agent_tools, running, monkeypatch):
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("gone")))
    retire = AsyncMock()
    monkeypatch.setattr(control, "_retire", retire)
    workspace["browser_state"].update(operation_at=1, saved_key="browser-state/org_browser/old.json")
    await control.autosave_once()
    retire.assert_awaited_once_with("session")
    state = workspace["browser_state"]
    assert state["session_id"] is None and state["ended_reason"] == "idle" and state["mode"] == "idle"
    assert state["saved_key"].endswith("old.json")  # the last completed save stands


@pytest.mark.asyncio
async def test_agent_acquire_on_a_dead_session_starts_fresh_and_restores(workspace, agent_tools, monkeypatch):
    for mode in ("idle", "error"):
        workspace["browser_state"] = {"generation": "g", "mode": mode, "session_id": "dead", "saved_key": "k"}
        monkeypatch.setattr(
            control.browser_sessions,
            "owns_session",
            AsyncMock(side_effect=lambda owner, sid, **kw: {"status": "READY"} if sid == "fresh" else None),
        )
        client = MagicMock()
        client.start_browser_session.return_value = {"sessionId": "fresh"}
        monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
        restore = AsyncMock()
        monkeypatch.setattr(control, "_restore", restore)
        got = await control.acquire_agent("org_browser", "run")
        assert got["cdpUrl"] == "wss://signed"
        assert workspace["browser_state"]["session_id"] == "fresh" and workspace["browser_state"]["mode"] == "agent"
        restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_view_says_an_aws_ended_session_expired(workspace, agent_tools, monkeypatch):
    monkeypatch.setattr(control.browser_sessions, "owns_session", AsyncMock(return_value=None))
    out = await control.view("org_browser", "user")
    assert out["active"] is False and out["endedReason"] == "expired" and out["error"] is None


@pytest.mark.asyncio
async def test_idle_stop_reads_dynamodb_decimals(workspace, agent_tools, running, monkeypatch):
    # DynamoDB returns numbers as Decimal; `float - Decimal` raised and killed every sweep.
    from decimal import Decimal

    retire = AsyncMock()
    monkeypatch.setattr(control, "_retire", retire)
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("gone")))
    workspace["browser_state"].update(operation_at=Decimal(int(time.time())), lease_until=Decimal(1))
    await control.autosave_once()
    retire.assert_not_awaited()
    workspace["browser_state"]["operation_at"] = Decimal(1)
    await control.autosave_once()
    retire.assert_awaited_once_with("session")


# --- cold starts never block a request; a watching person keeps the browser -----------


def _slow_start(workspace, monkeypatch, gate: asyncio.Event):
    workspace["browser_state"] = {"generation": "g", "mode": "idle", "session_id": None, "saved_key": "k"}
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "fresh"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)

    async def restore(*_a):  # the slow part of a cold start
        await gate.wait()

    monkeypatch.setattr(control, "_restore", restore)
    monkeypatch.setattr(control, "CLAIM_WAIT_SECONDS", 0.05)
    return client


@pytest.mark.asyncio
async def test_cold_acquire_answers_starting_then_the_poll_gets_the_browser(workspace, agent_tools, monkeypatch):
    restored = asyncio.Event()
    client = _slow_start(workspace, monkeypatch, restored)
    assert await control.acquire_agent("org_browser", "run") == control.STARTING
    assert await control.acquire_agent("org_browser", "run") == control.STARTING  # mid-start: no second start
    restored.set()
    await asyncio.gather(*control._background)
    got = await control.acquire_agent("org_browser", "run")
    assert got["cdpUrl"] == "wss://signed" and workspace["browser_state"]["controller_id"] == "run"
    client.start_browser_session.assert_called_once()


@pytest.mark.asyncio
async def test_watching_starts_the_browser_once_under_concurrent_heartbeats(workspace, agent_tools, monkeypatch):
    restored = asyncio.Event()
    client = _slow_start(workspace, monkeypatch, restored)
    outs = await asyncio.gather(*(control.watch("org_browser", "user") for _ in range(3)), return_exceptions=True)
    restored.set()
    await asyncio.gather(*control._background)
    client.start_browser_session.assert_called_once()
    assert any(isinstance(out, dict) and out["mode"] == "transition" for out in outs)
    assert workspace["human_view_at"] == pytest.approx(time.time(), abs=5)
    # Running now: a heartbeat only stamps the view.
    await control.watch("org_browser", "user")
    client.start_browser_session.assert_called_once()


@pytest.mark.asyncio
async def test_start_on_top_of_a_start_in_flight_answers_starting(workspace, agent_tools, monkeypatch):
    # The open panel's heartbeat is already starting it when a person clicks Start (or a second
    # panel opens): the same outcome, never "Browser is changing. Try again shortly."
    restored = asyncio.Event()
    client = _slow_start(workspace, monkeypatch, restored)
    assert (await control.watch("org_browser", "user"))["mode"] == "transition"
    out = await control.start("org_browser", "user")
    assert out["mode"] == "transition" and out["operation"] == "start"
    restored.set()
    await asyncio.gather(*control._background)
    client.start_browser_session.assert_called_once()
    state = workspace["browser_state"]
    assert state["mode"] == "idle" and state["session_id"] == "fresh"
    # An agent run is still told to wait, not handed a browser mid-start.
    workspace["browser_state"] = {**state, "mode": "transition", "operation_at": int(time.time())}
    with pytest.raises(control.BrowserError, match="Browser is changing"):
        await control._claim("org_browser", "run")


@pytest.mark.asyncio
async def test_a_watched_browser_never_idle_stops(workspace, agent_tools, running, monkeypatch):
    retire = AsyncMock()
    monkeypatch.setattr(control, "_retire", retire)
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("gone")))
    now = time.time()
    workspace["browser_state"]["operation_at"] = now - control.IDLE_STOP_SECONDS - 60
    workspace["human_view_at"] = now - 20  # the panel's last heartbeat
    await control.autosave_once(now=now)
    retire.assert_not_awaited()
    workspace["human_view_at"] = now - control.IDLE_STOP_SECONDS - 1  # hidden since: the idle clock ran out
    await control.autosave_once(now=now)
    retire.assert_awaited_once_with("session")


# --- a person's "Fill": the backend types a saved login into the active tab -------------


class _FakePage(_FakeCdp):
    """One visible, focused page at `url` with a sign-in form."""

    def __init__(self, url):
        super().__init__()
        self.url = url

    async def send(self, raw):
        import json

        message = json.loads(raw)
        self.sent.append(message)
        result = {
            "Target.getTargets": {"targetInfos": [{"targetId": "a", "type": "page", "url": self.url}]},
            "Target.attachToTarget": {"sessionId": "page-a"},
            "Runtime.evaluate": {
                "result": {"value": {"username": True, "password": True, "visible": True, "focused": True}}
            },
        }.get(message["method"], {})
        self._replies.append({"id": message["id"], "result": result})


LOGIN = {"id": "vault_aaaaaaaaaaaa", "origin": "https://a.example", "username": "me"}


@pytest.fixture
def vault(monkeypatch):
    from core.services import website_logins

    monkeypatch.setattr(website_logins, "list_logins", AsyncMock(return_value=[LOGIN]))
    resolve = AsyncMock(return_value={"username": "me", "password": "hunter2"})
    monkeypatch.setattr(website_logins, "resolve_login", resolve)
    return resolve


@pytest.mark.asyncio
async def test_fill_types_the_login_into_the_active_tab_and_returns_no_secret(
    workspace, agent_tools, vault, monkeypatch
):
    cdp = _FakePage("https://a.example/login?next=/")
    monkeypatch.setattr(control.websockets, "connect", MagicMock(return_value=cdp))
    assert await control.fill_login("org_browser", LOGIN["id"]) == {"ok": True}
    typed = [(m["params"]["text"], m["sessionId"]) for m in cdp.sent if m["method"] == "Input.insertText"]
    assert typed == [("me", "page-a"), ("hunter2", "page-a")]
    vault.assert_awaited_once_with("org_browser", LOGIN["id"], agent_id="browser")


@pytest.mark.asyncio
async def test_fill_refuses_a_tab_on_another_origin_before_resolving(workspace, agent_tools, vault, monkeypatch):
    for url in ("https://evil.example/login", "https://a.example.evil.test/", "http://a.example/"):
        cdp = _FakePage(url)
        monkeypatch.setattr(control.websockets, "connect", MagicMock(return_value=cdp))
        with pytest.raises(control.BrowserError, match="not this page"):
            await control.fill_login("org_browser", LOGIN["id"])
        assert not [m for m in cdp.sent if m["method"] == "Input.insertText"]
    vault.assert_not_called()


@pytest.mark.asyncio
async def test_fill_never_touches_a_browser_an_agent_holds(workspace, agent_tools, vault, monkeypatch):
    import websockets

    connect = MagicMock()
    monkeypatch.setattr(control.websockets, "connect", connect)
    _agent(workspace)
    with pytest.raises(control.BrowserError, match="agent is using"):
        await control.fill_login("org_browser", LOGIN["id"])
    connect.assert_not_called()
    # A run still connected (its lease just lapsed) holds the one automation slot: AWS says 429.
    workspace["browser_state"].update(mode="idle")
    refused = websockets.InvalidStatus(MagicMock(status_code=429))
    monkeypatch.setattr(control.websockets, "connect", MagicMock(side_effect=refused))
    with pytest.raises(control.BrowserError, match="agent is using"):
        await control.fill_login("org_browser", LOGIN["id"])
    vault.assert_not_called()


# --- live frames and input for people without the live view ---------------------------


@pytest.mark.asyncio
async def test_a_frames_heartbeat_asks_the_bridge_only_while_an_agent_browses(workspace, agent_tools, monkeypatch):
    rpc = AsyncMock(return_value={"runs": 1})
    monkeypatch.setattr(control.container_rpc, "call", rpc)
    await control.watch("org_browser", "user", frames=True)
    rpc.assert_not_awaited()  # idle: nothing to cast, and no microVM to wake
    assert workspace["human_frames_at"]
    _agent(workspace)
    await control.watch("org_browser", "user", frames=True)
    rpc.assert_awaited_once_with("org_browser", "browser.frames", {"ttl": control.FRAMES_TTL_SECONDS})
    await control.watch("org_browser", "user")  # the live-view holder's own beat asks for nothing
    rpc.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_frames_ask_never_fails_the_heartbeat(workspace, agent_tools, monkeypatch):
    monkeypatch.setattr(control.container_rpc, "call", AsyncMock(side_effect=RuntimeError("down")))
    _agent(workspace)
    assert (await control.watch("org_browser", "user", frames=True))["mode"] == "agent"


@pytest.mark.asyncio
async def test_acquire_tells_the_bridge_when_someone_last_wanted_frames(workspace, agent_tools):
    workspace["human_frames_at"] = 123.0
    assert (await control.acquire_agent("org_browser", "run"))["framesAt"] == 123.0


@pytest.mark.asyncio
async def test_input_is_forwarded_to_the_bridge_and_counts_as_a_persons(workspace, agent_tools, monkeypatch):
    rpc = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(control.container_rpc, "call", rpc)
    event = {"runId": "run", "type": "click", "x": 10, "y": 20, "button": "left"}
    with pytest.raises(control.BrowserError, match="Take live control"):
        await control.send_input("org_browser", event)  # no agent browsing: nothing is cast
    rpc.assert_not_awaited()
    _agent(workspace)
    before = time.time()
    assert await control.send_input("org_browser", event) == {"ok": True}
    rpc.assert_awaited_once_with("org_browser", "browser.input", event)
    assert workspace["human_input_at"] >= before  # the agent yields to it


@pytest.mark.asyncio
async def test_input_on_a_tab_that_is_no_longer_cast_is_a_plain_conflict(workspace, agent_tools, monkeypatch):
    monkeypatch.setattr(control.container_rpc, "call", AsyncMock(side_effect=RuntimeError('{"code":"NOT_CASTING"}')))
    _agent(workspace)
    with pytest.raises(control.BrowserError, match="Take live control"):
        await control.send_input("org_browser", {"type": "text", "text": "x"})


@pytest.mark.asyncio
async def test_a_live_session_loads_what_another_writer_published_before_the_next_run(
    workspace, agent_tools, monkeypatch
):
    """prod 2026-09-29: a paired Mac's sign-in reached the workspace save, but the running cloud
    browser only restores at start, so the next cloud run never saw it."""
    workspace["browser_state"].update(
        saved_key="browser-state/org_browser/cloud.json", live_key="browser-state/org_browser/cloud.json"
    )
    calls = []

    async def rpc(owner, method, params, timeout_s=None):
        calls.append(params["action"])
        return {"ok": True, "base": True}

    monkeypatch.setattr(control.container_rpc, "call", rpc)
    monkeypatch.setattr(control, "_state_url", lambda key, action: f"https://s3/{key}?{action}")
    monkeypatch.setattr(control, "_save_extension_storage", AsyncMock())
    monkeypatch.setattr(control, "_restore_extension_storage", AsyncMock())
    await control.acquire_agent("org_browser", "run-1")
    assert calls == []  # nothing new: the live session is used as it is
    await control.release_agent("org_browser", "run-1", workspace["browser_state"]["generation"])
    calls.clear()

    # A paired Mac publishes (desktop_browser_state moves saved_key; the live session never loaded it).
    workspace["browser_state"]["saved_key"] = "browser-state/org_browser/mac-1.json"
    await control.acquire_agent("org_browser", "run-2")
    state = workspace["browser_state"]
    assert calls == ["save", "load"]  # its own changes merged in first, then the merged save loaded
    assert state["mode"] == "agent" and state["controller_id"] == "run-2"
    assert state["live_key"] == state["saved_key"] == f"browser-state/org_browser/{state['generation']}.json"

    calls.clear()
    await control.release_agent("org_browser", "run-2", state["generation"])
    assert calls == ["save"]
    await control.acquire_agent("org_browser", "run-3")
    assert calls == ["save"]  # its own save is not news
    await control.release_agent("org_browser", "run-3", workspace["browser_state"]["generation"])

    # The Mac publishes again, and a save (release/autosave) runs before the next acquire: it
    # merges the Mac's state into the workspace without loading it, so the session stays stale.
    workspace["browser_state"]["saved_key"] = "browser-state/org_browser/mac-2.json"
    calls.clear()
    await control._save("org_browser", workspace["browser_state"])
    assert calls == ["save"] and control._stale(workspace["browser_state"])
    await control.acquire_agent("org_browser", "run-4")
    assert calls == ["save", "save", "load"]
    assert not control._stale(workspace["browser_state"])


@pytest.mark.asyncio
async def test_failed_catch_up_load_rejects_lease_and_replaces_partly_restored_session(
    workspace, agent_tools, monkeypatch
):
    workspace["browser_state"].update(
        saved_key="browser-state/org_browser/mac.json", live_key="browser-state/org_browser/cloud.json"
    )
    monkeypatch.setattr(
        control,
        "_checkpoint",
        AsyncMock(return_value={"saved_key": "browser-state/org_browser/merged.json", "base_key": None}),
    )
    monkeypatch.setattr(control, "_restore", AsyncMock(side_effect=control.BrowserError("partial load")))
    with pytest.raises(control.BrowserError, match="partial load"):
        await control._claim("org_browser", "run-1")
    state = workspace["browser_state"]
    assert state["mode"] == "error" and "controller_id" not in state
    assert state["saved_key"] == "browser-state/org_browser/merged.json"

    retire = AsyncMock()
    monkeypatch.setattr(control, "_retire", retire)
    client = MagicMock()
    client.start_browser_session.return_value = {"sessionId": "replacement"}
    monkeypatch.setattr(control.browser_sessions, "_data_client", lambda: client)
    control._restore.side_effect = None
    control._restore.return_value = None
    await control._claim("org_browser", "run-2")
    retire.assert_awaited_once_with("session")
    assert workspace["browser_state"]["session_id"] == "replacement"
    assert workspace["browser_state"]["controller_id"] == "run-2"


@pytest.mark.asyncio
async def test_failed_catch_up_save_leases_stale_session_for_retry_next_run(workspace, agent_tools, monkeypatch):
    workspace["browser_state"].update(
        saved_key="browser-state/org_browser/mac.json", live_key="browser-state/org_browser/cloud.json"
    )
    monkeypatch.setattr(control, "_checkpoint", AsyncMock(side_effect=RuntimeError("save failed")))
    restore = AsyncMock()
    monkeypatch.setattr(control, "_restore", restore)
    await control._claim("org_browser", "run-1")
    assert workspace["browser_state"]["controller_id"] == "run-1"
    assert control._stale(workspace["browser_state"])
    restore.assert_not_awaited()


# --- website sign-ins: which sites the save is signed in to, and signing one out --------

SECRET = "c00kie-value-never-logged"


def _cookie(name, domain, **extra):
    return {"name": name, "value": SECRET, "domain": domain, "path": "/", "expires": -1, "httpOnly": True} | extra


SNAPSHOT = {
    "cookies": [
        _cookie("SID", ".google.com"),
        _cookie("OSID", "mail.google.com"),
        _cookie("sess", "github.com"),
        _cookie("old", "expired.example", expires=1),
        _cookie("_ga", ".analytics.example", httpOnly=False),
    ],
    "origins": [
        {"origin": "https://mail.google.com", "localStorage": [{"name": "t", "value": SECRET}]},
        {"origin": "https://github.com", "localStorage": [{"name": "k", "value": "v"}]},
    ],
}


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]


@pytest.fixture
def saved(workspace, monkeypatch):
    import json

    key = "browser-state/org_browser/k1.json"
    objects = {key: json.dumps(SNAPSHOT).encode(), control._ext_key(key): b'{"ext": {}}'}
    workspace["browser_state"].update(saved_key=key, live_key=key, base_key="browser-state/org_browser/k1.base.json")
    monkeypatch.setattr(control.boto3, "client", lambda *a, **k: _FakeS3(objects))
    return objects


class _FakeBrowser(_FakeCdp):
    """A browser holding SNAPSHOT's cookies with one tab open on mail.google.com."""

    def __init__(self, stuck=False):
        super().__init__()
        self.cookies = [dict(c) for c in SNAPSHOT["cookies"]]
        self.stuck = stuck

    async def send(self, raw):
        import json

        message = json.loads(raw)
        self.sent.append(message)
        params = message.get("params") or {}
        if message["method"] == "Network.deleteCookies" and not self.stuck:
            self.cookies = [c for c in self.cookies if (c["name"], c["domain"]) != (params["name"], params["domain"])]
        result = {
            "Target.getTargets": {
                "targetInfos": [
                    {"targetId": "a", "type": "page", "url": "https://mail.google.com/mail/u/0/"},
                    {"targetId": "b", "type": "page", "url": "https://github.com/"},
                ]
            },
            "Target.attachToTarget": {"sessionId": f"page-{params.get('targetId')}"},
            "Storage.getCookies": {"cookies": self.cookies},
        }.get(message["method"], {})
        self._replies.append({"id": message["id"], "result": result})


@pytest.mark.asyncio
async def test_signed_in_sites_come_from_the_saves_unexpired_httponly_cookies(workspace, saved):
    assert await control.signed_in_sites("org_browser") == {"google.com", "github.com"}
    workspace["browser_state"]["saved_key"] = "browser-state/another_org/k1.json"  # never another owner's save
    assert await control.signed_in_sites("org_browser") == set()


@pytest.mark.asyncio
async def test_sign_out_removes_the_site_from_the_live_browser_and_the_save(
    workspace, agent_tools, saved, monkeypatch, caplog
):
    import json
    import logging

    caplog.set_level(logging.INFO)
    cdp = _FakeBrowser()
    monkeypatch.setattr(control.websockets, "connect", MagicMock(return_value=cdp))
    await control.sign_out("org_browser", "google.com", actor="user_1")

    # Live: only google.com's cookies deleted, its origins cleared, its open tab reloaded.
    deleted = [m["params"]["domain"] for m in cdp.sent if m["method"] == "Network.deleteCookies"]
    assert sorted(deleted) == [".google.com", "mail.google.com"]
    assert {c["domain"] for c in cdp.cookies} == {"github.com", "expired.example", ".analytics.example"}
    cleared = {m["params"]["origin"] for m in cdp.sent if m["method"] == "Storage.clearDataForOrigin"}
    assert cleared == {"https://google.com", "https://mail.google.com"}
    assert [m["sessionId"] for m in cdp.sent if m["method"] == "Page.reload"] == ["page-a"]

    # Saved: a NEW object (the committed one is never rewritten) without the site; the pointer
    # moved to it on a new generation, back in the mode it was in, with the extension data.
    state = workspace["browser_state"]
    assert state["mode"] == "idle" and state["session_id"] == "session" and state["generation"] != "current"
    assert state["saved_key"] == f"browser-state/org_browser/{state['generation']}.json"
    assert state["live_key"] == state["saved_key"] and state["base_key"] == "browser-state/org_browser/k1.base.json"
    after = json.loads(saved[state["saved_key"]])
    assert {c["domain"] for c in after["cookies"]} == {"github.com", "expired.example", ".analytics.example"}
    assert [o["origin"] for o in after["origins"]] == ["https://github.com"]
    assert saved[control._ext_key(state["saved_key"])] == b'{"ext": {}}'
    assert json.loads(saved["browser-state/org_browser/k1.json"]) == SNAPSHOT
    control._delete_snapshot.assert_awaited_once_with("browser-state/org_browser/k1.json")
    assert await control.signed_in_sites("org_browser") == {"github.com"}
    assert SECRET not in caplog.text and "browser.sign_out" in caplog.text


@pytest.mark.asyncio
async def test_sign_out_with_no_running_browser_edits_the_save_only(workspace, saved, monkeypatch):
    import json

    connect = MagicMock()
    monkeypatch.setattr(control.websockets, "connect", connect)
    workspace["browser_state"].update(session_id=None, ended_reason="stopped")
    await control.sign_out("org_browser", "github.com", actor="user_1")
    connect.assert_not_called()
    state = workspace["browser_state"]
    assert state["ended_reason"] == "stopped" and state["session_id"] is None  # a person's Stop still stands
    assert {c["domain"] for c in json.loads(saved[state["saved_key"]])["cookies"]} == {
        ".google.com",
        "mail.google.com",
        "expired.example",
        ".analytics.example",
    }


@pytest.mark.asyncio
async def test_sign_out_refuses_while_an_agent_holds_the_browser_and_changes_nothing(
    workspace, agent_tools, saved, monkeypatch
):
    import websockets

    connect = MagicMock()
    monkeypatch.setattr(control.websockets, "connect", connect)
    _agent(workspace)
    before = copy.deepcopy(workspace["browser_state"])
    with pytest.raises(control.BrowserError, match="agent is using"):
        await control.sign_out("org_browser", "google.com", actor="user_1")
    connect.assert_not_called()
    assert workspace["browser_state"] == before and len(saved) == 2
    # A run still connected after its lease lapsed holds the one automation slot: AWS says 429.
    workspace["browser_state"].update(mode="idle")
    before = copy.deepcopy(workspace["browser_state"])
    refused = websockets.InvalidStatus(MagicMock(status_code=429))
    monkeypatch.setattr(control.websockets, "connect", MagicMock(side_effect=refused))
    with pytest.raises(control.BrowserError, match="agent is using"):
        await control.sign_out("org_browser", "google.com", actor="user_1")
    assert workspace["browser_state"] == before and len(saved) == 2  # the pointer is back; no new save


@pytest.mark.asyncio
async def test_sign_out_commits_nothing_when_the_live_cookies_would_not_go(workspace, agent_tools, saved, monkeypatch):
    monkeypatch.setattr(control.websockets, "connect", MagicMock(return_value=_FakeBrowser(stuck=True)))
    before = copy.deepcopy(workspace["browser_state"])
    with pytest.raises(control.BrowserError, match="Couldn't sign out in the running browser"):
        await control.sign_out("org_browser", "google.com", actor="user_1")
    assert workspace["browser_state"] == before and len(saved) == 2


@pytest.mark.asyncio
async def test_sign_out_never_writes_a_save_it_could_not_read(workspace, saved, monkeypatch):
    workspace["browser_state"].update(session_id=None)
    saved["browser-state/org_browser/k1.json"] = b'{"cookies": "not a list"}'
    before = copy.deepcopy(workspace["browser_state"])
    with pytest.raises(control.BrowserError):
        await control.sign_out("org_browser", "google.com", actor="user_1")
    assert workspace["browser_state"] == before and len(saved) == 2


@pytest.mark.asyncio
async def test_sign_out_is_a_no_op_for_a_workspace_that_never_ran_a_browser(workspace):
    workspace["browser_state"] = None
    await control.sign_out("org_browser", "google.com", actor="user_1")
    assert workspace["browser_state"] is None


@pytest.mark.asyncio
async def test_sign_out_refuses_a_public_suffix_and_leaves_sibling_sites_alone(workspace, saved, monkeypatch):
    import json

    workspace["browser_state"].update(session_id=None)
    key = workspace["browser_state"]["saved_key"]
    cookies = [_cookie("a", ".example.com.ph"), _cookie("b", "shop.example.com.ph"), _cookie("c", "other.com.ph")]
    origins = [{"origin": f"https://{h}", "localStorage": []} for h in ("shop.example.com.ph", "other.com.ph")]
    saved[key] = json.dumps({"cookies": cookies, "origins": origins}).encode()
    before = copy.deepcopy(workspace["browser_state"])
    for suffix in ("com.ph", "ph", "co.uk", "github.io", "shop.example.com.ph"):
        with pytest.raises(control.BrowserError, match="Enter a site"):
            await control.sign_out("org_browser", suffix, actor="user_1")
    assert workspace["browser_state"] == before and len(saved) == 2
    await control.sign_out("org_browser", "example.com.ph", actor="user_1")
    after = json.loads(saved[workspace["browser_state"]["saved_key"]])
    assert [c["domain"] for c in after["cookies"]] == ["other.com.ph"]
    assert [o["origin"] for o in after["origins"]] == ["https://other.com.ph"]


@pytest.mark.asyncio
async def test_the_owner_role_is_granted_the_vpc_browser_once_before_a_session_starts(monkeypatch):
    """Codex on #4391: computer use calls InvokeBrowser under the owner's role, which names only
    the built-in browser until it is rewritten."""
    monkeypatch.setattr(control.bedrock_aip, "BROWSER_ID", "lightbulb_dev_workspace_browser-Ab12Cd34Ef")
    monkeypatch.setattr(control, "_GRANTED", set())
    grant = AsyncMock()
    monkeypatch.setattr(control.bedrock_aip, "ensure_owner_browser_policy", grant)
    await control._grant_browser("org_browser", "profile")
    await control._grant_browser("org_browser", "profile")
    grant.assert_awaited_once_with("org_browser", "profile")
