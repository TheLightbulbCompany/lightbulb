"""Tests for the APP-LEVEL Meta webhook ingress.

Unlike every other trigger source this route has no owner in its path — Meta
gives one callback URL for the whole app — so the two things worth pinning are
that the app-secret signature is the ONLY door in, and that a verified delivery
lands on the right owner's trigger via the asset→binding map.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

UPSTREAM = "http://paperclip.internal:3100"
VERIFY_TOKEN = "vtok_abc"
APP_SECRET = "appsecret_1"
PAGE_ID = "1112223334"
OTHER_PAGE_ID = "9998887776"


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """The rate buckets live in _public_ingress module state (shared helper) and
    the asset→binding map in this router — both must start empty or tests
    pollute their neighbours."""
    from routers import _public_ingress, meta_webhook

    _public_ingress._RATE.clear()
    meta_webhook._reset_bindings_cache()
    yield
    _public_ingress._RATE.clear()
    meta_webhook._reset_bindings_cache()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", UPSTREAM)
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)


@pytest.fixture
def client():
    return TestClient(app)


def _app_secret(secret: str = APP_SECRET):
    """The platform OAuth app read is a lazy import inside the router, so patch
    the source module rather than a router-level name."""
    return patch(
        "core.services.credentials.platform_oauth_app_store.get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": secret}),
    )


def _binding(
    *,
    asset_id=PAGE_ID,
    public_id="pub_1",
    secret="trigsec_1",
    fields=("leadgen",),
):
    return {
        "asset_id": asset_id,
        "asset_name": "Acme Page",
        "object_type": "page",
        "event_id": fields[0],
        "fields": list(fields),
        "trigger_public_id": public_id,
        "trigger_secret": secret,
    }


def _rows(*bindings, owner_id="user_1", provider="meta_ads"):
    return patch(
        "routers.meta_webhook.integrations_repo.scan_all",
        AsyncMock(return_value=[{"owner_id": owner_id, "provider": provider, "meta_webhook_bindings": list(bindings)}]),
    )


def _billing(status: str | None = "active"):
    return patch(
        "routers.meta_webhook.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self, sink: list[dict]):
        self.sink = sink

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sink.append({"url": str(request.url), "body": request.content, "headers": dict(request.headers)})
        return httpx.Response(202, content=b'{"status":"queued"}')


def _capture(sink: list[dict]):
    return patch(
        "routers.meta_webhook._client_factory",
        lambda: httpx.AsyncClient(transport=_CaptureTransport(sink)),
    )


def _payload(*entries, object_type="page"):
    return json.dumps({"object": object_type, "entry": list(entries)}).encode()


def _entry(asset_id=PAGE_ID, field="leadgen", time_ms=1700000000):
    return {"id": asset_id, "time": time_ms, "changes": [{"field": field, "value": {"x": 1}}]}


def _signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


# --- GET handshake -----------------------------------------------------------


def test_handshake_echoes_challenge_on_matching_verify_token(client):
    resp = client.get(
        "/api/v1/hooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "424242"},
    )
    assert resp.status_code == 200
    assert resp.text == "424242"
    assert resp.headers["content-type"].startswith("text/plain")


def test_handshake_rejects_wrong_verify_token(client):
    resp = client.get(
        "/api/v1/hooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "424242"},
    )
    assert resp.status_code == 403


def test_handshake_503s_when_verify_token_unset(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "")
    resp = client.get(
        "/api/v1/hooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "424242"},
    )
    # An unconfigured environment must not be able to complete a handshake and
    # start receiving deliveries it can't verify — even with an empty token on
    # both sides.
    assert resp.status_code == 503


# --- POST signature ----------------------------------------------------------


def test_post_without_signature_is_401_and_forwards_nothing(client):
    sink: list[dict] = []
    body = _payload(_entry())
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert sink == []


def test_post_with_wrong_signature_is_401_and_forwards_nothing(client):
    sink: list[dict] = []
    body = _payload(_entry())
    headers = _signed(body, secret="not_the_app_secret")
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=headers)
    assert resp.status_code == 401
    assert sink == []


def test_post_signature_over_the_raw_body_only(client):
    """A signature computed over a re-serialized body must not pass — which is
    what pins that verification uses the exact bytes received."""
    sink: list[dict] = []
    body = b'{"object": "page",  "entry": []}'  # extra whitespace, same JSON
    headers = _signed(json.dumps(json.loads(body)).encode())
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=headers)
    assert resp.status_code == 401
    assert sink == []


# --- POST routing ------------------------------------------------------------


def test_verified_delivery_fires_the_bound_trigger(client):
    sink: list[dict] = []
    body = _payload(_entry())
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))

    assert resp.status_code == 200
    assert len(sink) == 1
    call = sink[0]
    assert call["url"] == f"{UPSTREAM}/api/routine-triggers/public/pub_1/fire"
    # Byte-verbatim: Paperclip's run context is the payload Meta sent.
    assert call["body"] == body
    # The trigger's OWN secret, injected after the app-secret verification —
    # Paperclip's bearer mode does the final compare.
    assert call["headers"]["authorization"] == "Bearer trigsec_1"
    assert call["headers"]["idempotency-key"] == f"{PAGE_ID}:1700000000:0"


def test_unknown_asset_is_200_and_forwards_nothing(client):
    sink: list[dict] = []
    body = _payload(_entry(asset_id=OTHER_PAGE_ID))
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))
    # 200, not an error: Meta disables endpoints that keep failing, and someone
    # else's asset arriving here is not our failure.
    assert resp.status_code == 200
    assert sink == []


def test_unentitled_owner_is_not_forwarded(client):
    sink: list[dict] = []
    body = _payload(_entry())
    with _app_secret(), _rows(_binding()), _billing("trial_local"), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert sink == []


def test_multi_entry_payload_fires_once_per_bound_entry(client):
    sink: list[dict] = []
    bound_a = _binding(asset_id=PAGE_ID, public_id="pub_a")
    bound_b = _binding(asset_id=OTHER_PAGE_ID, public_id="pub_b", secret="trigsec_2")
    body = _payload(
        _entry(asset_id=PAGE_ID, time_ms=111),
        {"id": "5550001111", "time": 222, "changes": [{"field": "leadgen", "value": {}}]},
        _entry(asset_id=OTHER_PAGE_ID, time_ms=333),
    )
    with _app_secret(), _rows(bound_a, bound_b), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))

    assert resp.status_code == 200
    # The middle entry names an unbound asset — skipped, the others still fire.
    assert [c["url"].rsplit("/", 2)[-2] for c in sink] == ["pub_a", "pub_b"]
    # Idempotency keys carry the entry's own position, so two entries in one
    # delivery never dedupe into a single run.
    assert [c["headers"]["idempotency-key"] for c in sink] == [f"{PAGE_ID}:111:0", f"{OTHER_PAGE_ID}:333:2"]


def test_same_asset_two_events_routes_by_field(client):
    """One Page bound to two automations is subscribed to the UNION of their
    fields, so every delivery reaches both bindings — the entry's own field
    decides which one actually fires."""
    sink: list[dict] = []
    leads = _binding(public_id="pub_leads", fields=("leadgen",))
    posts = _binding(public_id="pub_posts", secret="trigsec_2", fields=("feed",))
    body = _payload(_entry(field="feed"))
    with _app_secret(), _rows(leads, posts), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))

    assert resp.status_code == 200
    assert [c["headers"]["authorization"] for c in sink] == ["Bearer trigsec_2"]


def test_entry_without_declared_fields_fans_out(client):
    """A messaging-shaped entry declares no `changes` field, so it must reach
    every binding on the asset rather than being dropped by the field filter."""
    sink: list[dict] = []
    dms = _binding(public_id="pub_dms", fields=("messages",))
    body = _payload({"id": PAGE_ID, "time": 999, "messaging": [{"sender": {"id": "u1"}}]})
    with _app_secret(), _rows(dms), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))

    assert resp.status_code == 200
    assert len(sink) == 1


def test_post_503s_when_verify_token_unset(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "")
    sink: list[dict] = []
    body = _payload(_entry())
    with _app_secret(), _rows(_binding()), _billing(), _capture(sink):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))
    assert resp.status_code == 503
    assert sink == []


def test_downstream_failure_still_answers_200(client):
    """Meta redelivers the whole batch on a non-2xx and eventually disables the
    callback for the entire fleet, so a failed fire is logged, not surfaced."""

    class _Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("paperclip down")

    body = _payload(_entry())
    with (
        _app_secret(),
        _rows(_binding()),
        _billing(),
        patch("routers.meta_webhook._client_factory", lambda: httpx.AsyncClient(transport=_Broken())),
    ):
        resp = client.post("/api/v1/hooks/meta", content=body, headers=_signed(body))
    assert resp.status_code == 200


def test_signature_accepts_the_instagram_app_secret(client):
    """Meta splits one app into a Facebook and an Instagram client with their
    own secrets and signs each product's webhooks with the matching one, so any
    auto_meta provider's platform-app secret must be accepted."""
    sink: list[dict] = []
    body = _payload(_entry(field="comments"), object_type="instagram")

    async def _by_name(name: str):
        return {"client_id": "cid", "client_secret": f"secret_for_{name}"}

    ig_binding = _binding(public_id="pub_ig", secret="trigsec_ig", fields=("comments",))
    with (
        patch("core.services.credentials.platform_oauth_app_store.get", AsyncMock(side_effect=_by_name)),
        _rows(ig_binding, provider="instagram"),
        _billing(),
        _capture(sink),
    ):
        resp = client.post(
            "/api/v1/hooks/meta", content=body, headers=_signed(body, secret="secret_for_meta-instagram")
        )

    assert resp.status_code == 200
    assert [c["headers"]["authorization"] for c in sink] == ["Bearer trigsec_ig"]


@pytest.fixture(autouse=True)
def _no_hook_row(monkeypatch):
    """These pin the Paperclip fallback: a binding whose trigger has no `HOOK#` row."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.repositories import automations_repo

    monkeypatch.setattr(automations_repo, "get_hook", _AsyncMock(return_value=None))
