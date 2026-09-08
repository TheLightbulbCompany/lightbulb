"""Chat has exactly ONE entrance.

The generic RPC proxy (``_process_rpc_background``, reached from any
``{"type": "req"}`` WS frame) used to forward *any* method to the per-user
container -- including ``chat.send``. That is a second door into chat: it skips
``gate_chat``, skips ``mark_run_active`` (which owns the parked-vs-dead reconnect
decision), and skips the streaming UX. The frontend already uses the typed
``agent_chat`` message, so the raw-RPC door is pure duplication.

These tests pin the fix in both directions:
  * the run-dispatching methods are refused with a structured error and NEVER
    reach ``pool.send_rpc``;
  * every method the frontend ACTUALLY calls (inventory grepped from
    ``apps/frontend/src`` + ``apps/mobile/src`` ``useGatewayRpc`` /
    ``useGatewayRpcMutation`` call sites) still forwards, and the existing
    interception/guard logic in the proxy is undisturbed.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import websocket_chat as wc  # noqa: E402

# --------------------------------------------------------------------------
# The frontend RPC inventory that used to live here MOVED. The proxy is now
# default-deny, so the live inventory is `websocket_chat._PROXY_ALLOWED_METHODS`
# itself, and tests/unit/routers/test_websocket_chat_method_allowlist.py drives
# the "every listed method still forwards" assertion straight off that frozenset.
# Keeping a second hand-maintained copy here is exactly how this one went two
# months stale (it was missing browser.request, progressCard.get, question.list,
# question.resolve, sessions.branches.list, sessions.branches.switch and
# sessions.rewind). This file is about the run-dispatch refusal; that is all.
# --------------------------------------------------------------------------


def _harness(monkeypatch, *, rpc_result=None, seeded=False, create_consumed=False):
    """Wire _process_rpc_background's collaborators; return (sent, pool, ecs)."""
    sent: list[dict] = []

    mgmt = MagicMock()
    mgmt.send_message = MagicMock(side_effect=lambda _cid, payload: sent.append(payload))
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)
    monkeypatch.setattr(wc, "management_api", None)
    monkeypatch.setattr(wc, "get_management_api_client", AsyncMock(return_value=mgmt))

    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True} if rpc_result is None else rpc_result)
    monkeypatch.setattr(wc, "get_gateway_pool", MagicMock(return_value=pool))

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.5"))
    monkeypatch.setattr(wc, "get_ecs_manager", MagicMock(return_value=ecs))

    # Interceptors / guards that would otherwise reach Paperclip, EFS or DDB.
    monkeypatch.setattr(wc, "_intercept_agents_create", AsyncMock(return_value=create_consumed))
    monkeypatch.setattr(wc, "_intercept_agents_update", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "is_seeded_agent", AsyncMock(return_value=seeded))
    monkeypatch.setattr(wc, "prepare_paperclip_agent_delete", AsyncMock(return_value={}))
    monkeypatch.setattr(wc, "delete_paperclip_for_chat", AsyncMock(return_value=None))
    import core.containers as containers

    monkeypatch.setattr(containers, "get_workspace", MagicMock(return_value=MagicMock(cleanup_agent_dirs=MagicMock())))
    return sent, pool, ecs


async def _run(method: str, params: dict | None = None):
    await wc._process_rpc_background(
        connection_id="conn-abcdefghijkl",
        user_id="user_1",
        owner_id="user_1",
        req_id="req-1",
        method=method,
        params=params if params is not None else {},
    )


# --------------------------------------------------------------------------
# 1. Run-dispatching methods are refused (the duplicate chat entrance).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,expected_use",
    [
        ("chat.send", "agent_chat"),
        ("sessions.send", "agent_chat"),
        ("sessions.steer", "agent_steer"),
        ("agent", "agent_chat"),
    ],
)
@pytest.mark.asyncio
async def test_run_dispatching_method_is_refused(monkeypatch, method, expected_use):
    sent, pool, ecs = _harness(monkeypatch)

    await _run(method, {"agentId": "luna", "message": "hi"})

    # Nothing reached the container.
    pool.send_rpc.assert_not_awaited()
    ecs.resolve_running_container.assert_not_awaited()

    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "res"
    assert frame["id"] == "req-1"
    assert frame["ok"] is False
    err = frame["error"]
    # Structured: machine-readable code + the real path to take.
    assert err["code"] == "INVALID_REQUEST"
    assert err["details"]["method"] == method
    assert err["details"]["use"] == expected_use
    assert expected_use in err["message"]


@pytest.mark.asyncio
async def test_run_dispatch_refusal_set_is_small_and_principled():
    """Guard against the set silently growing into a maintained allowlist."""
    assert wc._RUN_DISPATCH_METHODS == frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})


# --------------------------------------------------------------------------
# 2. The run-adjacent near-misses.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["chat.abort", "agent.wait", "tools.invoke", "cron.list"])
@pytest.mark.asyncio
async def test_run_adjacent_methods_are_now_denied_by_the_allowlist(monkeypatch, method):
    """These used to forward, as proof the run-dispatch refusal was narrow. They are
    not run-dispatching and they are also not on the proxy allowlist — no client sends
    any of them (aborts ride the typed `agent_abort` frame). So the correct assertion
    is now refusal, and the run-dispatch set is still exactly four."""
    sent, pool, _ = _harness(monkeypatch)

    await _run(method, {"runId": "r1"})

    pool.send_rpc.assert_not_awaited()
    assert sent[-1]["ok"] is False
    assert sent[-1]["error"]["details"]["method"] == method
    assert "use" not in sent[-1]["error"]["details"]  # not the run-dispatch refusal


# --------------------------------------------------------------------------
# 3. The existing interception / guard logic is undisturbed.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_create_interception_still_consumes(monkeypatch):
    _sent, pool, _ = _harness(monkeypatch, create_consumed=True)

    await _run("agents.create", {"name": "Bob", "role": "engineer"})

    wc._intercept_agents_create.assert_awaited_once()
    pool.send_rpc.assert_not_awaited()  # pairing already issued the real RPCs


@pytest.mark.asyncio
async def test_agents_update_interceptor_still_runs_before_forward(monkeypatch):
    _sent, pool, _ = _harness(monkeypatch)

    await _run("agents.update", {"agentId": "agent_bob", "name": "Bob"})

    wc._intercept_agents_update.assert_awaited_once()
    pool.send_rpc.assert_awaited_once()


@pytest.mark.asyncio
async def test_agents_delete_still_forwards_and_cascades(monkeypatch):
    _sent, pool, _ = _harness(monkeypatch)

    await _run("agents.delete", {"agentId": "agent_bob"})

    pool.send_rpc.assert_awaited_once()
    wc.delete_paperclip_for_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_agents_delete_retries_transient_openclaw_file_lock(monkeypatch):
    sent, pool, _ = _harness(monkeypatch)
    pool.send_rpc.side_effect = [
        RuntimeError(
            '{"code":"UNAVAILABLE","message":"file lock timeout for '
            '/home/node/.openclaw/openclaw.json: code=file_lock_timeout"}'
        ),
        {"ok": True},
    ]

    import core.services.paperclip_agent_lifecycle as lifecycle

    retry_sleep = AsyncMock()
    monkeypatch.setattr(lifecycle.asyncio, "sleep", retry_sleep)

    await _run("agents.delete", {"agentId": "agent_bob"})

    assert pool.send_rpc.await_count == 2
    delete_calls = pool.send_rpc.await_args_list
    assert all(call.kwargs["method"] == "agents.delete" for call in delete_calls)
    assert delete_calls[0].kwargs["req_id"] != delete_calls[1].kwargs["req_id"]
    retry_sleep.assert_awaited_once_with(0.25)
    assert sent[-1]["ok"] is True
    wc.delete_paperclip_for_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_seeded_agent_delete_guard_still_consumes(monkeypatch):
    sent, pool, _ = _harness(monkeypatch, seeded=True)

    await _run("agents.delete", {"agentId": "friday-uuid"})

    pool.send_rpc.assert_not_awaited()
    assert sent[-1]["ok"] is False
    assert "can't be deleted" in sent[-1]["error"]["message"].lower()


@pytest.mark.asyncio
async def test_sessions_list_is_still_filtered_to_the_caller(monkeypatch):
    sent, pool, _ = _harness(
        monkeypatch,
        rpc_result={
            "sessions": [
                {"key": "agent:luna:user_1"},
                {"key": "agent:luna:user_2"},
                {"key": "agent:luna:paperclip"},
            ]
        },
    )

    await _run("sessions.list", {})

    pool.send_rpc.assert_awaited_once()
    keys = [s["key"] for s in sent[-1]["payload"]["sessions"]]
    assert keys == ["agent:luna:user_1"]


@pytest.mark.asyncio
async def test_session_key_idor_guard_still_consumes(monkeypatch):
    sent, pool, _ = _harness(monkeypatch)

    await _run("sessions.delete", {"key": "agent:luna:user_2"})

    pool.send_rpc.assert_not_awaited()
    assert sent[-1]["ok"] is False
    assert "access" in sent[-1]["error"]["message"].lower()
