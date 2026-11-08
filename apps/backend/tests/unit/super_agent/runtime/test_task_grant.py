"""contracts-v2 §14.3: a task run's local-session grant, a background grant like a Paperclip run's."""

import jwt
import pytest

from core import tasks_schema as ts
from core.services import local_session_grants as grants
from core.services.messaging import ApiProblem

OWNER = "org_abc"
AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
RUN = ts.run_id(OWNER, TASK, 1)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")


def _tampered(token: str, **claims) -> str:
    decoded = jwt.decode(token, "hermetic-signing-key", algorithms=["HS256"], audience=grants.AUDIENCE)
    return jwt.encode({**decoded, **claims}, "hermetic-signing-key", algorithm="HS256")


def test_task_grant_round_trip_and_binding():
    token = grants.mint_task(OWNER, AGENT, ts.worker_session_key(AGENT, TASK), RUN, TASK)
    got = grants.verify(token)
    assert (got.room_id, got.requester_id, got.turn_id) == (None, None, RUN)
    assert got.origin == {"kind": "task", "runId": RUN, "taskId": TASK}

    other_agent = "b9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
    other_task = "8d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    for bad in (
        {"sessionKey": ts.worker_session_key(other_agent, TASK)},
        {"sessionKey": ts.worker_session_key(AGENT, other_task)},
        {"agentId": other_agent},
        {"requesterId": "user_1"},
        {"roomId": TASK},
        {"origin": {"kind": "task", "runId": RUN, "taskId": other_task}},
        {"origin": {"kind": "task", "runId": "not-a-uuid", "taskId": TASK}},
    ):
        with pytest.raises(ApiProblem):
            grants.verify(_tampered(token, **bad))


def test_room_grants_unchanged():
    room, event = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-9999-000000000000"
    got = grants.verify(grants.mint(OWNER, room, "ceo", event, "user_1"))
    assert (got.room_id, got.requester_id, got.turn_id) == (room, "user_1", event)
