"""Tests for backend lifespan startup/shutdown wiring."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_resume_wake_resize_transitions_kicks_off_poller_per_unclaimed_row():
    """After a backend restart any UNCLAIMED container still in
    status=provisioning in DDB (wake-from-stopped, resize — neither claims a
    provision lease) MUST have its transition poller resumed. Without this, a
    deploy that lands mid-wake/resize permanently strands the container at
    status=provisioning because the original asyncio task was killed on
    shutdown."""
    from main import _resume_wake_resize_transitions

    provisioning_rows = [
        {"owner_id": "user_a", "status": "provisioning"},
        {"owner_id": "user_b", "status": "provisioning"},
    ]

    mock_ecs = MagicMock()
    mock_ecs._await_running_transition = AsyncMock()

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=provisioning_rows,
        ) as mock_get,
        patch("core.containers.get_ecs_manager", return_value=mock_ecs),
    ):
        await _resume_wake_resize_transitions()

        # Give fire-and-forget tasks a chance to be scheduled and awaited.
        import asyncio as _asyncio

        await _asyncio.sleep(0)

        mock_get.assert_awaited_once_with("provisioning")
        assert mock_ecs._await_running_transition.await_count == 2
        awaited_users = {call.args[0] for call in mock_ecs._await_running_transition.await_args_list}
        assert awaited_users == {"user_a", "user_b"}


@pytest.mark.asyncio
async def test_resume_wake_resize_transitions_skips_sfn_claimed_rows():
    """A row with a stamped provision_claim_id belongs to the SFN executor
    (dispatch_provision) — SFN survives backend process death natively, and
    resuming a bare, UNFENCED poller over it would race the runner's own
    fenced write with an unconditioned one. Must be skipped."""
    from main import _resume_wake_resize_transitions

    provisioning_rows = [
        {"owner_id": "user_sfn", "status": "provisioning", "provision_claim_id": "claim-1"},
        {"owner_id": "user_wake", "status": "provisioning"},
    ]

    mock_ecs = MagicMock()
    mock_ecs._await_running_transition = AsyncMock()

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=provisioning_rows,
        ),
        patch("core.containers.get_ecs_manager", return_value=mock_ecs),
    ):
        await _resume_wake_resize_transitions()

        import asyncio as _asyncio

        await _asyncio.sleep(0)

        assert mock_ecs._await_running_transition.await_count == 1
        awaited_users = {call.args[0] for call in mock_ecs._await_running_transition.await_args_list}
        assert awaited_users == {"user_wake"}


@pytest.mark.asyncio
async def test_resume_wake_resize_transitions_tolerates_empty():
    """Zero provisioning rows -> no-op, no error."""
    from main import _resume_wake_resize_transitions

    mock_ecs = MagicMock()
    mock_ecs._await_running_transition = AsyncMock()

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("core.containers.get_ecs_manager", return_value=mock_ecs),
    ):
        await _resume_wake_resize_transitions()

        mock_ecs._await_running_transition.assert_not_called()


@pytest.mark.asyncio
async def test_resume_wake_resize_transitions_tolerates_ddb_failure():
    """get_by_status raising MUST NOT crash backend startup -- reconciliation
    is best-effort; a transient DDB error should be logged and shrug off."""
    from main import _resume_wake_resize_transitions

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ddb transient"),
        ),
        patch("core.containers.get_ecs_manager") as mock_get_ecs,
    ):
        # Must not raise.
        await _resume_wake_resize_transitions()

        mock_get_ecs.assert_not_called()


@pytest.mark.asyncio
async def test_running_count_gauge_loop_tags_the_emitting_instance(monkeypatch):
    """P12 cannot tell a dead instance from a halved rate without this dimension."""
    import main

    monkeypatch.setenv("HOSTNAME", "task-abc123")
    seen: list[tuple[str, float, dict | None]] = []
    monkeypatch.setattr(
        "core.observability.metrics.gauge",
        lambda name, value, dimensions=None: seen.append((name, value, dimensions)),
    )
    monkeypatch.setattr(main.container_repo, "get_by_status", AsyncMock(return_value=[{"owner_id": "o1"}]))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert seen == [("gateway.running.count", 1, {"instance": "task-abc123"})]


@pytest.mark.asyncio
async def test_running_count_gauge_loop_falls_back_off_ecs(monkeypatch):
    """No HOSTNAME (local/pytest) must not crash the canary."""
    import main

    monkeypatch.delenv("HOSTNAME", raising=False)
    seen = []
    monkeypatch.setattr(
        "core.observability.metrics.gauge",
        lambda name, value, dimensions=None: seen.append(dimensions),
    )
    monkeypatch.setattr(main.container_repo, "get_by_status", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert seen == [{"instance": "local"}]
