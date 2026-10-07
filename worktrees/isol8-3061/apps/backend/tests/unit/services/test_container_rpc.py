"""``container_rpc.call``'s contract, over the one transport there is.

Everything below goes through ``runtime_transport.request`` -- the transport's own wire shape
(the InvokeAgentRuntime envelope, boto config, error folding) is covered by
``test_runtime_transport.py``; these tests own the RPC contract layered on top of it.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services import container_rpc, runtime_transport
from core.services.messaging import ApiProblem

CONTAINER = {
    "owner_id": "owner_1",
    "gateway_token": "gw",
    "status": "running",
    "agentcore_runtime_arn": "arn:x",
}


def _row(container=CONTAINER):
    return patch(
        "core.services.container_rpc.container_repo.get_by_owner_id",
        new_callable=AsyncMock,
        return_value=container,
    )


async def _call(responder, **kw):
    """``responder`` takes the kwargs ``runtime_transport.request`` was given and returns a
    ``RuntimeResponse`` (or raises)."""
    seen = {}

    async def _fake_request(container, path, **kwargs):
        seen.update(kwargs, container=container, path=path)
        return responder(seen)

    with (
        _row(),
        patch("core.services.container_rpc.runtime_transport.request", _fake_request),
    ):
        result = await container_rpc.call("owner_1", "agents.list", {"a": 1}, **kw)
    return result, seen


@pytest.mark.asyncio
async def test_ok_returns_the_payload_and_posts_the_contract():
    payload, seen = await _call(
        lambda _: runtime_transport.RuntimeResponse(status_code=200, text='{"ok": true, "payload": {"agents": []}}'),
        timeout_s=7,
    )
    assert payload == {"agents": []}
    assert seen["path"] == container_rpc.RPC_PATH
    assert seen["method"] == "POST"
    assert seen["headers"] == {"Authorization": "Bearer gw"}
    # The floor applies to every call now, so the plugin's own budget is the floor, not the 7s.
    assert seen["json_body"] == {
        "method": "agents.list",
        "params": {"a": 1},
        "timeoutMs": int(runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S * 1000),
    }
    # No ip is passed any more -- addressing is the row's runtime ARN.
    assert "ip" not in seen


@pytest.mark.asyncio
async def test_gateway_error_is_a_runtime_error_with_the_error_json():
    error = {"code": "NOT_FOUND", "message": "no such agent"}
    with pytest.raises(RuntimeError) as exc:
        await _call(
            lambda _: runtime_transport.RuntimeResponse(status_code=200, text=json.dumps({"ok": False, "error": error}))
        )
    assert json.loads(str(exc.value)) == error


@pytest.mark.asyncio
async def test_504_is_a_timeout_error():
    with pytest.raises(asyncio.TimeoutError):
        await _call(lambda _: runtime_transport.RuntimeResponse(status_code=504, text='{"ok": false}'))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responder",
    [
        lambda _: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda _: runtime_transport.RuntimeResponse(status_code=502, text="bad gateway"),
    ],
)
async def test_transport_and_non_contract_answers_are_runtime_unavailable(responder):
    with pytest.raises(ApiProblem) as exc:
        await _call(responder)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc.value.error.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"status": "running"}, {"status": "running", "gateway_token": ""}])
async def test_a_row_without_a_gateway_token_is_runtime_unavailable_before_any_invoke(row):
    never = AsyncMock(side_effect=AssertionError("must not reach the transport"))
    with (
        _row(row),
        patch("core.services.container_rpc.runtime_transport.request", never),
        pytest.raises(ApiProblem) as exc,
    ):
        await container_rpc.call("owner_1", "health")
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_no_addressable_row_still_emits_the_unavailable_metric():
    """A no-row/missing-token failure must emit messaging.rpc{outcome:"unavailable"} and a
    latency sample like every other outcome -- resolution used to happen before the timer
    started, so this path emitted nothing."""
    with (
        _row(None),
        patch("core.services.container_rpc.put_metric") as mock_put_metric,
        pytest.raises(ApiProblem),
    ):
        await container_rpc.call("owner_1", "health")
    calls = {c.args[0]: c.kwargs for c in mock_put_metric.call_args_list}
    assert calls["messaging.rpc"]["dimensions"] == {"outcome": "unavailable", "method": "health"}
    assert "messaging.rpc.latency" in calls


@pytest.mark.asyncio
async def test_status_is_not_a_gate_and_a_parked_row_still_invokes():
    """AgentCore wakes the microVM on the invoke itself, so a parked row is addressable and
    there is no wake step to withhold -- a read-only caller simply invokes."""
    parked = dict(CONTAINER, status="stopped")
    with (
        _row(parked),
        patch(
            "core.services.container_rpc.runtime_transport.request",
            AsyncMock(return_value=runtime_transport.RuntimeResponse(status_code=200, text='{"ok": true}')),
        ) as request,
    ):
        await container_rpc.call("owner_1", "health")
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_ingress_floor_applies_to_every_call():
    """F4 (predeploy review, M6-R37): a parked workspace can take 54-57s to boot -- a caller's
    shorter default (e.g. runtime_rpc's 30s) must not fail the first call to a cold workspace."""
    _, seen = await _call(
        lambda _: runtime_transport.RuntimeResponse(status_code=200, text='{"ok": true, "payload": null}'),
        timeout_s=30.0,
    )
    assert seen["timeout_s"] == runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S


@pytest.mark.asyncio
async def test_a_caller_timeout_already_longer_than_the_floor_is_kept():
    """The floor is a minimum, not an override -- room ingress already passes a longer budget
    on its own, and any future caller with a longer one still gets it."""
    _, seen = await _call(
        lambda _: runtime_transport.RuntimeResponse(status_code=200, text='{"ok": true, "payload": null}'),
        timeout_s=120.0,
    )
    assert seen["timeout_s"] == 120.0


@pytest.mark.asyncio
async def test_target_skips_the_row_read_entirely():
    never = AsyncMock(side_effect=AssertionError("target must skip resolution"))
    with (
        patch("core.services.container_rpc.container_repo.get_by_owner_id", never),
        patch(
            "core.services.container_rpc.runtime_transport.request",
            AsyncMock(return_value=runtime_transport.RuntimeResponse(status_code=200, text='{"ok": true}')),
        ),
    ):
        await container_rpc.call("owner_1", "health", target=CONTAINER)
