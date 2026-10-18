import json

import httpx
import pytest

from core.config import settings
from core.services import sendblue


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(settings, "SENDBLUE_API_KEY_ID", "kid")
    monkeypatch.setattr(settings, "SENDBLUE_API_SECRET_KEY", "sec")
    monkeypatch.setattr(settings, "SENDBLUE_WEBHOOK_SECRET", "wh")
    yield
    monkeypatch.setattr(sendblue, "_transport", None)


def _mock(monkeypatch, handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(sendblue, "_transport", httpx.MockTransport(wrapped))
    return calls


async def test_send_text_posts_number_from_and_content(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"status": "QUEUED", "message_handle": "h1"}))
    out = await sendblue.send_text(from_number="+14155550100", to_number="+14155550199", content="hi")
    assert out["message_handle"] == "h1"
    (req,) = calls
    assert req.method == "POST"
    assert req.url.path == "/api/send-message"
    assert json.loads(req.content) == {"number": "+14155550199", "from_number": "+14155550100", "content": "hi"}
    assert req.headers["sb-api-key-id"] == "kid"
    assert req.headers["sb-api-secret-key"] == "sec"


async def test_declined_raises_with_error_key(monkeypatch):
    _mock(
        monkeypatch,
        lambda r: httpx.Response(400, json={"status": "DECLINED", "error_key": "PRE_REPLY_LIMIT_REACHED"}),
    )
    with pytest.raises(sendblue.SendblueError) as ei:
        await sendblue.send_text(from_number="+14155550100", to_number="+14155550199", content="hi")
    assert ei.value.code == "PRE_REPLY_LIMIT_REACHED"
    assert ei.value.status == 400


async def test_error_status_in_2xx_body_raises(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"status": "ERROR", "error": "bad_number"}))
    with pytest.raises(sendblue.SendblueError) as ei:
        await sendblue.send_text(from_number="+14155550100", to_number="+14155550199", content="hi")
    assert ei.value.code == "bad_number"


async def test_non_json_error_falls_back_to_http_code(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(sendblue.SendblueError) as ei:
        await sendblue.send_text(from_number="+14155550100", to_number="+14155550199", content="hi")
    assert ei.value.code == "http_502"
    assert ei.value.status == 502


async def test_fictional_555_number_is_a_dry_run(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(500))
    out = await sendblue.send_text(from_number="+14155550100", to_number="+15555550123", content="hi")
    assert calls == []
    assert out["dry_run"] is True
    assert out["status"] == "QUEUED"
    assert out["message_handle"].startswith("dry-")


async def test_provision_line_chains_preview_and_confirm(monkeypatch):
    def handler(r):
        if r.url.path == "/accounts/lines/provision-preview":
            return httpx.Response(200, json={"status": "OK", "preview_token": "t"})
        return httpx.Response(200, json={"status": "OK", "phone_number": "+14155550142"})

    calls = _mock(monkeypatch, handler)
    assert await sendblue.provision_line() == "+14155550142"
    assert [c.url.path for c in calls] == ["/accounts/lines/provision-preview", "/accounts/lines/provision-confirm"]
    assert json.loads(calls[1].content) == {"preview_token": "t"}


async def test_deprovision_chains_preview_and_confirm(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"status": "OK", "preview_token": "d"}))
    await sendblue.deprovision_line("+14155550142")
    assert [c.url.path for c in calls] == ["/accounts/lines/deprovision-preview", "/accounts/lines/deprovision-confirm"]
    assert json.loads(calls[0].content) == {"phone_number": "+14155550142"}
    assert json.loads(calls[1].content) == {"preview_token": "d"}


async def test_provision_daily_limit_surfaces_code(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(429, json={"status": "ERROR", "error": "daily_limit_reached"}))
    with pytest.raises(sendblue.SendblueError) as ei:
        await sendblue.provision_line()
    assert ei.value.code == "daily_limit_reached"
    assert ei.value.status == 429


async def test_typing_never_raises(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(503))
    assert await sendblue.send_typing(from_number="+14155550100", to_number="+14155550199") is None
    assert calls[0].url.path == "/api/send-typing-indicator"
    assert json.loads(calls[0].content) == {"number": "+14155550199", "from_number": "+14155550100"}


def test_enabled_requires_all_three_settings(monkeypatch):
    assert sendblue.enabled() is True
    for name in ("SENDBLUE_API_KEY_ID", "SENDBLUE_API_SECRET_KEY", "SENDBLUE_WEBHOOK_SECRET"):
        monkeypatch.setattr(settings, name, "")
        assert sendblue.enabled() is False
        monkeypatch.setattr(settings, name, "x")
