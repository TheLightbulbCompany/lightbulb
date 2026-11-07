"""T17: Ask to join from an "Anyone with the link" page (06 §2, D1/D2)."""

from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo
from core.services import local_sessions as service, session_links
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_controls import _events, decide
from tests.unit.services.test_local_session_relay import start
from tests.unit.services.test_local_sessions import ALICE, OWNER

db = registry_tests.db
transport = relay_tests.transport

PRIYA = AuthContext(user_id="user_priya", org_id=None)


@pytest.fixture(autouse=True)
def outside():
    session_links._names.clear()
    with (
        patch(
            "core.services.messaging.human_display_name",
            AsyncMock(side_effect=lambda u: "Priya Shah" if u == PRIYA.user_id else "Alice"),
        ),
        patch("core.services.clerk_admin.get_primary_email", AsyncMock(return_value="priya@acme.com")),
        patch("core.services.clerk_admin.get_user", AsyncMock(return_value={"first_name": "Alice"})),
        patch("core.services.push_service.send_push_to_user", AsyncMock()),
        patch("core.services.email_service.send_email", AsyncMock()) as email,
    ):
        yield email


async def public_session(transport):
    sid = await start(transport)
    await _events(transport, sid, 1)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    return sid, token


@pytest.mark.asyncio
async def test_an_outsider_asks_and_the_owner_sees_who_they_are(transport, outside):
    sid, token = await public_session(transport)
    page = await session_links.open_page(PRIYA, token)
    assert (page.me.can_ask, page.me.join, page.session_id) == (True, None, None)
    assert (await session_links.ask_to_join(PRIYA, token)).join == "pending"
    assert (await session_links.ask_to_join(PRIYA, token)).join == "pending"  # asking again changes nothing
    assert (await session_links.open_page(PRIYA, token, refresh=True)).me.join == "pending"
    assert not service.may_control(await repo.get_session(OWNER, sid), PRIYA.user_id)  # the Allow was undone
    owner_view = service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id)
    assert owner_view.pending_joins == [PRIYA.user_id]
    assert owner_view.pending_join_names == {PRIYA.user_id: "Priya Shah (priya@acme.com)"}
    assert outside.await_count == 0  # dev/test mails only test inboxes; the owner here is not one


@pytest.mark.asyncio
async def test_allow_makes_the_outsider_a_member_with_the_sessions_controls(transport):
    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    with patch(
        "core.services.clerk_admin.create_organization_membership", AsyncMock(return_value={"status": "joined"})
    ) as join:
        await decide(ALICE, sid, PRIYA.user_id, "allow", pushes=[])
    join.assert_awaited_once_with(org_id=OWNER, user_id=PRIYA.user_id, role="org:member")
    row = await repo.get_session(OWNER, sid)
    assert service.may_control(row, PRIYA.user_id)
    page = await session_links.open_page(PRIYA, token, refresh=True)
    assert (page.me.join, page.session_id, page.workspace_id) == ("allowed", sid, OWNER)


@pytest.mark.asyncio
async def test_a_refused_membership_leaves_the_ask_pending(transport):
    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    with (
        patch("core.services.clerk_admin.create_organization_membership", AsyncMock(side_effect=RuntimeError("cap"))),
        pytest.raises(RuntimeError),
    ):
        await decide(ALICE, sid, PRIYA.user_id, "allow", pushes=[])
    assert (await session_links.open_page(PRIYA, token, refresh=True)).me.join == "pending"


@pytest.mark.asyncio
async def test_a_decline_is_silent_and_they_may_ask_again(transport):
    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    pushes = []
    await decide(ALICE, sid, PRIYA.user_id, "decline", pushes)
    assert pushes == []
    page = await session_links.open_page(PRIYA, token, refresh=True)
    assert (page.me.join, page.me.can_ask) == (None, True)


@pytest.mark.asyncio
async def test_link_viewers_always_may_ask_and_team_control_leaves_their_ask_waiting(transport):
    """Owner ruling 2026-10-26: no "Viewers can ask to join" switch; a row left with the old flag
    still takes the ask. Teammates control covers members only, so it answers no outside ask."""
    sid, token = await public_session(transport)
    await service._change_session(ALICE, sid, lambda r: {**r, "askToJoinOff": True}, "ask_to_join_off", None)
    assert (await session_links.open_page(None, token)).me.can_ask is True
    assert (await session_links.ask_to_join(PRIYA, token)).join == "pending"
    await service.set_team_control(ALICE, sid, True)
    assert (await session_links.open_page(PRIYA, token, refresh=True)).me.join == "pending"
    assert PRIYA.user_id in (
        service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id).pending_joins or []
    )


@pytest.mark.asyncio
async def test_a_workspace_link_takes_no_outside_ask(transport):
    sid = await start(transport)
    token = (await service.put_link(ALICE, sid, "workspace")).url.split("#", 1)[1]
    with pytest.raises(ApiProblem) as refused:
        await session_links.ask_to_join(PRIYA, token)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_decline_that_lands_first_never_grants_membership(transport):
    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    await decide(ALICE, sid, PRIYA.user_id, "decline", pushes=[])
    with patch("core.services.clerk_admin.create_organization_membership", AsyncMock()) as join:
        with pytest.raises(ApiProblem):
            await decide(ALICE, sid, PRIYA.user_id, "allow", pushes=[])
    join.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_says_allow_grants_workspace_membership(transport):
    from core.services import activity
    from core.services.local_sessions import OUTSIDER_ALLOW_COPY

    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    items = await activity.list_activity(ALICE, cursor=None, limit=30)
    [item] = [i for i in items.items if i.subtype == "join_request"]
    assert item.summary.reason == OUTSIDER_ALLOW_COPY
    assert "Priya Shah (priya@acme.com)" in item.summary.title


@pytest.mark.asyncio
async def test_a_lost_membership_response_is_settled_by_asking_again(transport):
    sid, token = await public_session(transport)
    await session_links.ask_to_join(PRIYA, token)
    replies = AsyncMock(side_effect=[TimeoutError("lost"), {"status": "already_member"}])
    with patch("core.services.clerk_admin.create_organization_membership", replies):
        await decide(ALICE, sid, PRIYA.user_id, "allow", pushes=[])
    assert replies.await_count == 2
    assert service.may_control(await repo.get_session(OWNER, sid), PRIYA.user_id)
