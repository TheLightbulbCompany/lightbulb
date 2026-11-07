"""An agent's ask to add a Web Store extension: a Needs-you Activity item any member accepts
once; the agent itself never installs."""

# ruff: noqa: F811 -- moto fixtures imported and then requested by name.
from unittest.mock import AsyncMock, patch

import pytest

from core.services import activity, browser_extensions as ext
from core.services.activity import ActivityActionRequest
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, USER_A, repo, table  # noqa: F401
from tests.unit.services.test_website_logins import env, member  # noqa: F401

EXT_ID = "jblbcpkejogbghfdglhfjlplchcnmohm"


async def test_agent_ask_is_accepted_once_by_any_member(env):
    with (
        patch.object(ext, "fetch_webstore", AsyncMock(return_value=(b"zip", {"_name": "Supademo"}))),
        patch.object(ext.container_repo, "get_browser_extensions", AsyncMock(return_value=[])),
        patch("core.services.website_logins._humans_to_ask", AsyncMock(return_value=[USER_A])),
    ):
        asked = await ext.request_install(
            OWNER, f"https://chromewebstore.google.com/detail/x/{EXT_ID}", agent_id=FRIDAY
        )
    assert asked["status"] == "requested"

    rows = await repo.list_activity(OWNER, USER_A, before_sk=None, limit=10)
    [item] = [i for i in await activity._render_rows(OWNER, USER_A, rows, None) if i.kind == "extension"]
    assert activity.needs_you(item) and item.summary.title == "Friday wants to add Supademo to the shared browser"
    assert [a.id for a in item.actions] == ["add", "decline"]

    add = AsyncMock(return_value={"slug": EXT_ID})
    with (
        patch.object(ext, "add_from_webstore", add),
        patch("core.services.browser_control.restart_soon", AsyncMock(return_value=True)) as restart,
    ):
        done = await activity.run_action(member(), item.id, "add", ActivityActionRequest())
        again = await activity.run_action(member(), item.id, "add", ActivityActionRequest())
    assert done.state == "resolved" and done.actions == [] and again.state == "resolved"
    add.assert_awaited_once_with(OWNER, EXT_ID, USER_A)
    restart.assert_awaited_once_with(OWNER)


async def test_ask_for_an_installed_extension_is_a_no_op(env):
    with patch.object(ext.container_repo, "get_browser_extensions", AsyncMock(return_value=[{"slug": EXT_ID}])):
        assert await ext.request_install(OWNER, EXT_ID, agent_id=FRIDAY) == {"status": "installed"}


@pytest.mark.parametrize("action", ["decline"])
async def test_decline_settles_without_installing(env, action):
    with (
        patch.object(ext, "fetch_webstore", AsyncMock(return_value=(b"zip", {"_name": "Supademo"}))),
        patch.object(ext.container_repo, "get_browser_extensions", AsyncMock(return_value=[])),
        patch("core.services.website_logins._humans_to_ask", AsyncMock(return_value=[USER_A])),
    ):
        rid = (await ext.request_install(OWNER, EXT_ID, agent_id=FRIDAY))["requestId"]
    with patch.object(ext, "add_from_webstore", AsyncMock()) as add:
        done = await activity.run_action(member(), ext.activity_item_id(rid), action, ActivityActionRequest())
    assert done.state == "resolved"
    add.assert_not_called()
