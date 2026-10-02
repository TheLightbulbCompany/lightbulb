import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from core.services import openai_live


@pytest.mark.asyncio
async def test_create_session_refuses_without_key():
    with patch.object(openai_live.settings, "OPENAI_API_KEY", ""), pytest.raises(openai_live.LiveUnavailable):
        await openai_live.create_session(sdp="v=0\r\n", instructions="x")


@pytest.mark.asyncio
async def test_create_session_passes_sdp_byte_exact():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            201, json={"session": {"id": "live_1"}, "transport": {"type": "webrtc", "sdp": "answer\r\n"}}
        )

    with (
        patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"),
        patch.object(openai_live, "_transport", httpx.MockTransport(handler)),
    ):
        session_id, answer = await openai_live.create_session(sdp="offer\r\n", instructions="be brief")
    assert (session_id, answer) == ("live_1", "answer\r\n")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["transport"] == {"type": "webrtc", "sdp": "offer\r\n"}
    assert seen["body"]["session"]["model"] == "gpt-live-1"
    assert seen["body"]["session"]["delegation"] == {"type": "client"}
    assert seen["body"]["session"]["audio"] == {"output": {"voice": "marin"}}


@pytest.mark.asyncio
async def test_create_session_sends_requested_voice():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"session": {"id": "live_1"}, "transport": {"type": "webrtc", "sdp": "a"}})

    with (
        patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"),
        patch.object(openai_live, "_transport", httpx.MockTransport(handler)),
    ):
        await openai_live.create_session(sdp="o", instructions="i", voice="cedar")
    assert seen["body"]["session"]["audio"]["output"]["voice"] == "cedar"


@pytest.mark.asyncio
async def test_create_session_raises_on_non_201():
    transport = httpx.MockTransport(lambda _r: httpx.Response(400, json={"error": "bad sdp"}))
    with (
        patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"),
        patch.object(openai_live, "_transport", transport),
        pytest.raises(openai_live.LiveUpstreamError),
    ):
        await openai_live.create_session(sdp="x", instructions="y")


class _FakeSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return json.dumps(self.incoming.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_close_session_returns_billed_seconds():
    sock = _FakeSocket(
        [
            {"type": "session.usage.updated", "usage": {"seconds": 12}},
            {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 15.2}},
        ]
    )
    with (
        patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"),
        patch.object(openai_live, "_connect", MagicMock(return_value=sock)),
    ):
        assert await openai_live.close_session("live_1") == 16  # rounded up
    assert sock.sent[0]["type"] == "session.close"


@pytest.mark.asyncio
async def test_close_session_returns_none_when_attach_fails():
    def boom(*_a, **_k):
        raise OSError("gone")

    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"), patch.object(openai_live, "_connect", boom):
        assert await openai_live.close_session("live_1") is None


def test_configured_requires_sk_prefix():
    with patch.object(openai_live.settings, "OPENAI_API_KEY", "Xq9fK2mZp4Lw8RtV1nB7cD3hJ6yA0sGe"):
        assert openai_live.configured() is False
        with pytest.raises(openai_live.LiveUnavailable):
            openai_live._key()
    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-real"):
        assert openai_live.configured() is True
