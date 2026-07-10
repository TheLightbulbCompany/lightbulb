"""Unit tests for the Expo push sender."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


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
