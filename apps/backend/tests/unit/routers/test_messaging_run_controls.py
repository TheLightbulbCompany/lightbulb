"""Abort and steer are ledger-resolved HTTP calls to the container plugin.

No gateway RPC and no in-process run map: `_resolved_run` reads the run ledger
row and the room membership, then `_post_run_control` POSTs the plugin's
`/plugins/lightbulb-channel/runs/{runId}/{abort|steer}` route (202 accepted,
404 not active).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import AbortRunResponse, ApiProblem, SteerRunRequest, SteerRunResponse

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
ROW = {"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"}


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


@contextlib.contextmanager
def _common(row=ROW, control_status=202, member_error=None, member={"id": AGENT_ID}):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.9"))
    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=row),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock, side_effect=member_error),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value=member),
        patch("routers.messaging.get_ecs_manager", return_value=manager),
        patch(
            "routers.messaging.chat_gate.gate_chat", new_callable=AsyncMock, return_value={"blocked": False}
        ) as gate_chat_mock,
        patch("routers.messaging.container_repo.touch_last_interactive", new_callable=AsyncMock),
        patch("routers.messaging._post_run_control", new_callable=AsyncMock, return_value=control_status) as post,
    ):
        yield post, gate_chat_mock


@pytest.mark.asyncio
async def test_abort_posts_to_the_plugin_route():
    from routers.messaging import abort_run

    with _common() as (post, _):
        assert await abort_run("run-1", auth=_auth()) == AbortRunResponse(run_id="run-1", state="aborting")
    post.assert_awaited_once_with("http://10.0.0.9:18789/plugins/lightbulb-channel/runs/run-1/abort", "gw", {})


@pytest.mark.asyncio
async def test_abort_on_an_ended_run_is_already_terminal_without_a_call():
    from routers.messaging import abort_run

    with _common(row={**ROW, "status": "ended"}) as (post, _):
        assert (await abort_run("run-1", auth=_auth())).state == "already-terminal"
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_abort_reports_an_unreachable_runtime_rather_than_a_false_terminal():
    """Only the plugin's own 404 (and no container at all) mean the run ended.

    A 401 from a rotated gateway token during a fleet roll must not be reported
    as `already-terminal` -- the agent is still running and the client would
    stop showing it.
    """
    from routers.messaging import abort_run

    with _common(control_status=401):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_abort_refuses_a_run_in_a_room_the_caller_is_not_in():
    from routers.messaging import abort_run

    with _common(member_error=ApiProblem("ROOM_FORBIDDEN", "no")) as (post, _):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_abort_refuses_a_run_whose_agent_left_the_room():
    from routers.messaging import abort_run

    with _common(member=None) as (post, _):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "RUN_NOT_ACTIVE"
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_gates_then_posts_text():
    from routers.messaging import steer_run

    with _common() as (post, gate_chat_mock):
        assert await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth()) == SteerRunResponse(run_id="run-1")
    post.assert_awaited_once_with(
        "http://10.0.0.9:18789/plugins/lightbulb-channel/runs/run-1/steer", "gw", {"text": "focus"}
    )
    assert gate_chat_mock.await_args.kwargs["session_key"] == f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


@pytest.mark.asyncio
async def test_steer_404_from_the_plugin_is_run_not_active():
    from routers.messaging import steer_run

    with _common(control_status=404):
        with pytest.raises(ApiProblem) as exc:
            await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth())
    assert exc.value.error.code == "RUN_NOT_ACTIVE"
