"""POST /api/v1/slack/events — the platform Slack app's single Request URL.

Pins: the app signing secret is the only door in; a verified event lands on
the RIGHT owner's container, re-signed with THAT owner's hook_token, body
byte-identical; and every per-owner failure is a 200 (never lets one tenant
count against the shared app), while "no IP" is a 503 so Slack retries.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

APP_SECRET = "app-signing-secret"
HOOK_TOKEN = "owner-hook-token"
TEAM = "T0123ABC"
INSTALL = {"team_id": TEAM, "owner_id": "org_1", "agent_id": "agent_f", "bot_user_id": "U_BOT", "status": "active"}
CONTAINER = {"owner_id": "org_1", "hook_token": HOOK_TOKEN, "service_name": "openclaw-org-1-abc", "status": "running"}


@pytest.fixture
def client():
    return TestClient(app)


def _sig(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, secret: str = APP_SECRET, ts: str | None = None, ctype: str = "application/json"):
    ts = ts or str(int(time.time()))
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": _sig(secret, ts, body), "Content-Type": ctype}


def _event(event_id="Ev1", **event):
    e = {"type": "message", "user": "U_HUMAN", "text": "hi", "channel": "C1"}
    e.update(event)
    return json.dumps({"type": "event_callback", "team_id": TEAM, "event_id": event_id, "event": e}).encode()


def _app_store():
    return patch(
        "core.services.credentials.platform_oauth_app_store.get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "cs", "signing_secret": APP_SECRET}),
    )


def _dedup(result="recorded"):
    from core.services.webhook_dedup import WebhookDedupResult

    return patch(
        "routers.slack_app.webhook_dedup.record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult(result)),
    )


def _install(row=INSTALL):
    return patch("routers.slack_app.slack_installs_repo.get", AsyncMock(return_value=row))


def _container(row=CONTAINER):
    return patch("routers.slack_app.container_repo.get_by_owner_id", AsyncMock(return_value=row))


def _billing(status="active"):
    return patch(
        "routers.slack_app.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


def _ip(ip="10.0.42.17"):
    return patch("routers.slack_app.ecs_manager.discover_ip", return_value=ip)


class _Upstream(httpx.AsyncBaseTransport):
    def __init__(self, status=200, raise_exc=None):
        self.status, self.raise_exc, self.seen = status, raise_exc, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, content=b'{"ok":true}')


def _upstream(t: _Upstream):
    return patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t))


def test_url_verification_echoes_challenge_when_signed(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_bad_signature_is_401(client):
    body = _event()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body, secret="wrong"))
    assert r.status_code == 401


def test_stale_timestamp_is_401(client):
    body = _event()
    with _app_store():
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body, ts=str(int(time.time()) - 400)))
    assert r.status_code == 401


def test_unseeded_app_is_503(client):
    body = _event()
    with patch("core.services.credentials.platform_oauth_app_store.get", AsyncMock(return_value=None)):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 503


def test_duplicate_event_is_200_and_not_forwarded(client):
    body = _event()
    t = _Upstream()
    # Dedup now runs right before the forward, after install/container/billing/ip
    # all passed — those must be patched too or the duplicate never reaches it.
    with (
        _app_store(),
        _dedup("already_seen"),
        _install(),
        _container(),
        _billing(),
        _ip(),
        _upstream(t),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def test_unknown_team_is_200(client):
    body = _event()
    with _app_store(), _dedup(), _install(None):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200


def test_own_bot_echo_is_dropped(client):
    body = _event(user="U_BOT")
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def test_unentitled_owner_is_200_no_retry_not_forwarded(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing("trial_local"), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and r.headers.get("x-slack-no-retry") == "1" and t.seen == []


def test_no_ip_is_503_with_retry_after(client):
    body = _event()
    with _app_store(), _dedup() as dedup, _install(), _container(), _billing(), _ip(None):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 503 and "Retry-After" in r.headers
    # Critical 3: dedup must not be recorded until we're about to forward — a
    # 503 here leaves no dedup row, so Slack's retry is actually delivered.
    dedup.assert_not_awaited()


def test_forward_is_byte_identical_and_re_signed_with_hook_token(client):
    body = _event()
    t = _Upstream()
    sent = _headers(body)
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=sent)
    assert r.status_code == 200
    assert r.content == b'{"ok":true}'
    (req,) = t.seen
    assert str(req.url) == f"http://10.0.42.17:18789/slack/events/{TEAM}"
    assert req.content == body
    ts = req.headers["x-slack-request-timestamp"]
    assert ts == sent["X-Slack-Request-Timestamp"]
    assert req.headers["x-slack-signature"] == _sig(HOOK_TOKEN, ts, body)
    assert req.headers["content-type"] == "application/json"
    assert "authorization" not in {k.lower() for k in req.headers}
    assert "x-forwarded-for" not in {k.lower() for k in req.headers}


def test_dedup_recorded_only_before_forward(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup() as dedup, _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    dedup.assert_awaited_once_with(f"slack:{TEAM}:Ev1", source="slack", ttl_seconds=3600)


def test_form_encoded_slash_payload_routes_by_team_id_field(client):
    body = b"token=x&team_id=" + TEAM.encode() + b"&command=%2Flightbulb&text=hello"
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and t.seen[0].content == body


def test_form_encoded_interactive_payload_routes_by_team_id_in_payload(client):
    payload = json.dumps({"type": "block_actions", "team": {"id": TEAM}, "actions": []})
    body = b"payload=" + quote(payload).encode()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and t.seen[0].content == body


def test_upstream_timeout_is_200_with_metric(client):
    body = _event()
    t = _Upstream(raise_exc=httpx.ReadTimeout("slow"))
    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(),
        _billing(),
        _ip(),
        _upstream(t),
        patch("routers.slack_app.put_metric") as metric,
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    assert any(c.args and c.args[0] == "slack.forward_failed" for c in metric.call_args_list)
    assert metric.call_args_list[0].kwargs["dimensions"] == {"reason": "request_error"}


def test_upstream_5xx_is_200(client):
    body = _event()
    t = _Upstream(status=500)
    with _app_store(), _dedup(), _install(), _container(), _billing(), _ip(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200


def test_app_uninstalled_revokes_and_is_200(client):
    body = json.dumps(
        {"type": "event_callback", "team_id": TEAM, "event_id": "Ev9", "event": {"type": "app_uninstalled"}}
    ).encode()
    with (
        _app_store(),
        _dedup() as dedup,
        _install(),
        patch("routers.slack_app.slack_install_service.revoke_install", AsyncMock(return_value=True)) as rev,
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    # Dedup is recorded FIRST for revoke events (they never 503, so there's no
    # reason to skip dedup and risk re-rolling the container on every retry).
    dedup.assert_awaited_once_with(f"slack:{TEAM}:Ev9", source="slack", ttl_seconds=3600)
    rev.assert_awaited_once_with(TEAM)


def test_duplicate_app_uninstalled_is_200_without_revoke(client):
    body = json.dumps(
        {"type": "event_callback", "team_id": TEAM, "event_id": "Ev9", "event": {"type": "app_uninstalled"}}
    ).encode()
    with (
        _app_store(),
        _dedup("already_seen"),
        _install(),
        patch("routers.slack_app.slack_install_service.revoke_install", AsyncMock(return_value=True)) as rev,
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    rev.assert_not_awaited()


def test_revoke_failure_is_still_200(client):
    body = json.dumps(
        {"type": "event_callback", "team_id": TEAM, "event_id": "Ev10", "event": {"type": "app_uninstalled"}}
    ).encode()
    with (
        _app_store(),
        _dedup(),
        _install(),
        patch(
            "routers.slack_app.slack_install_service.revoke_install",
            AsyncMock(side_effect=RuntimeError("efs")),
        ),
        patch("routers.slack_app.put_metric") as metric,
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    assert any(c.args and c.args[0] == "slack.revoke_failed" for c in metric.call_args_list)
