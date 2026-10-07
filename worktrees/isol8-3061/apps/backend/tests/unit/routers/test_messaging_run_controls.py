"""Abort and steer are ledger-resolved HTTP calls to the container plugin.

No gateway RPC and no in-process run map: `_resolved_run` reads the run ledger
row and the room membership, then `_post_run_control` POSTs the plugin's
`/plugins/lightbulb-channel/runs/{runId}/{abort|steer}` route (202 accepted,
404 not active).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import runtime_transport
from core.services.messaging import AbortRunResponse, ApiProblem, SteerRunRequest, SteerRunResponse

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
ROW = {"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"}


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


@contextlib.contextmanager
def _common(row=ROW, response=None, member_error=None, member={"id": AGENT_ID}):
    """The run-control doors over the AgentCore transport seam: `captured` is what
    `runtime_transport.request` was handed."""
    container = {"gateway_token": "gw", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["path"] = path
        captured["json_body"] = json_body
        return response

    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=row),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock, side_effect=member_error),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value=member),
        patch(
            "routers.messaging.container_rpc.running_container",
            new_callable=AsyncMock,
            return_value=container,
        ),
        patch(
            "routers.messaging.chat_gate.gate_chat", new_callable=AsyncMock, return_value={"blocked": False}
        ) as gate_chat_mock,
        patch("routers.messaging.container_repo.touch_last_interactive", new_callable=AsyncMock),
        patch("routers.messaging.runtime_transport.request", fake_request),
    ):
        yield captured, gate_chat_mock


@pytest.mark.asyncio
async def test_abort_on_an_ended_run_is_already_terminal_without_a_call():
    from routers.messaging import abort_run

    with _common(row={**ROW, "status": "ended"}) as (captured, _):
        assert (await abort_run("run-1", auth=_auth())).state == "already-terminal"
    assert captured == {}


@pytest.mark.asyncio
async def test_abort_reports_an_unreachable_runtime_rather_than_a_false_terminal():
    """Only the plugin's own 404 (and no container at all) mean the run ended.

    A 401 from a rotated gateway token during a fleet roll must not be reported
    as `already-terminal` -- the agent is still running and the client would
    stop showing it.
    """
    from routers.messaging import abort_run

    with _common(response=runtime_transport.RuntimeResponse(401, "", {})):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_abort_refuses_a_run_in_a_room_the_caller_is_not_in():
    from routers.messaging import abort_run

    with _common(member_error=ApiProblem("ROOM_FORBIDDEN", "no")) as (captured, _):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert captured == {}


@pytest.mark.asyncio
async def test_abort_refuses_a_run_whose_agent_left_the_room():
    from routers.messaging import abort_run

    with _common(member=None) as (captured, _):
        with pytest.raises(ApiProblem) as exc:
            await abort_run("run-1", auth=_auth())
    assert exc.value.error.code == "RUN_NOT_ACTIVE"
    assert captured == {}


@pytest.mark.asyncio
async def test_steer_gates_then_posts_text():
    from routers.messaging import steer_run

    with _common(response=runtime_transport.RuntimeResponse(202, "{}", {})) as (captured, gate_chat_mock):
        assert await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth()) == SteerRunResponse(run_id="run-1")
    assert captured["path"] == "/plugins/lightbulb-channel/runs/run-1/steer"
    assert captured["json_body"] == {"text": "focus"}
    assert gate_chat_mock.await_args.kwargs["session_key"] == f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


@pytest.mark.asyncio
async def test_abort_goes_through_the_runtime_transport_seam():
    """A run-control call is addressed by runtime, not by a dialled IP."""
    from routers.messaging import abort_run

    agentcore_container = {"gateway_token": "gw", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["container"] = container
        captured["path"] = path
        captured["json_body"] = json_body
        captured["headers"] = headers
        captured["timeout_s"] = timeout_s
        return runtime_transport.RuntimeResponse(202, "{}", {})

    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=ROW),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value={"id": AGENT_ID}),
        patch(
            "routers.messaging.container_rpc.running_container",
            new_callable=AsyncMock,
            return_value=agentcore_container,
        ),
        patch("routers.messaging.runtime_transport.request", fake_request),
    ):
        result = await abort_run("run-1", auth=_auth())

    assert result == AbortRunResponse(run_id="run-1", state="aborting")
    assert captured["container"] is agentcore_container
    assert captured["path"] == "/plugins/lightbulb-channel/runs/run-1/abort"
    assert captured["json_body"] == {}
    assert captured["headers"] == {"Authorization": "Bearer gw"}
    assert captured["timeout_s"] == 10.0


@pytest.mark.asyncio
async def test_steer_404_from_the_plugin_is_run_not_active():
    from routers.messaging import steer_run

    with _common(response=runtime_transport.RuntimeResponse(404, "", {})):
        with pytest.raises(ApiProblem) as exc:
            await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth())
    assert exc.value.error.code == "RUN_NOT_ACTIVE"
