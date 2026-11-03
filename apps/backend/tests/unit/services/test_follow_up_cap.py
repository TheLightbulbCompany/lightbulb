"""Follow-up cap: 10 per agent per room since the latest human message; hitting it tells the room once."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import messaging_repo as repo
from core.services import messaging
from core.services.messaging import FOLLOW_UP_CAP_NOTICE, ApiProblem, FollowUpRequest
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, _post, _room, table  # noqa: F401


def _req(i, trigger_id):
    return FollowUpRequest.model_validate(
        {
            "followUpId": f"33333333-3333-4333-8333-{i:012d}",
            "roomId": ROOM[0],
            "agentId": FRIDAY,
            "triggerMessageId": trigger_id,
            "eventKind": "user_request",
            "reason": "delegation_completed",
            "text": "done",
        }
    )


ROOM = [""]


async def _notices(room_id):
    rows = await repo._query(repo.room_pk(room_id), begins_with="MSG#")
    return [r for r in rows if r["text"] == FOLLOW_UP_CAP_NOTICE]


@pytest.fixture
def roster():
    with patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": FRIDAY, "name": "Friday"}])):
        yield


@pytest.mark.asyncio
async def test_cap_counts_since_the_human_message_and_notice_posts_once(table, roster):  # noqa: F811
    room_id, _ = await _room()
    ROOM[0] = room_id
    human = await _post(room_id, client_id="h-1")
    for i in range(10):  # the run-7 case (4) and well past it
        assert (await messaging.create_follow_up(OWNER, _req(i, human["messageId"]))).status == "accepted"

    for i in (10, 11):  # the 11th, retried
        with pytest.raises(ApiProblem) as exc:
            await messaging.create_follow_up(OWNER, _req(i, human["messageId"]))
        assert exc.value.status_code == 429 and exc.value.error.code == "RATE_LIMITED"
    notices = await _notices(room_id)
    assert len(notices) == 1
    assert notices[0]["author"] == {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}

    await _post(room_id, client_id="h-2")  # a human reply resets the count
    assert (await messaging.create_follow_up(OWNER, _req(12, human["messageId"]))).status == "accepted"


@pytest.mark.asyncio
async def test_notice_threads_under_a_top_level_trigger(table, roster):  # noqa: F811
    room_id, _ = await _room()
    ROOM[0] = room_id
    human = await _post(room_id, client_id="h-1")
    for i in range(10):
        await messaging.create_follow_up(OWNER, _req(i, human["messageId"]))
    with pytest.raises(ApiProblem):
        await messaging.create_follow_up(OWNER, _req(10, human["messageId"]))
    (notice,) = await _notices(room_id)
    assert notice["threadRootMessageId"] == human["messageId"]


@pytest.mark.asyncio
async def test_without_a_human_message_only_the_last_hour_counts(table, roster):  # noqa: F811
    room_id, _ = await _room()
    ROOM[0] = room_id
    trigger = await _post(room_id, client_id="h-1", author={"kind": "agent", "id": FRIDAY, "displayName": "Friday"})
    assert await repo.latest_human_message(room_id) is None
    old = "2020-01-01T00:00:00+00:00"
    with patch("core.repositories.messaging_repo.utc_now_iso", return_value=old):
        for i in range(10):
            await messaging.create_follow_up(OWNER, _req(i, trigger["messageId"]))
    assert (await messaging.create_follow_up(OWNER, _req(10, trigger["messageId"]))).status == "accepted"


def test_the_cap_notice_claims_only_what_happened():
    assert FOLLOW_UP_CAP_NOTICE == (
        "I paused here so my background work can't loop, and a finished result wasn't delivered. "
        "Ask me here to check where it got to."
    )
