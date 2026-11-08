"""A hold on the workspace's network member (a stranger's first message, waiting for a person's
Allow) as a Needs-you item. The notice is content-free, so no item ever carries message text."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import activity, network_member
from core.services.activity import ActivityActionRequest
from core.services.messaging import ApiProblem
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    OWNER,
    USER_A,
    USER_B,
    repo,
    table,
)


@pytest.fixture
def env(table):
    rpc = AsyncMock(return_value={})
    with (
        patch(
            "core.repositories.workspace_repo.WorkspaceRepo.get_workspace",
            AsyncMock(return_value=SimpleNamespace(member_user_ids=[USER_A, USER_B])),
        ),
        patch("core.services.network_member._invalidate_activity", AsyncMock()),
        patch("core.services.container_rpc.call", rpc),
    ):
        yield rpc


def viewer(user=USER_A):
    return AuthContext(user_id=user, org_id=OWNER, org_role="org:member")


def notice(state="open", target="friday", hold="h1"):
    return {"holdId": hold, "state": state, "label": "Aden's Claude", "target": target}


async def _items(user):
    rows = await repo.list_activity(OWNER, user, before_sk=None, limit=10)
    return await activity._render_rows(OWNER, user, rows, None)


async def test_a_notice_files_one_item_per_asked_human_with_no_text(env):
    await network_member.hold_notice(OWNER, notice())
    await network_member.hold_notice(OWNER, notice())  # a repeated notice files nothing new
    for user in (USER_A, USER_B):
        rows = await repo.list_activity(OWNER, user, before_sk=None, limit=10)
        assert len(rows) == 1 and rows[0]["kind"] == "network_hold"
        [item] = await _items(user)
        assert item.id == "nethold:h1" and item.kind == "session" and item.subtype == "join_request"
        assert item.state == "open" and item.summary.preview is None
        assert item.summary.reason == activity.NETWORK_HOLD_REASON
        assert "Aden's Claude" in item.summary.title
        assert [(a.id, a.label) for a in item.actions] == [
            ("decline", "Deny"),
            ("send", "Send"),
            ("allow", "Always allow"),
        ]


async def test_an_app_session_hold_goes_to_that_person_only(env):
    target = f"github-{network_member.app_person(USER_B)}"
    await network_member.hold_notice(OWNER, notice(target=target))
    assert await _items(USER_A) == []
    [item] = await _items(USER_B)
    assert item.id == "nethold:h1" and "your github" in item.summary.title


@pytest.mark.parametrize(
    ("action", "verdict"),
    [
        ("send", {"verdict": "allow"}),
        ("decline", {"verdict": "decline"}),
        ("allow", {"verdict": "allow", "scope": "always"}),
    ],
)
async def test_a_tap_answers_the_hold_with_decided_by_the_tapper(env, action, verdict):
    await network_member.hold_notice(OWNER, notice())
    item = await activity.run_action(viewer(USER_B), "nethold:h1", action, ActivityActionRequest())
    env.assert_awaited_once_with(OWNER, "network.hold", {"hold": "h1", **verdict, "decided_by": USER_B})
    assert item.state == "resolved" and item.actions == []
    # A second tap is the current state, not a second answer.
    await activity.run_action(viewer(USER_A), "nethold:h1", action, ActivityActionRequest())
    assert env.await_count == 1


async def test_a_closed_notice_resolves_the_item(env):
    await network_member.hold_notice(OWNER, notice())
    await network_member.hold_notice(OWNER, notice(state="closed"))
    [item] = await _items(USER_A)
    assert item.state == "resolved" and item.actions == []
    await network_member.hold_notice(OWNER, notice(state="closed", hold="never-opened"))  # no-op


async def test_another_viewer_cannot_answer(env):
    await network_member.hold_notice(OWNER, notice(target=f"github-{network_member.app_person(USER_B)}"))
    for item_id in ("nethold:h1", "nethold:nope", "nethold:a:b"):
        with pytest.raises(ApiProblem) as exc:
            await activity.run_action(viewer(USER_A), item_id, "send", ActivityActionRequest())
        assert exc.value.error.code == "ACTIVITY_NOT_FOUND"
    env.assert_not_awaited()


async def test_the_internal_route_needs_the_hook_token(env, async_client):
    url = f"/api/v1/internal/messaging/network/holds/{OWNER}"
    with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"})):
        assert (await async_client.post(url, json=notice())).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (await async_client.post(url, json=notice(), headers=bad)).status_code == 401
        good = {"Authorization": "Bearer tok"}
        # Text never rides along: an extra field is refused, and the refusal does not echo it.
        refused = await async_client.post(url, json={**notice(), "text": "held words"}, headers=good)
        assert refused.status_code == 400 and "held words" not in refused.text
        assert (await async_client.post(url, json=notice(), headers=good)).status_code == 204
    assert [i.id for i in await _items(USER_A)] == ["nethold:h1"]


async def test_an_app_hold_whose_person_left_asks_nobody(env):
    await network_member.hold_notice(OWNER, notice(target=f"github-{network_member.app_person('user_gone')}"))
    assert await _items(USER_A) == [] and await _items(USER_B) == []


async def test_a_lost_activity_row_is_refiled_by_the_repeat_notice(env, caplog):
    real = repo.put_activity_row
    calls = {"n": 0}

    async def flaky(item):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("throttled")
        await real(item)

    with patch.object(repo, "put_activity_row", flaky), pytest.raises(RuntimeError):
        await network_member.hold_notice(OWNER, notice())
    assert "network hold activity row failed" in caplog.text
    lost = [u for u in (USER_A, USER_B) if not await _items(u)]
    assert len(lost) == 1
    await network_member.hold_notice(OWNER, notice())  # the bridge resends
    for user in (USER_A, USER_B):
        [item] = await _items(user)
        assert item.id == "nethold:h1" and item.state == "open"


@pytest.mark.parametrize("error", ['{"code": "hold_not_found"}', None])
async def test_a_gone_hold_answers_hold_gone_and_resolves(env, error):
    await network_member.hold_notice(OWNER, notice())
    if error:  # the engine no longer has it
        env.side_effect = RuntimeError(error)
    else:  # a second tap raced past the open check after the first closed it

        async def first_tap_won(*_a, **_k):
            await network_member.hold_notice(OWNER, notice(state="closed"))
            raise RuntimeError('{"code": "UNAVAILABLE"}')

        env.side_effect = first_tap_won
    with pytest.raises(ApiProblem) as exc:
        await activity.run_action(viewer(), "nethold:h1", "send", ActivityActionRequest())
    assert exc.value.error.code == "HOLD_GONE" and exc.value.status_code == 409
    [item] = await _items(USER_A)
    assert item.state == "resolved"


async def test_another_engine_error_keeps_the_hold_open(env):
    await network_member.hold_notice(OWNER, notice())
    env.side_effect = RuntimeError('{"code": "UNAVAILABLE"}')
    with pytest.raises(RuntimeError):
        await activity.run_action(viewer(), "nethold:h1", "send", ActivityActionRequest())
    [item] = await _items(USER_A)
    assert item.state == "open"
