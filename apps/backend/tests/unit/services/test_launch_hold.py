"""Pre-drop hold: a signed-up account is kept container-less until the Launch
Competition's drop instant. See core/services/launch_hold.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from core.repositories import launch_settings_repo
from core.services import launch_hold, launch_phase

NOW = datetime.now(timezone.utc)


async def test_holds_new_owner_before_drop(launch_schedule):
    with (
        launch_schedule(NOW + timedelta(days=10)),
        patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
    ):
        g = await launch_hold.pre_drop_gate("user_1")
    assert g is not None and g.code == "launch_pending" and g.action_href == "/launch" and g.owner_role == "admin"


async def test_never_holds_an_owner_with_a_container(launch_schedule):
    with (
        launch_schedule(NOW + timedelta(days=10)),
        patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await launch_hold.pre_drop_gate("user_1") is None


async def test_lifts_at_the_drop(launch_schedule):
    with (
        launch_schedule(NOW - timedelta(seconds=1)),
        patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
    ):
        assert await launch_hold.pre_drop_gate("user_1") is None


async def test_off_when_unconfigured_and_on_repo_error(launch_schedule):
    with (
        launch_schedule(None),
        patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
    ):
        assert await launch_hold.pre_drop_gate("user_1") is None
    with (
        launch_schedule(NOW + timedelta(days=10)),
        patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb"))),
    ):
        assert await launch_hold.pre_drop_gate("user_1") is None


def _settings_read_down():
    """A cold launch_phase cache plus a failing settings read — the shape a
    DynamoDB blip has on the poll path."""
    launch_phase.invalidate()
    return patch.object(launch_settings_repo, "get_sync", side_effect=RuntimeError("ddb down"))


async def test_holds_a_container_less_owner_when_the_settings_read_fails():
    """An unreadable settings row reads as 'feature off', and un-holding is
    irreversible (the next step stamps the 3-day trial clock) — so hold, and
    let the next poll retry. The copy can't name a drop instant it never
    read."""
    try:
        with (
            _settings_read_down(),
            patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        ):
            g = await launch_hold.pre_drop_gate("user_1")
            # The failure is not cached: the next poll reads again.
            assert launch_phase._CACHE["row"] is None
    finally:
        launch_phase.invalidate()
    assert g is not None and g.code == "launch_pending"
    assert "Doors open soon" in g.message


async def test_never_holds_an_owner_with_a_container_when_the_settings_read_fails():
    """The container read runs first, so an existing owner is untouched by a
    settings-read failure."""
    try:
        with (
            _settings_read_down(),
            patch.object(
                launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "user_1"})
            ) as container_read,
        ):
            assert await launch_hold.pre_drop_gate("user_1") is None
        container_read.assert_awaited_once()
    finally:
        launch_phase.invalidate()


async def test_a_recovered_settings_read_stops_holding():
    """One poll later, with DynamoDB back and the feature genuinely off, the
    same owner is released."""
    try:
        with (
            _settings_read_down(),
            patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        ):
            assert await launch_hold.pre_drop_gate("user_1") is not None
        with (
            patch.object(launch_settings_repo, "get_sync", return_value=None),
            patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        ):
            assert await launch_hold.pre_drop_gate("user_1") is None
    finally:
        launch_phase.invalidate()
