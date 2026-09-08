"""The generic RPC proxy forwards only what the product actually sends.

`_process_rpc_background` used to forward ANY `method` string a browser put in a
`{"type":"req"}` frame straight to the per-owner container. The container trusts
us completely: our operator device holds `operator.admin`
(core/crypto/operator_device.py:85-89), and OpenClaw's `roleVisibilityFilter`
only engages behind `hasOperatorBoundary` (fork src/gateway/operator-role-policy.ts:138-140),
which needs a configured operator role — we configure none.

Orgs share ONE container (`owner_id = org_id or user_id`), so an unguarded method
that carries a session key is a cross-tenant IDOR, and one that carries none at all
(audit reads, device pairing, cron writes) cannot be fixed by any key check.
`core-descriptors.ts` at the pinned tag declares ~390 dotted methods; the product
sends 44. Deny the rest.

This is default-deny, so the table IS the security boundary: see the inventory in
docs/superpowers/plans/2026-09-08-rpc-proxy-session-guard.md for how it was built and
what may be added to it.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import websocket_chat as wc  # noqa: E402


def _harness(monkeypatch):
    """Wire _process_rpc_background's collaborators; return (sent, pool, ecs)."""
    sent: list[dict] = []

    mgmt = MagicMock()
    mgmt.send_message = MagicMock(side_effect=lambda _cid, payload: sent.append(payload))
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)
    monkeypatch.setattr(wc, "management_api", None)
    monkeypatch.setattr(wc, "get_management_api_client", AsyncMock(return_value=mgmt))

    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(wc, "get_gateway_pool", MagicMock(return_value=pool))

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.5"))
    monkeypatch.setattr(wc, "get_ecs_manager", MagicMock(return_value=ecs))
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


# Methods that are real, reachable, dangerous, and that nothing of ours sends.
_DENIED = [
    "sessions.preview",  # reads another member's transcript
    "sessions.companion.ask",  # model-summarises another member's transcript
    "sessions.patchMany",  # routes around the sessions.patch guard
    "sessions.fork",
    "sessions.assignOwner",
    "session.members.add",
    "session.visibility.set",
    "sessions.diff",
    "sessions.files.get",
    "sessions.reset",  # key-guarded, but no client sends it — guarded != needed
    "cron.add",  # carries no session key at all
    "device.pair.approve",
    "audit.list",
]


@pytest.mark.parametrize("method", _DENIED)
@pytest.mark.asyncio
async def test_unlisted_method_is_refused_before_touching_the_container(monkeypatch, method):
    sent, pool, ecs = _harness(monkeypatch)

    await _run(method, {"sessionKey": "agent:a:user_2"})

    pool.send_rpc.assert_not_awaited()
    ecs.resolve_running_container.assert_not_awaited()
    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "res"
    assert frame["id"] == "req-1"
    assert frame["ok"] is False
    assert frame["error"]["code"] == "INVALID_REQUEST"
    assert frame["error"]["details"]["method"] == method


@pytest.mark.asyncio
async def test_refusal_emits_a_named_metric(monkeypatch):
    """The dev-deploy safety valve: a method we wrongly omitted shows up in
    CloudWatch BY NAME before the prod promotion."""
    _harness(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(wc, "put_metric", lambda name, **kw: calls.append((name, kw)))

    await _run("cron.add", {})

    assert ("rpc.refused", {"dimensions": {"method": "cron.add", "reason": "not_allowlisted"}}) in calls


@pytest.mark.asyncio
async def test_run_dispatch_methods_keep_their_own_more_helpful_refusal(monkeypatch):
    """chat.send is denied by BOTH gates. The run-dispatch one runs first because
    it names the typed frame the caller should use instead."""
    sent, pool, _ = _harness(monkeypatch)

    await _run("chat.send", {"agentId": "luna", "message": "hi"})

    pool.send_rpc.assert_not_awaited()
    assert sent[0]["error"]["details"]["use"] == "agent_chat"


# --------------------------------------------------------------------------
# The positive half: every allowlisted method must still reach the container.
#
# Session-key-bearing methods need a key the caller owns, or
# _guard_session_key_ownership consumes the request before it forwards.
# --------------------------------------------------------------------------

_OWNED_KEY = "agent:luna:user_1"

_PARAMS_BY_METHOD: dict[str, dict] = {
    "agents.delete": {"agentId": "agent_bob"},
    "chat.history": {"sessionKey": _OWNED_KEY},
    "chat.message.get": {"sessionKey": _OWNED_KEY},
    "progressCard.get": {"sessionKey": _OWNED_KEY},
    "sessions.branches.list": {"sessionKey": _OWNED_KEY},
    "sessions.branches.switch": {"sessionKey": _OWNED_KEY},
    "sessions.delete": {"key": _OWNED_KEY},
    "sessions.patch": {"key": _OWNED_KEY},
    "sessions.rewind": {"sessionKey": _OWNED_KEY},
    "sessions.search": {"sessionKeys": [_OWNED_KEY]},
}


def _forwarding_harness(monkeypatch):
    sent, pool, ecs = _harness(monkeypatch)
    # Interceptors / guards that would otherwise reach Paperclip, EFS or DDB.
    monkeypatch.setattr(wc, "_intercept_agents_create", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "_intercept_agents_update", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "is_seeded_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "prepare_paperclip_agent_delete", AsyncMock(return_value={}))
    monkeypatch.setattr(wc, "delete_paperclip_for_chat", AsyncMock(return_value=None))
    import core.containers as containers

    monkeypatch.setattr(
        containers,
        "get_workspace",
        MagicMock(return_value=MagicMock(cleanup_agent_dirs=MagicMock())),
    )
    return sent, pool, ecs


@pytest.mark.parametrize("method", sorted(wc._PROXY_ALLOWED_METHODS))
@pytest.mark.asyncio
async def test_every_allowlisted_method_still_forwards(monkeypatch, method):
    """Drives the list itself, so it can never claim coverage it does not have."""
    sent, pool, _ = _forwarding_harness(monkeypatch)

    await _run(method, dict(_PARAMS_BY_METHOD.get(method, {})))

    pool.send_rpc.assert_awaited_once()
    assert pool.send_rpc.await_args.kwargs["method"] == method
    assert sent[-1]["type"] == "res"
    assert sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_the_allowlist_and_the_run_dispatch_set_are_disjoint():
    """chat.send must never be admitted by adding it here — the typed agent_chat
    frame owns that job, and _RUN_DISPATCH_METHODS is deliberately frozen at four."""
    assert not (wc._PROXY_ALLOWED_METHODS & wc._RUN_DISPATCH_METHODS)


@pytest.mark.asyncio
async def test_the_template_literal_methods_are_both_present():
    """SkillProposalsPanel.tsx:93 sends `skills.proposals.${action}` with action typed
    "apply" | "reject". Neither is visible to a literal grep, so pin them."""
    assert {"skills.proposals.apply", "skills.proposals.reject"} <= wc._PROXY_ALLOWED_METHODS


@pytest.mark.asyncio
async def test_the_dreams_panel_dispatch_table_is_covered():
    """DreamsPanel.tsx dispatches `callRpc(method, …)` off a module-level
    ADVANCED_ACTIONS table, so adding a row there ships a new method with no
    change at the call site — the highest drift risk on the list. Pin the seven
    doctor.memory.* methods it can emit (five from the table, two hook literals)."""
    assert {
        "doctor.memory.backfillDreamDiary",
        "doctor.memory.dedupeDreamDiary",
        "doctor.memory.dreamDiary",
        "doctor.memory.repairDreamingArtifacts",
        "doctor.memory.resetDreamDiary",
        "doctor.memory.resetGroundedShortTerm",
        "doctor.memory.status",
    } <= wc._PROXY_ALLOWED_METHODS
