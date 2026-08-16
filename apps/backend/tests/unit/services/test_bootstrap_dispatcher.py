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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.gateway.connection_pool import GatewayAuthBackoff, GatewayReconnectThrottled
from core.repositories.paperclip_repo import BootstrapClaimResult
from core.services import bootstrap_dispatcher
from core.services.onboarding_brief import FIRST_RUN_NO_BRIEF_MESSAGE, build_first_run_message


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


def _ago(seconds: float) -> str:
    """An ISO stamp *seconds* in the past, in the shape the onboarding bridge
    dict carries (``get_onboarding_state`` normalizes datetimes to ISO)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


_STALE = bootstrap_dispatcher.STALE_DISPATCH_AFTER_S + 60


def _fake_repo(*, claim_result=BootstrapClaimResult.WON, dispatch_id="dispatch-1", onboarding=None):
    repo = MagicMock()
    repo.get_onboarding_state = AsyncMock(return_value=onboarding or _onboarding())
    repo.claim_bootstrap_dispatch = AsyncMock(
        return_value=(claim_result, dispatch_id if claim_result is BootstrapClaimResult.WON else None)
    )
    repo.mark_bootstrap_send_attempted = AsyncMock(return_value=True)
    repo.mark_bootstrap_sent = AsyncMock()
    repo.mark_bootstrap_delivered = AsyncMock()
    repo.mark_bootstrap_failed = AsyncMock()
    repo.rearm_bootstrap_dispatch = AsyncMock()
    repo.delete_pending_brief = AsyncMock(return_value=True)
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
    """A peer already won this dispatch -- this caller must not claim again.

    No ``bootstrap_dispatched_at`` on this fixture: a claim that cannot be
    DATED is never treated as expired (fail closed -- see is_dispatch_stale)."""
    repo, ecs, pool, _ = await _run(_onboarding(bootstrap_state="dispatched"))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_sent_is_a_noop():
    """`sent` means the chat.send was ACKED: delivery happened and only the
    completion bookkeeping can still be outstanding. Re-sending would
    double-run Friday, so this state is left alone no matter how old it is."""
    repo, ecs, pool, _ = await _run(_onboarding(bootstrap_state="sent", bootstrap_dispatched_at=_ago(_STALE)))
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_not_awaited()
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


# ---------------------------------------------------------------------------
# Lazy staleness expiry for a stuck `dispatched` claim.
#
# The claim used to be held in-process across the whole connect ladder + first
# run, so a process death (deploy/OOM/scale-in) left the row at `dispatched`
# FOREVER — nothing read bootstrap_dispatched_at and the admin lever was
# fenced on `failed`. Recovery is evaluated on the poll that already reads
# the row; the write-ahead stamp is what decides re-arm vs. terminal.
# ---------------------------------------------------------------------------


def _stale_dispatched(**overrides) -> dict:
    base = dict(
        bootstrap_state="dispatched",
        bootstrap_dispatch_id="dead-claim-1",
        bootstrap_dispatched_at=_ago(_STALE),
        bootstrap_attempts=1,
    )
    base.update(overrides)
    return _onboarding(**base)


@pytest.mark.asyncio
async def test_expired_claim_with_no_send_attempt_rearms_and_redispatches_now():
    """PROVABLY UNSENT: no write-ahead stamp means nothing ever reached the
    socket, so a fresh dispatch cannot double-run Friday. The SAME call that
    expires the claim re-claims it — recovery is not deferred to a later poll
    (that would need something to guarantee a later poll)."""
    repo, ecs, pool, _ = await _run(_stale_dispatched())

    repo.rearm_bootstrap_dispatch.assert_awaited_once()
    rearm = repo.rearm_bootstrap_dispatch.call_args
    assert rearm.args[0] == "creator_1"  # the ROW's PK, like every DDB write here
    # Fenced on the DEAD claim's dispatch_id, so a live claimant can't be stomped.
    assert rearm.kwargs["dispatch_id"] == "dead-claim-1"
    assert rearm.kwargs["onboarding_generation"] == "01G"
    assert "expired" in rearm.kwargs["reason"]

    repo.claim_bootstrap_dispatch.assert_awaited_once_with("creator_1", onboarding_generation="01G")
    pool.send_rpc.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_claim_after_a_send_attempt_marks_failed_and_never_resends():
    """AMBIGUOUS: the write-ahead stamp is set but the ack never was, so
    OpenClaw may already have taken the run. Re-dispatching would mint a
    run_id it cannot dedup — the same reason the post-connect classifier
    refuses to re-arm. Terminal + explained beats stuck forever."""
    onboarding = _stale_dispatched(bootstrap_send_attempted_at=_ago(_STALE))
    repo, ecs, pool, _ = await _run(onboarding)

    repo.mark_bootstrap_failed.assert_awaited_once()
    failed = repo.mark_bootstrap_failed.call_args
    assert failed.args[0] == "creator_1"
    assert failed.kwargs["dispatch_id"] == "dead-claim-1"
    assert "ack never recorded" in failed.kwargs["reason"]
    assert "admin reset" in failed.kwargs["reason"]

    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_dispatched_claim_is_left_completely_alone():
    """The whole hazard of a staleness rule: expiring a claim that is merely
    SLOW (the connect ladder alone is ~3 minutes) would double-dispatch."""
    repo, ecs, pool, _ = await _run(_stale_dispatched(bootstrap_dispatched_at=_ago(30)))

    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()
    # And it costs nothing: no container/EFS reads on the pre-claim guard path.
    ecs.resolve_running_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_claim_with_no_dispatch_id_is_left_alone():
    """No fencing token (a legacy/degraded row) — leave it for the admin lever
    rather than write unfenced."""
    repo, ecs, pool, _ = await _run(_stale_dispatched(bootstrap_dispatch_id=None))

    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_claim_out_of_attempts_is_recorded_failed_not_rearmed():
    """The cap still bounds total dispatches (re-arming keeps the counter) —
    but at the cap the row must land in a state a LEVER accepts. Re-arming
    would drop it at not_started, where every later poll stands down on the
    cap and the admin reset (failed / stale-dispatched only) can't take it."""
    onboarding = _stale_dispatched(bootstrap_attempts=bootstrap_dispatcher.MAX_BOOTSTRAP_ATTEMPTS)
    repo, ecs, pool, _ = await _run(onboarding)

    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_awaited_once()
    assert "budget is spent" in repo.mark_bootstrap_failed.call_args.kwargs["reason"]
    repo.claim_bootstrap_dispatch.assert_not_awaited()
    pool.send_rpc.assert_not_awaited()


def test_is_dispatch_stale_accepts_both_shapes_and_fails_closed():
    """The admin lever reads a ``datetime`` off the dataclass, the dispatcher
    an ISO string off the bridge dict — one threshold, one parser."""
    stale_dt = datetime.now(timezone.utc) - timedelta(seconds=_STALE)
    assert bootstrap_dispatcher.is_dispatch_stale(stale_dt) is True
    assert bootstrap_dispatcher.is_dispatch_stale(stale_dt.isoformat()) is True
    assert bootstrap_dispatcher.is_dispatch_stale(_ago(30)) is False
    # Naive stamps are read as UTC, not as local time.
    assert bootstrap_dispatcher.is_dispatch_stale(stale_dt.replace(tzinfo=None)) is True
    # Undatable == not stale.
    assert bootstrap_dispatcher.is_dispatch_stale(None) is False
    assert bootstrap_dispatcher.is_dispatch_stale("") is False
    assert bootstrap_dispatcher.is_dispatch_stale("not-a-timestamp") is False


# ---------------------------------------------------------------------------
# The three-stamp ladder: claimed -> send attempted -> sent (acked).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_is_write_ahead_stamped_then_acked_in_order():
    """Stamp 2 goes down BEFORE the RPC (it is the only evidence of which side
    of the socket write a dying process was on), stamp 3 immediately after the
    ack. Order is the whole point — asserting both calls happened would pass
    on the version of this that stamps nothing until the end."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    order: list[str] = []
    repo.mark_bootstrap_send_attempted = AsyncMock(side_effect=lambda *a, **k: order.append("attempted") or True)
    repo.mark_bootstrap_sent = AsyncMock(side_effect=lambda *a, **k: order.append("sent"))
    pool.send_rpc = AsyncMock(side_effect=lambda **k: order.append("send_rpc"))

    await _run(onboarding, repo=repo, pool=pool)

    assert order == ["attempted", "send_rpc", "sent"]
    assert repo.mark_bootstrap_send_attempted.call_args.args[0] == "creator_1"
    assert repo.mark_bootstrap_send_attempted.call_args.kwargs == {
        "onboarding_generation": "01G",
        "dispatch_id": "dispatch-1",
    }
    assert repo.mark_bootstrap_sent.call_args.kwargs == {
        "onboarding_generation": "01G",
        "dispatch_id": "dispatch-1",
    }


@pytest.mark.asyncio
async def test_lost_claim_at_the_write_ahead_stamp_refuses_to_send():
    """A False return means the row moved on under this claim (superseded
    generation, admin reset, a stale-claim recovery) — sending anyway would be
    a second kickoff on a row someone else now owns."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    repo.mark_bootstrap_send_attempted = AsyncMock(return_value=False)
    pool = _fake_pool()
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.send_rpc.assert_not_awaited()
    pool.clear_active_run.assert_called_once()
    pool.unregister_run_completion_callback.assert_called_once()
    repo.mark_bootstrap_sent.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_ack_retires_the_pending_brief():
    """DELIVERY, not completion, is what the pending-brief sweep depends on:
    the kickoff carrying that brief has been accepted. Hanging it off the
    run-completion observer meant a run that never terminated never swept it,
    and the leftover put-if-absent key silently blocks the owner's NEXT brief."""
    repo, ecs, pool, _ = await _run()

    repo.delete_pending_brief.assert_awaited_once_with("user_owner")
    # Completion has not happened yet — the sweep did not wait for it.
    repo.mark_bootstrap_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_observer_no_longer_touches_the_pending_brief():
    repo, ecs, pool, _ = await _run()
    repo.delete_pending_brief.reset_mock()

    callback = pool.register_run_completion_callback.call_args.args[2]
    await callback("final", {"runId": "r1"})

    repo.mark_bootstrap_delivered.assert_awaited_once()
    repo.delete_pending_brief.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_bookkeeping_failure_never_marks_a_delivered_kickoff_failed(monkeypatch):
    """Everything after the ack is bookkeeping. A DDB blip there must not
    reach the failure classifier — it would mark a DELIVERED kickoff failed
    and hand it to a recovery path as if it were retryable."""
    monkeypatch.setattr(bootstrap_dispatcher, "_ACK_STAMP_RETRY_DELAYS_S", (0, 0))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    repo.mark_bootstrap_sent = AsyncMock(side_effect=RuntimeError("ddb throttled"))
    _, _, pool, _ = await _run(onboarding, repo=repo)

    pool.send_rpc.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_stamp_is_retried_through_a_transient_write_failure(monkeypatch):
    """The sent stamp is the ONLY durable record that this kickoff was
    delivered. Dropping it on the first DDB blip leaves a row that reads as an
    unacked send — which the staleness rule settles `failed` and an admin reset
    would re-deliver. So it retries instead of being swallowed."""
    monkeypatch.setattr(bootstrap_dispatcher, "_ACK_STAMP_RETRY_DELAYS_S", (0, 0, 0))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    repo.mark_bootstrap_sent = AsyncMock(side_effect=[RuntimeError("throttled"), RuntimeError("throttled"), None])
    _, _, pool, _ = await _run(onboarding, repo=repo)

    assert repo.mark_bootstrap_sent.await_count == 3  # landed on the third try
    repo.delete_pending_brief.assert_awaited_once_with("user_owner")
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrecordable_delivery_is_loud_and_still_sweeps_the_brief(monkeypatch):
    """The double-fault: acked delivery AND every sent-stamp write fails. There
    is no second durable place to record it (the table that would hold it is
    the one failing), so the guarantee is: never re-armed, never classified
    failed by THIS dispatch, and never silent — an operator must see it before
    reaching for the reset lever that would double-deliver."""
    monkeypatch.setattr(bootstrap_dispatcher, "_ACK_STAMP_RETRY_DELAYS_S", (0, 0))
    metrics: list[str] = []
    monkeypatch.setattr(bootstrap_dispatcher, "put_metric", lambda name, **kw: metrics.append(name))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    repo.mark_bootstrap_sent = AsyncMock(side_effect=RuntimeError("ddb down"))
    _, _, pool, _ = await _run(onboarding, repo=repo)

    assert repo.mark_bootstrap_sent.await_count == 3  # ladder (2) + final attempt
    assert metrics == ["bootstrap.ack_unrecorded"]
    # No failure classification, no re-arm, and the delivered-brief sweep still
    # runs — it is guarded separately from the stamp.
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()
    repo.delete_pending_brief.assert_awaited_once_with("user_owner")


@pytest.mark.asyncio
async def test_pending_brief_sweep_failure_does_not_undo_the_sent_stamp():
    """The two ack-time writes are independently guarded: delivery happened
    either way, so one failing must never skip or reverse the other."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    repo.delete_pending_brief = AsyncMock(side_effect=RuntimeError("ddb throttled"))
    _, _, pool, _ = await _run(onboarding, repo=repo)

    repo.mark_bootstrap_sent.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


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
    # No brief on this fixture -> the no-brief default, still in the user's voice.
    # startswith, not equality: the kickoff now appends the closing steps
    # (name the company, then email the owner) to every path.
    assert send_kwargs["params"]["message"].startswith(FIRST_RUN_NO_BRIEF_MESSAGE)
    assert "isol8-owner-email.sh" in send_kwargs["params"]["message"]
    assert send_kwargs["params"]["deliver"] is False
    assert send_kwargs["ip"] == "10.0.0.5"
    assert send_kwargs["token"] == "tok-123"
    assert send_kwargs["req_id"] == "bootstrap-dispatch-dispatch-1"

    # Completion observer registered BEFORE the send.
    pool.register_run_completion_callback.assert_called_once()
    reg_args = pool.register_run_completion_callback.call_args.args
    assert reg_args[0] == "user_owner"


@pytest.mark.asyncio
async def test_kickoff_message_leads_with_the_users_brief_when_present():
    """Option A: the dispatched chat.send carries the USER's own brief
    (build_first_run_message woven from onboarding_brief_text), not a canned
    prompt -- this is the whole point of the first-message fix."""
    brief = "a social media agency for small restaurants — it's for busy owners"
    onboarding = _onboarding(onboarding_brief_text=brief)
    _, _, pool, _ = await _run(onboarding)

    pool.send_rpc.assert_awaited_once()
    message = pool.send_rpc.call_args.kwargs["params"]["message"]
    assert message == build_first_run_message(brief)
    # The user's verbatim idea leads the message; the ask is woven in.
    assert brief in message
    assert message.startswith("I'm starting ")
    assert "three best next moves" in message
    assert "I'm watching" in message


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


# --- connect retry (boot window) + transient/deterministic classification ---


@pytest.mark.asyncio
async def test_connect_refused_during_boot_retries_then_dispatches(monkeypatch):
    """The ECS task can be RUNNING 1-3 minutes before OpenClaw's gateway
    listens on its port. A connect refusal in that window must be retried
    in place (within the ONE claimed dispatch), not marked failed."""
    monkeypatch.setattr(bootstrap_dispatcher, "_CONNECT_RETRY_DELAYS_S", (0, 0, 0))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(
        side_effect=[
            ConnectionRefusedError(111, "Connect call failed"),
            ConnectionRefusedError(111, "Connect call failed"),
            None,
        ]
    )
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    assert pool.ensure_connection.await_count == 3
    pool.send_rpc.assert_awaited_once()
    # The retries all happen inside ONE claimed dispatch — no re-claim.
    repo.claim_bootstrap_dispatch.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_retry_exhaustion_rearms_for_next_poll(monkeypatch):
    """TRANSIENT after the in-dispatch budget: bootstrap_state goes back to
    not_started (attempts kept on the row) so the next /container/status
    poll re-dispatches — NOT the terminal mark_bootstrap_failed."""
    monkeypatch.setattr(bootstrap_dispatcher, "_CONNECT_RETRY_DELAYS_S", (0,))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(side_effect=ConnectionRefusedError(111, "Connect call failed"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    assert pool.ensure_connection.await_count == 2  # ladder (1) + final attempt
    pool.send_rpc.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_awaited_once()
    rearm = repo.rearm_bootstrap_dispatch.call_args
    assert rearm.args[0] == "creator_1"  # the ROW's PK, like every DDB write here
    assert rearm.kwargs["onboarding_generation"] == "01G"
    assert rearm.kwargs["dispatch_id"] == "dispatch-1"
    assert "Connect call failed" in rearm.kwargs["reason"]
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_failure_at_attempts_cap_marks_failed_for_real(monkeypatch):
    """The re-arm is bounded: the attempt that just died is pre-claim
    attempts + 1, and at MAX_BOOTSTRAP_ATTEMPTS even a transient failure
    becomes the terminal failed — no unbounded retry storm."""
    monkeypatch.setattr(bootstrap_dispatcher, "_CONNECT_RETRY_DELAYS_S", ())
    onboarding = _onboarding(bootstrap_attempts=bootstrap_dispatcher.MAX_BOOTSTRAP_ATTEMPTS - 1)
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(side_effect=ConnectionRefusedError(111, "Connect call failed"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    repo.mark_bootstrap_failed.assert_awaited_once()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_connect_failure_neither_retries_nor_rearms():
    """Non-connection errors (auth backoff, GatewayConnectAuthError) would
    fail identically on a retry — no connect retry, and the terminal
    mark_bootstrap_failed, exactly as before.

    The reconnect FLOOR is deliberately absent from that list: it clears on
    its own in seconds and is retryable. It used to be lumped in here, which
    is precisely how a 5s cooldown latched a row at ``failed`` forever."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(side_effect=RuntimeError("auth backoff active"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    assert pool.ensure_connection.await_count == 1
    repo.mark_bootstrap_failed.assert_awaited_once()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_throttle_retries_then_dispatches(monkeypatch):
    """The prod regression this guards (2026-07-26, org_3H3vi…).

    A ``/container/status`` poll opened the gateway ~0.3s before the
    dispatcher, so the pool's 5s reconnect floor rejected the dispatcher's
    connect. That rejection was a bare RuntimeError, so it was neither
    retried nor re-armed: first contact was lost permanently for that owner,
    on a cooldown that clears by itself. The floor is transient, so the
    ladder must ride it out — its first rung is 5s, exactly the floor.
    """
    monkeypatch.setattr(bootstrap_dispatcher, "_CONNECT_RETRY_DELAYS_S", (0, 0))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(
        side_effect=[
            GatewayReconnectThrottled("throttled — last attempt 0.3s ago, floor 5s"),
            None,
        ]
    )
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    assert pool.ensure_connection.await_count == 2
    pool.send_rpc.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_throttle_exhaustion_rearms_rather_than_failing(monkeypatch):
    """Classified TRANSIENT, so exhausting the in-dispatch budget re-arms for
    the next poll instead of burning the owner's only first contact."""
    monkeypatch.setattr(bootstrap_dispatcher, "_CONNECT_RETRY_DELAYS_S", (0,))
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(side_effect=GatewayReconnectThrottled("throttled, floor 5s"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.send_rpc.assert_not_awaited()
    repo.rearm_bootstrap_dispatch.assert_awaited_once()
    repo.mark_bootstrap_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_backoff_is_not_retried_despite_sharing_a_base_class():
    """``GatewayAuthBackoff`` and ``GatewayReconnectThrottled`` are both
    RuntimeErrors guarding the same call, and mean opposite things. Retrying
    an auth hold cannot succeed AND each attempt counts toward the gateway's
    10-in-60s lockout, so widening the transient set to ``RuntimeError``
    would hammer the owner into that lockout. Only the throttle is
    retryable."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.ensure_connection = AsyncMock(side_effect=GatewayAuthBackoff("auth backoff active for another 42s"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    assert pool.ensure_connection.await_count == 1
    repo.mark_bootstrap_failed.assert_awaited_once()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_connect_send_rpc_reset_marks_failed_not_rearm():
    """P1 (Codex, PR #1433): a connection-level failure AFTER a successful
    connect is AMBIGUOUS -- the chat.send socket write may already have
    landed and OpenClaw may already have ACCEPTED the run. Re-arming would
    mint a fresh run_id/idempotencyKey the next poll and could double-run
    Friday, so a post-connect failure is DELIBERATELY terminal: mark failed,
    never re-arm. Cleanup (clear_active_run + unregister) is unchanged."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.send_rpc = AsyncMock(side_effect=OSError(104, "Connection reset by peer"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.clear_active_run.assert_called_once()
    pool.unregister_run_completion_callback.assert_called_once()
    repo.mark_bootstrap_failed.assert_awaited_once()
    assert "Connection reset by peer" in repo.mark_bootstrap_failed.call_args.kwargs["reason"]
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_connect_send_rpc_ack_timeout_marks_failed_not_rearm():
    """The precise P1 scenario: the connect SUCCEEDED, chat.send was written,
    then the ack wait TIMED OUT (connection_pool.send_rpc raises TimeoutError
    while OpenClaw may already be running the kickoff). A TimeoutError is a
    transient TYPE, but occurring post-connect it must NOT re-arm -- it marks
    failed, because a new run_id on retry cannot be deduped by OpenClaw."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.send_rpc = AsyncMock(side_effect=TimeoutError("ack timed out"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    repo.mark_bootstrap_failed.assert_awaited_once()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_connect_mark_run_active_failure_marks_failed_not_rearm():
    """Any post-connect failure is terminal, not just send_rpc: if
    mark_run_active raised a transient-typed error after the connect
    succeeded, the classifier must still refuse to re-arm (the connect is the
    pivot, not the specific call that raised)."""
    onboarding = _onboarding()
    repo = _fake_repo(onboarding=onboarding)
    pool = _fake_pool()
    pool.mark_run_active = MagicMock(side_effect=OSError(104, "reset"))
    _, _, pool, _ = await _run(onboarding, repo=repo, pool=pool)

    pool.send_rpc.assert_not_awaited()
    repo.mark_bootstrap_failed.assert_awaited_once()
    repo.rearm_bootstrap_dispatch.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# First-contact nudge
#
# The observer NEVER composes or sends mail -- the send stays the agent's own
# tool call. It only notices she hasn't sent and reminds her.
# ---------------------------------------------------------------------------


class _NudgeRepo:
    def __init__(self, state="not_started", allow_bump=True):
        self.state = state
        self.allow_bump = allow_bump
        self.bumps = 0
        self.settled = None
        self.settle_reason = None

    async def get_onboarding_state(self, owner_id):
        return {"first_contact_state": self.state}

    async def bump_first_contact_attempts(self, user_id, *, onboarding_generation, limit):
        self.bumps += 1
        return self.allow_bump

    async def settle_first_contact(self, user_id, *, onboarding_generation, state, reason=None):
        self.settled = state
        self.settle_reason = reason


def _nudge_pool_and_ecs(monkeypatch, sent):
    """Wire the gateway pool + ECS resolver so a nudge can actually dispatch."""
    import core.containers as containers_mod

    class _Pool:
        def register_run_completion_callback(self, owner_id, run_id, cb):
            return True

        def mark_run_active(self, *a, **kw):
            pass

        def clear_active_run(self, *a, **kw):
            pass

        def unregister_run_completion_callback(self, *a, **kw):
            pass

        async def send_rpc(self, **kwargs):
            sent.append(kwargs)

    class _Ecs:
        async def resolve_running_container(self, owner_id):
            return {"gateway_token": "gw"}, "10.0.0.1"

    monkeypatch.setattr(containers_mod, "get_gateway_pool", lambda: _Pool())
    monkeypatch.setattr(containers_mod, "get_ecs_manager", lambda: _Ecs())


async def _run_nudge(repo):
    from core.services.bootstrap_dispatcher import _maybe_nudge_first_contact

    await _maybe_nudge_first_contact(
        repo,
        "u1",
        owner_id="u1",
        generation="01G",
        session_key="sk",
        friday_agent_id="agent_friday",
        submitted_by="u1",
    )


async def test_nudges_when_first_contact_not_started(monkeypatch):
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    repo = _NudgeRepo(state="not_started")
    await _run_nudge(repo)
    assert len(sent) == 1
    assert "first email" in sent[0]["params"]["message"]


async def test_nudges_when_first_contact_failed(monkeypatch):
    """A failed mint or SES send must not make the first failure terminal."""
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    await _run_nudge(_NudgeRepo(state="failed"))
    assert len(sent) == 1


async def test_does_not_nudge_once_sent(monkeypatch):
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    repo = _NudgeRepo(state="sent")
    await _run_nudge(repo)
    assert sent == []
    assert repo.bumps == 0


async def test_does_not_nudge_while_a_claim_is_in_flight(monkeypatch):
    """`sending` means a send is mid-flight -- nudging would risk a double."""
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    await _run_nudge(_NudgeRepo(state="sending"))
    assert sent == []


async def test_exhausted_attempts_settle_failed_and_dispatch_nothing(monkeypatch):
    """The counter is the sole loop bound, and exhausting it must be VISIBLE --
    the previous version of this feature failed silently for its whole life."""
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    repo = _NudgeRepo(state="not_started", allow_bump=False)
    await _run_nudge(repo)
    assert sent == []
    assert repo.settled == "failed"
    assert repo.settle_reason == "nudge_attempts_exhausted"


async def test_attempts_are_bumped_BEFORE_dispatch(monkeypatch):
    """Dispatching first would be an unbounded chain of agent runs."""
    sent = []
    _nudge_pool_and_ecs(monkeypatch, sent)
    repo = _NudgeRepo(state="not_started")
    await _run_nudge(repo)
    assert repo.bumps == 1
    assert len(sent) == 1


async def test_observer_never_raises(monkeypatch):
    """A completion callback that raises would break the gateway's terminal
    handling for everyone on that connection."""
    import core.containers as containers_mod

    def _boom():
        raise RuntimeError("ecs down")

    monkeypatch.setattr(containers_mod, "get_ecs_manager", _boom)
    await _run_nudge(_NudgeRepo(state="not_started"))  # must not raise
