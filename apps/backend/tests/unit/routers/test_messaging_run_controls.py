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
TRIGGER = "33333333-3333-4333-8333-333333333333"
ROW = {"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started", "triggerMessageId": TRIGGER}


@pytest.fixture(autouse=True)
def _no_activity_hint(monkeypatch):
    """close_aborted_run announces "idle"; keep it off AppSync/DynamoDB here."""
    from core.services import messaging

    monkeypatch.setattr(messaging, "announce_run", AsyncMock())


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


captured_close: list = []


@contextlib.contextmanager
def _common(row=ROW, response=None, member_error=None, member={"id": AGENT_ID}, trigger_author="user_1"):
    """The run-control doors over the AgentCore transport seam: `captured` is what
    `runtime_transport.request` was handed."""
    container = {"gateway_token": "gw", "agentcore_runtime_arn": "arn:x"}
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["path"] = path
        captured["json_body"] = json_body
        captured["headers"] = headers
        return response

    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=row),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock, side_effect=member_error),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value=member),
        patch(
            "core.services.openclaw_work.messaging_repo.get_message_by_id",
            new_callable=AsyncMock,
            return_value={"author": {"kind": "human", "id": trigger_author}},
        ),
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
        patch("routers.messaging.service.close_aborted_run", new_callable=AsyncMock) as close,
    ):
        captured_close.append(close)
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
    captured_close[-1].assert_not_awaited()  # an unreachable runtime is not a stopped run


@pytest.mark.asyncio
async def test_abort_of_a_run_the_bridge_no_longer_has_closes_the_ledger():
    from routers.messaging import abort_run

    with _common(response=runtime_transport.RuntimeResponse(404, "", {})):
        assert (await abort_run("run-1", auth=_auth())).state == "already-terminal"
    captured_close[-1].assert_awaited_once_with("org_1", "run-1")


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
    assert "X-Lightbulb-Local-Session-Token" not in captured["headers"]  # the run's own requester steered


async def test_another_members_steer_lowers_the_runs_grant(monkeypatch):
    """Codex #3997: a member steering someone else's run from the app must not borrow that person's
    access (their DMs through lightbulb_read_rooms, their sessions): the steer carries a grant naming nobody."""
    from core.services import local_session_grants
    from routers.messaging import steer_run

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "k" * 32)
    ok = runtime_transport.RuntimeResponse(202, "{}", {})
    with _common(response=ok, trigger_author="user_ada") as (captured, _gate):
        await steer_run("run-1", SteerRunRequest(text="read her DMs"), auth=_auth())
    lowered = local_session_grants.verify(captured["headers"]["X-Lightbulb-Local-Session-Token"])
    assert lowered.requester_id is None and lowered.turn_id == TRIGGER


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
        patch("routers.messaging.service.close_aborted_run", new_callable=AsyncMock) as close,
        patch("routers.messaging.messaging_repo.request_abort", new_callable=AsyncMock) as request_abort,
        patch("routers.messaging._force_close_later") as force_later,
    ):
        result = await abort_run("run-1", auth=_auth())

    # Accepted is not stopped: the run stays open for the bridge's run.ended(aborted), with a
    # grace deadline and a timer that force-closes it if that never comes.
    close.assert_not_awaited()
    request_abort.assert_awaited_once_with("org_1", "run-1")
    force_later.assert_called_once_with("org_1", "run-1")
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


@pytest.mark.asyncio
async def test_close_aborted_run_ends_the_ledger_and_tells_the_room_once():
    from core.services import messaging as service

    publisher = MagicMock(publish_room=AsyncMock(return_value=True))
    ended = {**ROW, "status": "ended", "eventKind": "user_request"}
    with (
        patch.object(service.messaging_repo, "end_run", AsyncMock(side_effect=[ended, None])) as end_run,
        patch.object(service, "get_live_event_publisher", return_value=publisher),
    ):
        await service.close_aborted_run("org_1", "run-1")
        await service.close_aborted_run("org_1", "run-1")  # a second Stop: already closed, no frame
    end_run.assert_awaited_with("org_1", "run-1", outcome="aborted")
    publisher.publish_room.assert_awaited_once()
    room_id, event = publisher.publish_room.await_args.args
    assert room_id == ROOM_ID
    payload = event.payload if hasattr(event, "payload") else event["payload"]
    frame = payload["frame"] if isinstance(payload, dict) else payload.frame
    assert frame["type"] == "run_ended" and frame["outcome"] == "aborted" and frame["runId"] == "run-1"


@pytest.mark.asyncio
async def test_a_stop_the_bridge_never_confirms_is_force_closed_after_the_grace(monkeypatch):
    import asyncio

    import routers.messaging as router

    monkeypatch.setattr(router.messaging_repo, "RUN_ABORT_GRACE_S", 0)
    close = AsyncMock()
    monkeypatch.setattr(router.service, "close_aborted_run", close)
    router._force_close_later("org_1", "run-1")
    await asyncio.gather(*router._abort_timers)
    close.assert_awaited_once_with("org_1", "run-1", forced=True)


@pytest.mark.asyncio
async def test_a_forced_close_tells_the_room_the_agent_may_still_be_finishing():
    from core.services import messaging as service

    publisher = MagicMock(publish_room=AsyncMock(return_value=True))
    ended = {**ROW, "status": "ended", "eventKind": "user_request"}
    with (
        patch.object(service.messaging_repo, "end_run", AsyncMock(return_value=ended)),
        patch.object(service, "get_live_event_publisher", return_value=publisher),
    ):
        await service.close_aborted_run("org_1", "run-1", forced=True)
    event = publisher.publish_room.await_args.args[1]
    payload = event.payload if hasattr(event, "payload") else event["payload"]
    frame = payload["frame"] if isinstance(payload, dict) else payload.frame
    assert frame["type"] == "run_ended" and frame["outcome"] == "aborted" and frame["forced"] is True


@pytest.mark.asyncio
async def test_the_sweeper_closes_an_unconfirmed_stop_as_aborted_not_failed(monkeypatch):
    from core.services import run_ledger

    row = {**ROW, "ownerId": "org_1", "deadlineAt": 1, "abortRequestedAt": "2026-09-25T20:20:33Z"}
    close, end_run = AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        run_ledger.messaging_repo, "list_overdue_runs", AsyncMock(return_value=[{"ownerId": "org_1", "runId": "run-1"}])
    )
    monkeypatch.setattr(run_ledger.messaging_repo, "get_run", AsyncMock(return_value=row))
    monkeypatch.setattr(run_ledger.messaging_repo, "end_run", end_run)
    monkeypatch.setattr(run_ledger, "close_aborted_run", close)
    assert await run_ledger.sweep_runs_once() == 1
    close.assert_awaited_once_with("org_1", "run-1", forced=True)
    end_run.assert_not_awaited()  # no error outcome, no failure message
