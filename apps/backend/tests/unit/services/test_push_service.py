"""Unit tests for the Expo push sender."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


def _client(post):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _send_capturing_metrics(row, post, clear=None) -> list[dict]:
    """Drive one send and return the dimensions of every push.send emitted."""
    captured: list[dict] = []

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)),
        patch("core.services.push_service.user_repo.clear_push_token", clear or AsyncMock()),
        patch("core.services.push_service.httpx.AsyncClient", return_value=_client(post)),
        patch(
            "core.services.push_service.put_metric",
            side_effect=lambda name, value=1.0, unit="Count", dimensions=None: captured.append(
                {"name": name, **(dimensions or {})}
            ),
        ),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})

    return captured


@pytest.mark.asyncio
async def test_sends_to_all_device_tokens():
    row = {
        "push_tokens": {
            "d1": {"token": "ExponentPushToken[a]", "platform": "ios"},
            "d2": {"token": "ExponentPushToken[b]", "platform": "ios"},
        }
    }
    post = AsyncMock(return_value=_resp({"data": [{"status": "ok"}, {"status": "ok"}]}))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)),
        patch("core.services.push_service.httpx.AsyncClient", return_value=client),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "Friday", "hello", {"agentId": "a1"})

    sent = post.await_args.kwargs["json"]
    assert {m["to"] for m in sent} == {"ExponentPushToken[a]", "ExponentPushToken[b]"}
    assert sent[0]["title"] == "Friday"
    assert sent[0]["body"] == "hello"
    assert sent[0]["data"] == {"agentId": "a1"}


@pytest.mark.asyncio
async def test_prunes_device_not_registered():
    row = {"push_tokens": {"d1": {"token": "ExponentPushToken[dead]", "platform": "ios"}}}
    post = AsyncMock(return_value=_resp({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]}))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)
    clear = AsyncMock()

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)),
        patch("core.services.push_service.user_repo.clear_push_token", clear),
        patch("core.services.push_service.httpx.AsyncClient", return_value=client),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "Friday", "hi", {})

    clear.assert_awaited_once_with("user_1", "d1")


@pytest.mark.asyncio
async def test_no_tokens_is_noop():
    with patch("core.services.push_service.user_repo.get", AsyncMock(return_value={})):
        from core.services import push_service

        # Must not raise and must not construct a client.
        with patch("core.services.push_service.httpx.AsyncClient") as client_ctor:
            await push_service.send_push_to_user("user_1", "t", "b", {})
            client_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_repo_get_failure_does_not_raise():
    # A DynamoDB fault in user_repo.get must NOT escape into the caller.
    with patch(
        "core.services.push_service.user_repo.get",
        AsyncMock(side_effect=Exception("ddb down")),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})  # no raise == pass


@pytest.mark.asyncio
async def test_post_failure_does_not_raise():
    # A transport/HTTP error on the Expo POST must NOT escape into the caller.
    row = {"push_tokens": {"d1": {"token": "ExponentPushToken[a]", "platform": "ios"}}}
    post = AsyncMock(side_effect=Exception("connection reset"))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)),
        patch("core.services.push_service.httpx.AsyncClient", return_value=client),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})  # no raise == pass


@pytest.mark.asyncio
async def test_non_device_not_registered_error_is_logged_not_pruned():
    # Systemic errors (e.g. MessageTooBig) are warned, never pruned.
    row = {"push_tokens": {"d1": {"token": "ExponentPushToken[a]", "platform": "ios"}}}
    post = AsyncMock(return_value=_resp({"data": [{"status": "error", "details": {"error": "MessageTooBig"}}]}))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)
    clear = AsyncMock()

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)),
        patch("core.services.push_service.user_repo.clear_push_token", clear),
        patch("core.services.push_service.httpx.AsyncClient", return_value=client),
        patch("core.services.push_service.logger.warning") as warn,
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})

    clear.assert_not_awaited()
    warn.assert_called_once()


# --- push.send outcome/error_kind mapping -----------------------------------
# The metric is the only signal that a push failed: every failure below is
# deliberately swallowed so it cannot reach the gateway reader loop.

_ONE = {"push_tokens": {"d1": {"token": "ExponentPushToken[a]", "platform": "ios"}}}
_TWO = {
    "push_tokens": {
        "d1": {"token": "ExponentPushToken[a]", "platform": "ios"},
        "d2": {"token": "ExponentPushToken[b]", "platform": "ios"},
    }
}


@pytest.mark.asyncio
async def test_metric_ok_when_every_ticket_is_clean():
    metrics = await _send_capturing_metrics(_TWO, AsyncMock(return_value=_resp({"data": [{"status": "ok"}] * 2})))

    assert metrics == [{"name": "push.send", "outcome": "ok", "error_kind": "none"}]


@pytest.mark.asyncio
async def test_metric_error_when_every_ticket_fails():
    post = AsyncMock(
        return_value=_resp({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]}),
    )
    metrics = await _send_capturing_metrics(_ONE, post)

    assert metrics == [{"name": "push.send", "outcome": "error", "error_kind": "device_not_registered"}]


@pytest.mark.asyncio
async def test_metric_partial_when_only_some_tickets_fail():
    post = AsyncMock(
        return_value=_resp({"data": [{"status": "ok"}, {"status": "error", "details": {"error": "MessageTooBig"}}]}),
    )
    metrics = await _send_capturing_metrics(_TWO, post)

    assert metrics == [{"name": "push.send", "outcome": "partial", "error_kind": "MessageTooBig"}]


@pytest.mark.asyncio
async def test_metric_maps_invalid_credentials_verbatim():
    post = AsyncMock(
        return_value=_resp({"data": [{"status": "error", "details": {"error": "InvalidCredentials"}}]}),
    )
    metrics = await _send_capturing_metrics(_ONE, post)

    assert metrics[0]["error_kind"] == "InvalidCredentials"


@pytest.mark.asyncio
async def test_metric_maps_an_unknown_expo_error_to_other():
    """Expo owns that enum; an unlisted value must not become a new dimension."""
    post = AsyncMock(
        return_value=_resp({"data": [{"status": "error", "details": {"error": "SomeBrandNewExpoError"}}]}),
    )
    metrics = await _send_capturing_metrics(_ONE, post)

    assert metrics[0]["error_kind"] == "other"


@pytest.mark.asyncio
async def test_metric_ticket_mismatch_when_expo_returns_an_error_envelope():
    post = AsyncMock(return_value=_resp({"errors": [{"code": "VALIDATION_ERROR"}]}))
    metrics = await _send_capturing_metrics(_TWO, post)

    assert metrics == [{"name": "push.send", "outcome": "partial", "error_kind": "ticket_mismatch"}]


@pytest.mark.asyncio
async def test_metric_transport_on_a_connection_failure():
    post = AsyncMock(side_effect=httpx.ConnectError("connection reset"))
    metrics = await _send_capturing_metrics(_ONE, post)

    assert metrics == [{"name": "push.send", "outcome": "error", "error_kind": "transport"}]


@pytest.mark.asyncio
async def test_metric_http_on_a_non_2xx_response():
    resp = _resp({})
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("400", request=MagicMock(), response=MagicMock()),
    )
    metrics = await _send_capturing_metrics(_ONE, AsyncMock(return_value=resp))

    assert metrics == [{"name": "push.send", "outcome": "error", "error_kind": "http"}]


@pytest.mark.asyncio
async def test_metric_other_on_a_repository_fault():
    captured: list[dict] = []

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(side_effect=Exception("ddb down"))),
        patch(
            "core.services.push_service.put_metric",
            side_effect=lambda name, value=1.0, unit="Count", dimensions=None: captured.append(dimensions),
        ),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})

    assert captured == [{"outcome": "error", "error_kind": "other"}]


@pytest.mark.asyncio
async def test_no_registered_device_emits_no_metric():
    """Not a send — counting it would drown the real outcomes in no-ops."""
    captured: list[dict] = []

    with (
        patch("core.services.push_service.user_repo.get", AsyncMock(return_value={})),
        patch("core.services.push_service.put_metric", side_effect=lambda *a, **k: captured.append(k)),
    ):
        from core.services import push_service

        await push_service.send_push_to_user("user_1", "t", "b", {})

    assert captured == []
