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

from core.services import runtime_transport
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
    with _app_store(), _dedup(), _install(), _container(), _billing(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def test_bot_own_reaction_is_still_dropped_as_an_echo(client):
    """The exemption is for lifecycle events only. On a reaction, `user == our
    bot` really is our own action echoing back, and the plugin's reaction
    handler defaults to reactionNotifications="own" — which admits exactly a
    reaction on the bot's own message — so forwarding it invites a self-trigger."""
    body = _event(type="reaction_added", user="U_BOT", reaction="eyes", text=None)
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing(), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and t.seen == []


def _agentcore_container(row=CONTAINER):
    return {**row, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}


def test_bot_own_member_joined_channel_is_forwarded(client):
    """A membership event legitimately has the bot as its subject. The plugin's
    room-admission path (members.ts reportChannelRoomJoin) fires ONLY on the
    bot's own member_joined_channel — an identity-keyed echo filter eats it, and
    the container never learns it was added to the channel."""
    body = _event(type="member_joined_channel", user="U_BOT", inviter="U_HUMAN", channel_type="C", text=None)
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["content"] = content
        return runtime_transport.RuntimeResponse(200, '{"ok":true}', {})

    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(_agentcore_container()),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    assert captured["content"] == body


def test_unentitled_owner_is_200_no_retry_not_forwarded(client):
    body = _event()
    t = _Upstream()
    with _app_store(), _dedup(), _install(), _container(), _billing("trial_local"), _upstream(t):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200 and r.headers.get("x-slack-no-retry") == "1" and t.seen == []


# NOTE: `test_no_ip_is_503_with_retry_after` was deleted. It drove the ECS-only
# `discover_ip` returns None -> 503 branch, which is unreachable now: `host_for` is pinned to
# "agentcore", so `ip` is always the truthy `AGENTCORE_ADDRESS` sentinel and the ECS
# `discover_ip`/`if not ip` branch never runs. The AgentCore equivalent -- a runtime that
# hasn't answered yet -> 503 + Retry-After with dedup released -- is already covered by
# test_agentcore_cold_session_defers_to_slack_retry_and_releases_dedup below.


# NOTE: `test_forward_is_byte_identical_and_re_signed_with_hook_token` was deleted. It
# asserted a forward to the ECS `http://{ip}:18789/...` target via the raw httpx client,
# which is unreachable now that every container forwards through `runtime_transport.request`
# (`host_for` is pinned to "agentcore"). The identical byte-for-byte / re-signed-with-
# hook-token assertions are already covered on the AgentCore seam by
# test_agentcore_owner_forwards_through_the_runtime_transport_seam below.


def test_agentcore_owner_forwards_through_the_runtime_transport_seam(client):
    """An owner on AgentCore is never Cloud-Map-discovered -- addressing comes
    from the container row's ARN, and the forward goes through
    ``runtime_transport.request`` instead of the ECS httpx client."""
    body = _event()
    agentcore_container = {**CONTAINER, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    sent = _headers(body)
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["container"] = container
        captured["path"] = path
        captured["content"] = content
        captured["headers"] = headers
        captured["timeout_s"] = timeout_s
        return runtime_transport.RuntimeResponse(200, '{"ok":true}', {})

    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(agentcore_container),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=sent)

    assert r.status_code == 200
    assert r.content == b'{"ok":true}'
    assert captured["container"] is agentcore_container
    assert captured["path"] == f"/slack/events/{TEAM}"
    from routers.slack_app import FORWARD_TIMEOUT_S

    assert captured["timeout_s"] == FORWARD_TIMEOUT_S
    assert captured["content"] == body
    assert captured["headers"]["X-Slack-Signature"] == _sig(HOOK_TOKEN, sent["X-Slack-Request-Timestamp"], body)


def test_dedup_recorded_only_before_forward(client):
    body = _event()

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        return runtime_transport.RuntimeResponse(200, '{"ok":true}', {})

    with (
        _app_store(),
        _dedup() as dedup,
        _install(),
        _container(_agentcore_container()),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))
    assert r.status_code == 200
    dedup.assert_awaited_once_with(f"slack:{TEAM}:Ev1", source="slack", ttl_seconds=3600)


def test_form_encoded_slash_payload_routes_by_team_id_field(client):
    body = b"token=x&team_id=" + TEAM.encode() + b"&command=%2Flightbulb&text=hello"
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["content"] = content
        return runtime_transport.RuntimeResponse(200, '{"ok":true}', {})

    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(_agentcore_container()),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and captured["content"] == body


def test_form_encoded_interactive_payload_routes_by_team_id_in_payload(client):
    payload = json.dumps({"type": "block_actions", "team": {"id": TEAM}, "actions": []})
    body = b"payload=" + quote(payload).encode()
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["content"] = content
        return runtime_transport.RuntimeResponse(200, '{"ok":true}', {})

    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(_agentcore_container()),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
        r = client.post(
            "/api/v1/slack/events", content=body, headers=_headers(body, ctype="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200 and captured["content"] == body


# NOTE: `test_upstream_timeout_is_200_with_metric` was deleted. It drove the ECS-only raw
# httpx client's `httpx.RequestError` -> 200 + "slack.forward_failed"/"request_error" metric
# branch, which is unreachable now that every container forwards through
# `runtime_transport.request`. On AgentCore, the SAME failure shape (no answer inside Slack's
# budget) is not a final failure at all -- it's the ordinary "cold microVM" case, which
# defers to Slack's retry (503) instead of swallowing it as 200. That behaviour is already
# covered by test_agentcore_cold_session_defers_to_slack_retry_and_releases_dedup below.


def test_upstream_5xx_is_200(client):
    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        return runtime_transport.RuntimeResponse(500, "", {})

    body = _event()
    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(_agentcore_container()),
        _billing(),
        patch("routers.slack_app.runtime_transport.request", fake_request),
    ):
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


def _agentcore_forward(outcome):
    """A seam fake: ``outcome`` is a RuntimeResponse to return or an exception to raise."""

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fake_request


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(httpx.ReadTimeout("cold"), id="no-answer-inside-slacks-budget"),
        pytest.param(
            runtime_transport.RuntimeResponse(503, '{"error":"gateway still booting"}', {}),
            id="contract-server-booting",
        ),
    ],
)
def test_agentcore_cold_session_defers_to_slack_retry_and_releases_dedup(client, outcome):
    """On AgentCore a parked workspace is the normal first contact: the abandoned invoke is
    what wakes it. The event goes back to Slack's retry (503) and the dedup row is released so
    that retry is delivered instead of swallowed — the ECS path's 503-before-dedup, one step
    later."""
    body = _event()
    agentcore_container = {**CONTAINER, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(agentcore_container),
        _billing(),
        patch("routers.slack_app.webhook_dedup.delete_event", new_callable=AsyncMock) as released,
        patch("routers.slack_app.runtime_transport.request", _agentcore_forward(outcome)),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    released.assert_awaited_once()
    assert "Ev1" in released.await_args.args[0]


def test_agentcore_upstream_4xx_is_still_200_and_keeps_dedup(client):
    # Only a runtime that has not answered yet is handed back to Slack; a real refusal stays final.
    body = _event()
    agentcore_container = {**CONTAINER, "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    with (
        _app_store(),
        _dedup(),
        _install(),
        _container(agentcore_container),
        _billing(),
        patch("routers.slack_app.webhook_dedup.delete_event", new_callable=AsyncMock) as released,
        patch(
            "routers.slack_app.runtime_transport.request",
            _agentcore_forward(runtime_transport.RuntimeResponse(401, "", {})),
        ),
    ):
        r = client.post("/api/v1/slack/events", content=body, headers=_headers(body))

    assert r.status_code == 200
    released.assert_not_awaited()
