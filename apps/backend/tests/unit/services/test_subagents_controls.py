import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services import subagents
from core.services.messaging import ApiProblem

OWNER, ROOM = "org_1", "11111111-1111-4111-8111-111111111111"
CEO, FIN = "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"


def _warm(warm=True):
    until = int(time.time()) + (600 if warm else -10)
    return patch("core.services.subagents.container_repo.get_session_sync", return_value={"warm_until": until})


def _members():
    return patch(
        "core.services.subagents.messaging_repo.list_members",
        AsyncMock(
            return_value=[{"kind": "agent", "id": CEO}, {"kind": "agent", "id": FIN}, {"kind": "human", "id": "user_1"}]
        ),
    )


@pytest.mark.asyncio
async def test_cold_session_answers_empty_without_rpc():
    with _warm(False), _members(), patch("core.services.subagents.container_rpc.call", AsyncMock()) as rpc:
        assert await subagents.list_for_room(OWNER, ROOM) == {"subagents": []}
        assert (await subagents.tail(OWNER, ROOM, CEO, "sa-1"))["available"] is False
        with pytest.raises(ApiProblem) as exc:
            await subagents.steer(OWNER, ROOM, CEO, "sa-1", "go")
        assert exc.value.error.code == "SUBAGENT_NOT_FOUND"
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_asks_once_for_every_agent_member_and_names_the_agent():
    key = f"agent:{CEO}:lightbulb:channel:{ROOM}"
    rpc = AsyncMock(return_value={"subagents": [{"subagent_id": "sa-1", "goal": "g", "session_key": key}]})
    with _warm(), _members(), patch("core.services.subagents.container_rpc.call", rpc):
        out = await subagents.list_for_room(OWNER, ROOM)
    assert out == {"subagents": [{"subagent_id": "sa-1", "goal": "g", "agent_id": CEO}]}
    method, params = rpc.await_args.args[1], rpc.await_args.args[2]
    assert method == "subagent.list" and sorted(params["sessionKeys"]) == sorted(
        [key, f"agent:{FIN}:lightbulb:channel:{ROOM}"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fn,args,payload",
    [
        (subagents.steer, ("sa-1", "go"), {"queued": False}),
        (subagents.stop, ("sa-1",), {"found": False}),
    ],
)
async def test_unowned_or_finished_subagent_is_404(fn, args, payload):
    with _warm(), patch("core.services.subagents.container_rpc.call", AsyncMock(return_value=payload)):
        with pytest.raises(ApiProblem) as exc:
            await fn(OWNER, ROOM, CEO, *args)
    assert exc.value.error.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError('{"code":"UNAVAILABLE"}'), asyncio.TimeoutError()])
async def test_runtime_errors_are_retryable_unavailable(error):
    with _warm(), _members(), patch("core.services.subagents.container_rpc.call", AsyncMock(side_effect=error)):
        with pytest.raises(ApiProblem) as exc:
            await subagents.list_for_room(OWNER, ROOM)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
