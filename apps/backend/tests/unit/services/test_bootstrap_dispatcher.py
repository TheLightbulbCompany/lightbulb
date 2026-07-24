"""Unit tests for core.services.bootstrap_dispatcher.maybe_dispatch_bootstrap.

INV-5: dispatch is claimed via a conditional bootstrap_state:
not_started -> dispatched write fenced on onboarding_generation. Only the
caller that wins the CAS ever calls chat.send. Completion is observed via
the gateway pool's run-completion callback, not assumed.

Every field the dispatcher reads comes from the ``get_onboarding_state``-
shaped bridge dict (see paperclip_repo.py / readiness_state.py), passed in
via the ``onboarding=`` kwarg (the common case here, mirroring how
submit_brief/container_status actually call this) or resolved via
``readiness_state.resolve_onboarding_state`` when omitted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repositories.paperclip_repo import BootstrapClaimResult
from core.services import bootstrap_dispatcher


def _onboarding(**overrides) -> dict:
    base = dict(
        onboarding_generation="01G",
        funnel_state="submitted",
        bootstrap_state="not_started",
        bootstrap_attempts=0,
        user_id="creator_1",
        onboarding_submitted_by="member_1",
    )
    base.update(overrides)
    return base


def _fake_repo(*, claim_result=BootstrapClaimResult.WON, dispatch_id="dispatch-1", onboarding=None):
    repo = MagicMock()
    repo.get_onboarding_state = AsyncMock(return_value=onboarding or _onboarding())
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
    pool.unregister_run_completion_callback = MagicMock()
    pool.mark_run_active = MagicMock()
    pool.clear_active_run = MagicMock()
    pool.send_rpc = AsyncMock()
    return pool


_OPENCLAW_CONFIG = {"agents": {"list": [{"id": "friday-uuid", "default": True}, {"id": "atlas-uuid"}]}}


async def _run(onboarding=None, *, repo=None, ecs=None, pool=None, config=_OPENCLAW_CONFIG, pass_onboarding=True):
    """Drive maybe_dispatch_bootstrap("user_owner", ...).

    ``pass_onboarding=True`` (the default, and the shape both real callers
    use) passes ``onboarding`` explicitly via the kwarg -- the dispatcher
    must never call ``resolve_onboarding_state`` itself in that case. Set
    False to exercise the fallback-fetch path instead. Either way, the
    ``resolve_onboarding_state`` mock is returned so callers can assert on
    whether it was actually invoked.
    """
    onboarding = onboarding if onboarding is not None else _onboarding()
    repo = repo or _fake_repo(onboarding=onboarding)
    ecs = ecs or _fake_ecs_manager()
    pool = pool or _fake_pool()
    resolve_mock = AsyncMock(return_value=onboarding)
    with (
        patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
        patch("core.services.readiness_state.resolve_onboarding_state", new=resolve_mock),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_gateway_pool", return_value=pool),
        patch("core.containers.config.read_openclaw_config_from_efs", new=AsyncMock(return_value=config)),
        patch("routers.websocket_chat.build_chat_session_key", return_value="agent:friday-uuid:member_1"),
    ):
        if pass_onboarding:
            await bootstrap_dispatcher.maybe_dispatch_bootstrap("user_owner", onboarding=onboarding)
        else:
            await bootstrap_dispatcher.maybe_dispatch_bootstrap("user_owner")
    return repo, ecs, pool, resolve_mock


# --- onboarding= not given: falls back to readiness_state.resolve_onboarding_state ---


@pytest.mark.asyncio
async def test_omitted_onboarding_falls_back_to_resolve_onboarding_state():
    repo, ecs, pool, resolve_mock = await _run(pass_onboarding=False)
    resolve_mock.assert_awaited_once_with("user_owner")
    pool.send_rpc.assert_awaited_once()  # the happy-path fallback still dispatches


@pytest.mark.asyncio
async def test_passed_onboarding_never_triggers_a_fallback_read():
    """The whole point of the onboarding= kwarg: no readiness_state read
    happens when the caller already supplied it."""
    repo, ecs, pool, resolve_mock = await _run(pass_onboarding=True)
    resolve_mock.assert_not_awaited()
    pool.send_rpc.assert_awaited_once()


# --- guards ---


@pytest.mark.asyncio
async def test_no_generation_is_a_noop():
    repo, ecs, pool, _ = await _run(_onboarding(onboarding_generation=None))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_funnel_not_submitted_is_a_noop():
    repo, ecs, pool, _ = await _run(_onboarding(funnel_state="not_started"))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_funnel_already_complete_is_a_noop():
    repo, ecs, pool, _ = await _run(_onboarding(funnel_state="complete"))
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_already_dispatched_is_a_noop():
    """A peer already won this dispatch -- this caller must not claim again."""
    repo, ecs, pool, _ = await _run(_onboarding(bootstrap_state="dispatched"))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_already_delivered_is_a_noop():
    repo, ecs, pool, _ = await _run(_onboarding(bootstrap_state="delivered"))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_attempts_reached_is_a_noop():
    repo, ecs, pool, _ = await _run(_onboarding(bootstrap_attempts=bootstrap_dispatcher.MAX_BOOTSTRAP_ATTEMPTS))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_row_user_id_is_a_noop():
    """Defensive: a shape with no row PK (shouldn't happen once a generation
    exists) must degrade, not crash on a bare owner_id fallback."""
    repo, ecs, pool, _ = await _run(_onboarding(user_id=None))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_not_up_does_not_claim_or_write_anything():
    """No running container -- WAIT for the next lifecycle transition, do
    NOT store a stale handoff token (no claim, no dispatch)."""
    ecs = _fake_ecs_manager(container=None, ip=None)
    repo, _, pool, _ = await _run(ecs=ecs)
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_default_agent_resolved_does_not_claim():
    repo, ecs, pool, _ = await _run(config={"agents": {"list": [{"id": "atlas-uuid"}]}})
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_not_ready_does_not_dispatch():
    onboarding = _onboarding()
    repo = _fake_repo(claim_result=BootstrapClaimResult.NOT_READY, onboarding=onboarding)
    _, _, pool, _ = await _run(onboarding, repo=repo)
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_stale_generation_does_not_dispatch():
    onboarding = _onboarding()
    repo = _fake_repo(claim_result=BootstrapClaimResult.STALE_GENERATION, onboarding=onboarding)
    _, _, pool, _ = await _run(onboarding, repo=repo)
    pool.send_rpc.assert_not_awaited()


# --- claim keys off the ROW's user_id; session/member off onboarding_submitted_by ---


@pytest.mark.asyncio
async def test_won_claim_dispatches_chat_send_addressed_at_the_submitting_member():
    """CRITICAL: the DDB claim addresses the ROW's PK (user_id -- the org
    creator for an org), but the gateway session/member is the ACTUAL
    SUBMITTER (onboarding_submitted_by) -- these are DIFFERENT ids in this
    fixture on purpose, pinning the org non-creator-submitter fix."""
    onboarding = _onboarding(user_id="creator_1", onboarding_submitted_by="member_not_creator")
    repo, ecs, pool, _ = await _run(onboarding)

    # The DDB claim is addressed at the ROW's PK.
    repo.claim_bootstrap_dispatch.assert_awaited_once_with("creator_1", onboarding_generation="01G")

    pool.ensure_connection.assert_awaited_once_with("user_owner", "10.0.0.5", "tok-123")
    pool.mark_run_active.assert_called_once()
    mark_args = pool.mark_run_active.call_args
    assert mark_args.args[0] == "user_owner"  # owner_id (the pool key)
    # member_id is the SUBMITTER, not the row's PK.
    assert mark_args.kwargs["member_id"] == "member_not_creator"

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
async def test_session_key_built_with_submitting_member_not_row_pk():
    onboarding = _onboarding(user_id="creator_1", onboarding_submitted_by="member_not_creator")
    with patch("routers.websocket_chat.build_chat_session_key") as build_key:
        build_key.return_value = "agent:friday-uuid:member_not_creator"
        repo = _fake_repo(onboarding=onboarding)
        ecs = _fake_ecs_manager()
        pool = _fake_pool()
        with (
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
            patch("core.containers.get_ecs_manager", return_value=ecs),
            patch("core.containers.get_gateway_pool", return_value=pool),
            patch("core.containers.config.read_openclaw_config_from_efs", new=AsyncMock(return_value=_OPENCLAW_CONFIG)),
        ):
            await bootstrap_dispatcher.maybe_dispatch_bootstrap("user_owner", onboarding=onboarding)
    build_key.assert_called_once_with("friday-uuid", "member_not_creator", None)


@pytest.mark.asyncio
async def test_personal_owner_submitted_by_equals_row_user_id_is_a_noop_distinction():
    """For a personal owner submitted_by == user_id always -- session/member
    and the DDB claim address the SAME id, matching pre-fix behavior."""
    onboarding = _onboarding(user_id="user_owner", onboarding_submitted_by="user_owner")
    repo, ecs, pool, _ = await _run(onboarding)
    mark_args = pool.mark_run_active.call_args
    assert mark_args.kwargs["member_id"] == "user_owner"
    repo.claim_bootstrap_dispatch.assert_awaited_once_with("user_owner", onboarding_generation="01G")


@pytest.mark.asyncio
async def test_missing_submitted_by_falls_back_to_row_user_id():
    """Legacy/degraded row that predates onboarding_submitted_by -- degrade
    to the row's own PK rather than crash or address nothing."""
    onboarding = _onboarding(user_id="creator_1", onboarding_submitted_by=None)
    repo, ecs, pool, _ = await _run(onboarding)
    mark_args = pool.mark_run_active.call_args
    assert mark_args.kwargs["member_id"] == "creator_1"


# --- completion observer ---


@pytest.mark.asyncio
async def test_completion_observer_marks_delivered_on_final():
    onboarding = _onboarding()
    repo, ecs, pool, _ = await _run(onboarding)

    # Grab the registered callback and simulate chat.final.
    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("final", {"runId": "r1"})

    repo.mark_bootstrap_delivered.assert_awaited_once_with(
        "creator_1", onboarding_generation="01G", dispatch_id="dispatch-1"
    )
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_observer_marks_failed_on_error():
    onboarding = _onboarding()
    repo, ecs, pool, _ = await _run(onboarding)

    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("error", {"errorMessage": "boom"})

    repo.mark_bootstrap_failed.assert_awaited_once_with(
        "creator_1", onboarding_generation="01G", dispatch_id="dispatch-1", reason="boom"
    )
    repo.mark_bootstrap_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_observer_marks_failed_on_aborted():
    onboarding = _onboarding()
    repo, ecs, pool, _ = await _run(onboarding)

    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("aborted", {})

    repo.mark_bootstrap_failed.assert_awaited_once()
    assert repo.mark_bootstrap_failed.call_args.kwargs["reason"] == "chat.aborted"


# --- failure paths: no orphaned callback registration ---


@pytest.mark.asyncio
async def test_send_rpc_failure_clears_active_run_and_marks_failed():
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("container unreachable"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.clear_active_run.assert_called_once()
    repo.mark_bootstrap_failed.assert_awaited_once()
    assert "container unreachable" in repo.mark_bootstrap_failed.call_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_send_rpc_failure_unregisters_the_completion_callback():
    """The completion observer was registered BEFORE send_rpc raised -- no
    terminal event will EVER arrive for this run_id (chat.send never
    reached OpenClaw), so it must be explicitly withdrawn or it leaks in
    the connection's _run_completion_callbacks until teardown."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("container unreachable"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.unregister_run_completion_callback.assert_called_once()
    unreg_args = pool.unregister_run_completion_callback.call_args.args
    assert unreg_args[0] == "user_owner"
    # Same run_id that was registered.
    reg_args = pool.register_run_completion_callback.call_args.args
    assert unreg_args[1] == reg_args[1]


@pytest.mark.asyncio
async def test_registration_failure_marks_failed_without_sending():
    """The connection vanished between ensure_connection and registration --
    degrade rather than dispatch with no completion path."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool(register_returns=False)
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.send_rpc.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_registration_failure_does_not_unregister_since_nothing_was_registered():
    """register_run_completion_callback returning False means NOTHING was
    registered on the connection -- unregister must not be called (it would
    be a harmless no-op on the real pool, but asserting this pins that the
    dispatcher doesn't reason about it as if a registration happened)."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool(register_returns=False)
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.unregister_run_completion_callback.assert_not_called()
