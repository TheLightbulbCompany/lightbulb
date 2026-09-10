import uuid

import pytest

from core.auth import AuthContext
from core.services.messaging import (
    ApiProblem,
    CreateRoomMember,
    CreateRoomRequest,
    create_room,
    get_agent_receipt,
)


@pytest.mark.asyncio
async def test_get_agent_receipt_maps_malformed_event_id_to_400_not_500():
    with pytest.raises(ApiProblem) as exc_info:
        await get_agent_receipt("user_owner", "bad/event/id")

    assert exc_info.value.status_code == 400
    assert exc_info.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_create_room_over_member_limit_returns_409_not_a_pydantic_400():
    auth = AuthContext(user_id="user_owner")
    members = [CreateRoomMember(kind="human", id=f"user_{i}", role="member", listen_mode=None) for i in range(32)]
    request = CreateRoomRequest(client_room_id=uuid.uuid4(), name=None, kind="group-dm", members=members)

    with pytest.raises(ApiProblem) as exc_info:
        await create_room(auth, request)

    assert exc_info.value.status_code == 409
    assert exc_info.value.error.code == "MEMBER_LIMIT"
