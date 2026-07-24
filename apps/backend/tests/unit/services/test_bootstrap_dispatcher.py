"""Unit tests for core.services.bootstrap_dispatcher.maybe_dispatch_bootstrap.

INV-5: dispatch is claimed via a conditional bootstrap_state:
not_started -> dispatched write fenced on onboarding_generation. Only the
caller that wins the CAS ever calls chat.send. Completion is observed via
the gateway pool's run-completion callback, not assumed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repositories.paperclip_repo import BootstrapClaimResult
from core.services import bootstrap_dispatcher


def _owner_row(**overrides) -> SimpleNamespace:
    base = dict(
        user_id="user_owner",
        onboarding_generation="01G",
        funnel_state="submitted",
        bootstrap_state="not_started",
        bootstrap_attempts=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_repo(owner_row, *, claim_result=BootstrapClaimResult.WON, dispatch_id="dispatch-1"):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=owner_row)
    repo.claim_bootstrap_dispatch = AsyncMock(
        return_value=(claim_result, dispatch_id if claim_result is BootstrapClaimResult.WON else None)
    )
    repo.mark_bootstrap_delivered = AsyncMock()
    repo.mark_bootstrap_failed = AsyncMock()
    return repo


_NOT_GIVEN = object()


def _fake_ecs_manager(*, container=_NOT_GIVEN, ip=_NOT_GIVEN):
    ecs = MagicMock()
    resolved_container = {"gateway_token": "tok-123"} if container is _NOT_GIVEN else container
    resolved_ip = "10.0.0.5" if ip is _NOT_GIVEN else ip
    ecs.resolve_running_container = AsyncMock(return_value=(resolved_container, resolved_ip))
    return ecs


def _fake_pool(*, register_returns=True):
    pool = MagicMock()
    pool.ensure_connection = AsyncMock()
    pool.register_run_completion_callback = MagicMock(return_value=register_returns)
    pool.mark_run_active = MagicMock()
    pool.clear_active_run = MagicMock()
    pool.send_rpc = AsyncMock()
    return pool


_OPENCLAW_CONFIG = {"agents": {"list": [{"id": "friday-uuid", "default": True}, {"id": "atlas-uuid"}]}}


async def _run(owner_row=None, *, repo=None, ecs=None, pool=None, config=_OPENCLAW_CONFIG):
    owner_row = owner_row if owner_row is not None else _owner_row()
    repo = repo or _fake_repo(owner_row)
    ecs = ecs or _fake_ecs_manager()
    pool = pool or _fake_pool()
    with (
        patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_gateway_pool", return_value=pool),
        patch("core.containers.config.read_openclaw_config_from_efs", new=AsyncMock(return_value=config)),
        patch("routers.websocket_chat.build_chat_session_key", return_value="agent:friday-uuid:user_owner"),
    ):
        await bootstrap_dispatcher.maybe_dispatch_bootstrap("user_owner")
    return repo, ecs, pool


@pytest.mark.asyncio
async def test_no_owner_row_is_a_noop():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=None)
    with patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo):
        await bootstrap_dispatcher.maybe_dispatch_bootstrap("user_owner")
    repo.resolve_owner_row.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_onboarding_generation_is_a_noop():
    repo, ecs, pool = await _run(_owner_row(onboarding_generation=None))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_funnel_not_submitted_is_a_noop():
    repo, ecs, pool = await _run(_owner_row(funnel_state="not_started"))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_funnel_already_complete_is_a_noop():
    repo, ecs, pool = await _run(_owner_row(funnel_state="complete"))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_already_dispatched_is_a_noop():
    """A peer already won this dispatch -- this caller must not claim again."""
    repo, ecs, pool = await _run(_owner_row(bootstrap_state="dispatched"))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_already_delivered_is_a_noop():
    repo, ecs, pool = await _run(_owner_row(bootstrap_state="delivered"))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_attempts_reached_is_a_noop():
    repo, ecs, pool = await _run(_owner_row(bootstrap_attempts=bootstrap_dispatcher.MAX_BOOTSTRAP_ATTEMPTS))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_not_up_does_not_claim_or_write_anything():
    """No running container -- WAIT for the next lifecycle transition, do
    NOT store a stale handoff token (no claim, no dispatch)."""
    ecs = _fake_ecs_manager(container=None, ip=None)
    repo, _, pool = await _run(ecs=ecs)
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_default_agent_resolved_does_not_claim():
    repo, ecs, pool = await _run(config={"agents": {"list": [{"id": "atlas-uuid"}]}})
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_not_ready_does_not_dispatch():
    owner_row = _owner_row()
    repo = _fake_repo(owner_row, claim_result=BootstrapClaimResult.NOT_READY)
    _, _, pool = await _run(owner_row, repo=repo)
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_stale_generation_does_not_dispatch():
    owner_row = _owner_row()
    repo = _fake_repo(owner_row, claim_result=BootstrapClaimResult.STALE_GENERATION)
    _, _, pool = await _run(owner_row, repo=repo)
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_won_claim_dispatches_chat_send_with_first_run_prompt():
    owner_row = _owner_row()
    repo, ecs, pool = await _run(owner_row)

    pool.ensure_connection.assert_awaited_once_with("user_owner", "10.0.0.5", "tok-123")
    pool.mark_run_active.assert_called_once()
    mark_args = pool.mark_run_active.call_args
    assert mark_args.args[0] == "user_owner"  # owner_id
    assert mark_args.kwargs["member_id"] == "user_owner"

    pool.send_rpc.assert_awaited_once()
    send_kwargs = pool.send_rpc.call_args.kwargs
    assert send_kwargs["method"] == "chat.send"
    assert send_kwargs["params"]["message"] == bootstrap_dispatcher.FIRST_RUN_PROMPT
    assert send_kwargs["params"]["deliver"] is False
    assert send_kwargs["ip"] == "10.0.0.5"
    assert send_kwargs["token"] == "tok-123"
    assert send_kwargs["req_id"] == "bootstrap-dispatch-dispatch-1"

    # Completion observer registered BEFORE the send.
    pool.register_run_completion_callback.assert_called_once()
    reg_args = pool.register_run_completion_callback.call_args.args
    assert reg_args[0] == "user_owner"


@pytest.mark.asyncio
async def test_completion_observer_marks_delivered_on_final():
    owner_row = _owner_row()
    repo, ecs, pool = await _run(owner_row)

    # Grab the registered callback and simulate chat.final.
    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("final", {"runId": "r1"})

    repo.mark_bootstrap_delivered.assert_awaited_once_with(
        "user_owner", onboarding_generation="01G", dispatch_id="dispatch-1"
    )
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_observer_marks_failed_on_error():
    owner_row = _owner_row()
    repo, ecs, pool = await _run(owner_row)

    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("error", {"errorMessage": "boom"})

    repo.mark_bootstrap_failed.assert_awaited_once_with(
        "user_owner", onboarding_generation="01G", dispatch_id="dispatch-1", reason="boom"
    )
    repo.mark_bootstrap_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_observer_marks_failed_on_aborted():
    owner_row = _owner_row()
    repo, ecs, pool = await _run(owner_row)

    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("aborted", {})

    repo.mark_bootstrap_failed.assert_awaited_once()
    assert repo.mark_bootstrap_failed.call_args.kwargs["reason"] == "chat.aborted"


@pytest.mark.asyncio
async def test_send_rpc_failure_clears_active_run_and_marks_failed():
    owner_row = _owner_row()
    repo = _fake_repo(owner_row)
    pool = _fake_pool()
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("container unreachable"))
    _, _, pool = await _run(owner_row, repo=repo, pool=pool)

    pool.clear_active_run.assert_called_once()
    repo.mark_bootstrap_failed.assert_awaited_once()
    assert "container unreachable" in repo.mark_bootstrap_failed.call_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_registration_failure_marks_failed_without_sending():
    """The connection vanished between ensure_connection and registration --
    degrade rather than dispatch with no completion path."""
    owner_row = _owner_row()
    repo = _fake_repo(owner_row)
    pool = _fake_pool(register_returns=False)
    _, _, pool = await _run(owner_row, repo=repo, pool=pool)

    pool.send_rpc.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_awaited_once()
