"""Unit tests for the run-completion push dispatch.

ROOT-CAUSE NOTE: the push AUDIENCE must be the CANONICAL Clerk id (the users
table PK), not the id parsed from the echoed session key — OpenClaw lowercases
the whole session key on the wire (session-key-utils.ts), so the parsed
``member_id`` is lowercased and a case-sensitive ``user_repo.get`` PK lookup
would miss. The canonical dispatch member id is therefore carried on the
``_active_runs`` entry (set by ``mark_run_active(..., member_id=...)``) and read
back at the terminal. These tests pin that behaviour with a MIXED-CASE member id
so a regression that reintroduces the lowercased echoed id fails loudly.

The ``_active_runs`` dict key is normalized to lowercase at every access site so
the canonical (mixed-case) dispatch key stored by ``mark_run_active`` matches the
lowercased echoed key read at the terminal (a latent bug fixed here: the entry
used to leak and ``chat.e2e.latency`` never emitted for mixed-case users).

Other divergences from the original brief (see task-4-report.md): the real
``send_rpc(req_id, method, params)`` only sends (result via
``wait_for_response``); the agents.list entry key is ``name``/``identity.name``;
``has_active_runs`` is a pre-existing ``@property``.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnection


def _make_conn(owner_id="user_owner"):
    # Construct without running __init__'s socket setup: build a bare instance
    # and set only the attributes the dispatch reads.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = owner_id
    conn.user_id = owner_id
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    # send_rpc only sends (returns None); wait_for_response carries the result.
    conn.send_rpc = AsyncMock(return_value=None)
    conn.wait_for_response = AsyncMock(return_value={"agents": [{"id": "a1", "name": "Friday"}]})
    return conn


def _make_terminal_conn(owner_id="org_1"):
    """A fuller GatewayConnection able to drive the real chat-terminal path in
    ``_handle_message`` (models the fixture in test_e2e_latency_emit.py plus the
    push-dispatch attrs)."""
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = owner_id
    conn.owner_id = owner_id
    conn._frontend_connections = set()
    conn._pending_rpcs = {}
    conn._management_api = MagicMock()
    conn._billing_tasks = set()
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    conn._conn_svc = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    conn._recipients_cache = None
    conn._recipients_cache_ts = 0.0
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._parked = False
    conn._parked_watchdog_task = None
    # Run-completion observer registry (INV-5 bootstrap dispatcher). Not
    # exercised by these push-dispatch tests, but _handle_message's
    # TRUE-terminal branch always pops from it now.
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()
    return conn


@pytest.mark.asyncio
async def test_dispatch_pushes_to_member_with_preview():
    conn = _make_conn()
    payload = {
        "sessionKey": "agent:a1:user_member:s1",
        "message": {"content": [{"type": "text", "text": "here is the summary"}]},
    }
    sent = AsyncMock()
    # Canonical (mixed-case) member id — must be pushed to VERBATIM, not lowercased.
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, carried_member="user_MemberXYZ", agent_id="a1")
        # drain the fire-and-forget task
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_awaited_once()
    args = sent.await_args.args
    assert args[0] == "user_MemberXYZ"  # audience = the canonical carried member
    assert args[1] == "Friday"  # title = agent name
    assert args[2] == "here is the summary"  # body = preview
    assert args[3]["agentId"] == "a1"
    assert args[3]["sessionKey"] == "agent:a1:user_member:s1"


@pytest.mark.asyncio
async def test_personal_session_targets_owner():
    conn = _make_conn(owner_id="user_owner")
    payload = {
        "sessionKey": "agent:a1:main",
        "message": {"content": [{"type": "text", "text": "done"}]},
    }
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        # No carried member (personal session) → falls back to the canonical owner.
        conn._dispatch_push_notification(payload, carried_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    assert sent.await_args.args[0] == "user_owner"


@pytest.mark.asyncio
async def test_org_owner_without_member_does_not_push():
    conn = _make_conn(owner_id="org_123")
    payload = {"sessionKey": "agent:a1:cron:c1", "message": {"content": []}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, carried_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_no_preview_does_not_push():
    conn = _make_conn(owner_id="user_owner")
    payload = {"sessionKey": "agent:a1:main", "message": {"content": []}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, carried_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_body_truncated_to_120_chars():
    conn = _make_conn(owner_id="user_owner")
    long_text = "x" * 200
    payload = {
        "sessionKey": "agent:a1:main",
        "message": {"content": [{"type": "text", "text": long_text}]},
    }
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, carried_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    body = sent.await_args.args[2]
    assert len(body) == 120
    assert body.endswith("…")


@pytest.mark.asyncio
async def test_generic_title_when_agent_name_unresolved():
    conn = _make_conn(owner_id="user_owner")
    conn.wait_for_response = AsyncMock(return_value={"agents": []})
    payload = {
        "sessionKey": "agent:a1:main",
        "message": {"content": [{"type": "text", "text": "hi"}]},
    }
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, carried_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    assert sent.await_args.args[1] == "New reply"


def test_active_runs_key_normalized():
    """mark_run_active stores under a LOWERCASED key so the lowercased echoed
    key from OpenClaw hits the same entry (store canonical, read echoed).

    This is the latent-bug fix: previously the canonical (mixed-case) store key
    and the lowercased echoed read key never matched for mixed-case users.
    """
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn._run_state = {}
    canonical = "agent:a1:user_MixedCase"
    echoed = "agent:a1:user_mixedcase"

    conn.mark_run_active(canonical, agent_id="a1", member_id="user_MixedCase")

    # Stored under the normalized (lowercased) key only.
    assert canonical not in conn._active_runs
    assert echoed in conn._active_runs
    assert conn._active_runs[echoed]["member_id"] == "user_MixedCase"
    assert conn._active_runs[echoed]["agent_id"] == "a1"

    # Every read path resolves via the lowercased echoed key.
    conn._touch_run(echoed)  # no miss
    assert conn._is_true_terminal(echoed, "final") is True
    conn._clear_active_run(echoed)
    assert echoed not in conn._active_runs


def test_mark_run_active_member_id_defaults_none():
    """member_id is optional (default None) so existing call sites are unaffected."""
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn.mark_run_active("agent:a1:main", agent_id="a1")
    assert conn._active_runs["agent:a1:main"]["member_id"] is None


@pytest.mark.asyncio
async def test_carry_canonical_member_through_terminal():
    """End-to-end: mark_run_active with a CANONICAL dispatch key + canonical
    member, drive the terminal with the LOWERCASED echoed key, and assert the
    entry is found and the CANONICAL member is the push audience."""
    conn = _make_terminal_conn(owner_id="org_1")
    conn.mark_run_active("agent:a1:user_MixedCase", agent_id="a1", member_id="user_MixedCase")

    # OpenClaw echoes the session key lowercased.
    payload = {
        "sessionKey": "agent:a1:user_mixedcase",
        "state": "final",
        "runId": "run-1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "carry me"}]},
    }
    sent = AsyncMock()
    with (
        patch("core.services.push_service.send_push_to_user", sent),
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
        patch.object(conn, "_resolve_agent_name", AsyncMock(return_value="Friday")),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks))

    sent.assert_awaited_once()
    assert sent.await_args.args[0] == "user_MixedCase"  # canonical, NOT lowercased
    assert sent.await_args.args[1] == "Friday"
    # The run window was cleared via the lowercased echoed key (no leak).
    assert "agent:a1:user_mixedcase" not in conn._active_runs


@pytest.mark.asyncio
async def test_push_dispatched_before_active_run_cleared():
    """Race guard: _dispatch_push_notification must run BEFORE _clear_active_run.

    Clearing empties _active_runs (flipping has_active_runs to False), which is
    what lets a concurrent _grace_close tear the container connection down. If
    that happens before the push task is created, the closed-app push is lost.
    So the terminal path must spawn the push first, then clear.
    """
    conn = _make_terminal_conn(owner_id="org_1")
    conn.mark_run_active("agent:a1:user_Mixed", agent_id="a1", member_id="user_Mixed")
    payload = {
        "sessionKey": "agent:a1:user_mixed",
        "state": "final",
        "runId": "run-1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }
    order: list[str] = []
    real_dispatch = conn._dispatch_push_notification
    real_clear = conn._clear_active_run

    def rec_dispatch(*a, **k):
        order.append("dispatch")
        return real_dispatch(*a, **k)

    def rec_clear(key):
        order.append("clear")
        return real_clear(key)

    with (
        patch("core.services.push_service.send_push_to_user", AsyncMock()),
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
        patch.object(conn, "_resolve_agent_name", AsyncMock(return_value="Friday")),
        patch.object(conn, "_dispatch_push_notification", side_effect=rec_dispatch),
        patch.object(conn, "_clear_active_run", side_effect=rec_clear),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks))

    assert order == ["dispatch", "clear"], order


@pytest.mark.asyncio
async def test_terminal_error_state_does_not_push():
    """error/aborted terminals never push (only state == 'final')."""
    conn = _make_terminal_conn(owner_id="org_1")
    conn.mark_run_active("agent:a1:user_MixedCase", agent_id="a1", member_id="user_MixedCase")
    payload = {
        "sessionKey": "agent:a1:user_mixedcase",
        "state": "error",
        "runId": "run-1",
        "errorMessage": "boom",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "carry me"}]},
    }
    sent = AsyncMock()
    with (
        patch("core.services.push_service.send_push_to_user", sent),
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
        patch.object(conn, "_resolve_agent_name", AsyncMock(return_value="Friday")),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_side_run_final_does_not_push():
    """A /btw side-run final must NOT push.

    v1 push is for the user-initiated primary reply only. A side run carries no
    member (carried_member is None), so without a guard the audience would fall
    back to the owner and a personal owner would get a push for every aside,
    sometimes mid-stream. Registering the runId makes _is_side_run True so the
    terminal treats it as a side run.
    """
    conn = _make_terminal_conn(owner_id="user_owner")
    run_id = "side-run-1"
    conn.mark_side_run(run_id)  # _is_side_run(run_id) -> True
    payload = {
        "sessionKey": "agent:a1:main",
        "state": "final",
        "runId": run_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "by the way"}]},
    }
    sent = AsyncMock()
    with (
        patch("core.services.push_service.send_push_to_user", sent),
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
        patch.object(conn, "_resolve_agent_name", AsyncMock(return_value="Friday")),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


def test_has_active_runs():
    # has_active_runs is a pre-existing @property (not a method) on the class.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    assert conn.has_active_runs is False
    conn._active_runs = {"agent:a1:main": {}}
    assert conn.has_active_runs is True
