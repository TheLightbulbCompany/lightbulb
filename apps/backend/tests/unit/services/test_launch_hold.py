"""Pre-drop hold: a signed-up account is kept container-less until the Launch
Competition's drop instant. See core/services/launch_hold.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from core.services import launch_hold

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
