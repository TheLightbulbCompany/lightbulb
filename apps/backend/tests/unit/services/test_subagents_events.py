from unittest.mock import AsyncMock, patch

import pytest

from core.services import subagents
from core.services.messaging import ApiProblem

ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
KEY = f"agent:{AGENT}:lightbulb:channel:{ROOM}"
OWNER = "org_1"


def _body(events=None, key=KEY):
    return subagents.SubagentEventsRequest(
        sessionKey=key,
        events=events
        or [
            {
                "type": "subagent.tool",
                "seq": 1,
                "payload": {"subagent_id": "sa-1", "goal": "g" * 900, "text": "t" * 900},
            }
        ],
    )


def _patches(meta=None, member=True):
    return (
        patch(
            "core.services.subagents.messaging_repo.get_room",
            AsyncMock(return_value=meta if meta is not None else {"ownerId": OWNER}),
        ),
        patch(
            "core.services.subagents.messaging_repo.get_member",
            AsyncMock(return_value={"id": AGENT} if member else None),
        ),
        patch("core.services.subagents._publish", AsyncMock(return_value=True)),
    )


@pytest.mark.asyncio
async def test_publishes_clipped_events_on_the_room():
    a, b, c = _patches()
    with a, b, c as publish:
        assert await subagents.publish_events(OWNER, _body()) == {"published": 1}
    room_id, event = publish.await_args.args
    assert room_id == ROOM and event["type"] == "room.subagent.event"
    [evt] = event["payload"]["events"]
    assert len(evt["payload"]["goal"]) == 500 and len(evt["payload"]["text"]) == 220
    assert event["payload"]["agentId"] == AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("meta,member", [({"ownerId": "org_other"}, True), ({"ownerId": OWNER}, False)])
async def test_foreign_room_or_non_member_agent_is_refused(meta, member):
    a, b, c = _patches(meta, member)
    with a, b, c as publish, pytest.raises(ApiProblem):
        await subagents.publish_events(OWNER, _body())
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_room_session_key_is_invalid():
    with pytest.raises(ApiProblem):
        await subagents.publish_events(OWNER, _body(key="agent:x:main"))


def test_unknown_event_types_and_oversize_batches_are_rejected():
    with pytest.raises(ValueError):
        _body(events=[{"type": "tool.started", "seq": 1, "payload": {}}])
    with pytest.raises(ValueError):
        _body(events=[{"type": "subagent.tool", "seq": i, "payload": {}} for i in range(26)])
