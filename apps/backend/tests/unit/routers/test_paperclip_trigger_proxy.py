"""Tests for the public Paperclip routine-trigger ingress proxy.

This router deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have Clerk
tokens. The real auth is Paperclip's per-trigger signing, verified
upstream. The owner_id segment drives edge gating only, and is BOUND to
the trigger via the instance-admin binding resolver before any
entitlement it names is honoured.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from main import app

UPSTREAM = "http://paperclip.internal:3100"
PUBLIC_ID = "abc123def456abc123def456"
SIBLING_ID = "fedcba987654fedcba987654"


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Rate buckets live in _public_ingress module state (shared helper), and
    the binding + gmail-token caches in the trigger router — all must start
    empty or tests pollute their neighbours."""
    from routers import _public_ingress, paperclip_trigger_proxy

    _public_ingress._RATE.clear()
    paperclip_trigger_proxy._BINDING_CACHE.clear()
    paperclip_trigger_proxy._GMAIL_TOKEN_CACHE.clear()
    yield
    _public_ingress._RATE.clear()
    paperclip_trigger_proxy._BINDING_CACHE.clear()
    paperclip_trigger_proxy._GMAIL_TOKEN_CACHE.clear()


@pytest.fixture(autouse=True)
def _paperclip_url(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", UPSTREAM)


@pytest.fixture
def client():
    return TestClient(app)


def _mock_billing(status: str | None):
    """None = no billing row; otherwise a row with that subscription status."""
    return patch(
        "routers.paperclip_trigger_proxy.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


def _mock_binding(bound: bool):
    """Mock the owner↔trigger binding resolver."""
    return patch(
        "routers.paperclip_trigger_proxy._resolve_trigger_binding",
        AsyncMock(return_value=bound),
    )


class _StubTransport(httpx.AsyncBaseTransport):
    def __init__(self, captured: dict, status: int = 202, content: bytes = b'{"status":"queued"}'):
        self.captured = captured
        self.status = status
        self.content = content

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured["url"] = str(request.url)
        self.captured["body"] = request.content
        self.captured["headers"] = dict(request.headers)
        return httpx.Response(self.status, content=self.content)


def _stub_client(captured: dict, **kw):
    transport = _StubTransport(captured, **kw)
    return patch(
        "routers.paperclip_trigger_proxy._client_factory",
        lambda: httpx.AsyncClient(transport=transport),
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_trial_local_owner_returns_402(client):
    """Triggers are a paid always-on feature — trial owners get the
    structured 402 before anything is resolved or forwarded."""
    captured: dict = {}
    with _mock_billing("trial_local"), _mock_binding(True), _stub_client(captured):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["blocked"] is True
    assert detail["code"] == "always_on_requires_subscription"
    assert captured == {}  # nothing reached upstream


def test_unknown_owner_fails_closed_with_402(client):
    """No billing row (unknown / fabricated owner_id) = not entitled."""
    with _mock_billing(None):
        r = client.post(f"/api/v1/hooks/t/u_404/{PUBLIC_ID}", json={})
    assert r.status_code == 402


def test_owner_substitution_is_rejected_with_404(client):
    """THE binding gate: a caller naming a PAID owner's id for a trigger
    that owner does not own must be refused — otherwise a trial owner
    could borrow any paid owner's entitlement to fire their own trigger."""
    captured: dict = {}
    with (
        _mock_billing("active"),  # the substituted owner IS entitled...
        _mock_binding(False),  # ...but does not own this trigger
        _stub_client(captured),
    ):
        r = client.post(f"/api/v1/hooks/t/u_paid_victim/{PUBLIC_ID}", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown_trigger"
    assert captured == {}  # nothing forwarded on a failed binding


def test_binding_resolver_failure_fails_closed_with_503(client):
    """Paperclip down / admin session broken = 503, never fail-open."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        patch(
            "routers.paperclip_trigger_proxy._resolve_trigger_binding",
            AsyncMock(side_effect=RuntimeError("paperclip down")),
        ),
        _stub_client(captured),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 503
    assert captured == {}


async def test_invalid_public_id_rejected_with_400(client):
    """publicIds are hex upstream; non-alphanumerics are URL-injection
    attempts and oversized ids would make unbounded rate-limit keys.

    The ``..`` case calls the handler directly — TestClient/httpx
    URL-normalize dot segments before the request leaves the client, but a
    raw HTTP client can send them verbatim (same rationale as the
    old hooks-proxy traversal test)."""
    from fastapi import HTTPException

    from routers.paperclip_trigger_proxy import proxy_trigger

    for bad in ("..", "abc/def", "abc def", "", "a" * 65):
        with pytest.raises(HTTPException) as exc:
            await proxy_trigger(owner_id="u_1", public_id=bad, request=None)  # type: ignore[arg-type]
        assert exc.value.status_code == 400
        assert exc.value.detail == "invalid_public_id"

    # Percent-encoded slash through the real stack: starlette decodes it
    # BEFORE routing, so "abc%2Fdef" becomes two path segments and never
    # matches the single-segment route — 404 at the router, equally safe.
    with _mock_billing("active"):
        r = client.post("/api/v1/hooks/t/u_1/abc%2Fdef", json={})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Binding resolver
# ---------------------------------------------------------------------------


def _resolver_env(routines: list[dict], details: dict[str, dict]):
    """Patch the resolver's collaborators: repo → company c_1, cached admin
    cookie, and an admin client serving the given list/detail payloads."""
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=MagicMock(company_id="c_1"))
    admin = MagicMock()
    admin.list_routines = AsyncMock(return_value=routines)
    admin.get_routine = AsyncMock(side_effect=lambda *, session_cookie, routine_id: details[routine_id])
    return (
        patch("routers.paperclip_trigger_proxy.PaperclipRepo", return_value=repo),
        patch("routers.paperclip_trigger_proxy.get_admin_session_cookie", AsyncMock(return_value="s=1")),
        patch("routers.paperclip_trigger_proxy.PaperclipAdminClient", return_value=admin),
        admin,
    )


async def test_resolver_matches_public_id_and_caches():
    from routers import paperclip_trigger_proxy as mod

    routines = [
        {"id": "r_cron", "triggers": [{"kind": "cron"}]},  # skipped, no detail fetch
        {"id": "r_hook", "triggers": [{"kind": "webhook"}]},
    ]
    details = {"r_hook": {"triggers": [{"kind": "webhook", "publicId": PUBLIC_ID}]}}
    p_repo, p_cookie, p_client, admin = _resolver_env(routines, details)
    with p_repo, p_cookie, p_client:
        assert await mod._resolve_trigger_binding("u_1", PUBLIC_ID) is True
    assert mod._binding_cached("u_1", PUBLIC_ID)
    admin.get_routine.assert_awaited_once()  # cron-only routine not fetched


async def test_resolver_rejects_foreign_public_id():
    from routers import paperclip_trigger_proxy as mod

    routines = [{"id": "r_hook", "triggers": [{"kind": "webhook"}]}]
    details = {"r_hook": {"triggers": [{"kind": "webhook", "publicId": SIBLING_ID}]}}
    p_repo, p_cookie, p_client, _ = _resolver_env(routines, details)
    with p_repo, p_cookie, p_client:
        assert await mod._resolve_trigger_binding("u_1", PUBLIC_ID) is False
    assert not mod._binding_cached("u_1", PUBLIC_ID)  # misses are never cached


async def test_resolver_retries_once_on_admin_401():
    from core.services.paperclip_admin_client import PaperclipApiError
    from routers import paperclip_trigger_proxy as mod

    routines = [{"id": "r_hook", "triggers": [{"kind": "webhook"}]}]
    details = {"r_hook": {"triggers": [{"kind": "webhook", "publicId": PUBLIC_ID}]}}
    p_repo, p_cookie, p_client, admin = _resolver_env(routines, details)
    admin.list_routines = AsyncMock(side_effect=[PaperclipApiError("expired", status_code=401, body=""), routines])
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("routers.paperclip_trigger_proxy.invalidate_admin_session") as invalidate,
    ):
        assert await mod._resolve_trigger_binding("u_1", PUBLIC_ID) is True
    invalidate.assert_called_once()
    assert admin.list_routines.await_count == 2


async def test_resolver_returns_false_without_company_row():
    from routers import paperclip_trigger_proxy as mod

    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=None)
    with patch("routers.paperclip_trigger_proxy.PaperclipRepo", return_value=repo):
        assert await mod._resolve_trigger_binding("org_nobody", PUBLIC_ID) is False
    repo.get_workspace.assert_awaited_once_with("org_nobody")


async def test_resolver_returns_false_for_workspace_row_without_company():
    """A workspace row still seeding (no company_id yet) resolves to no company."""
    from routers import paperclip_trigger_proxy as mod

    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=MagicMock(company_id=""))
    with patch("routers.paperclip_trigger_proxy.PaperclipRepo", return_value=repo):
        assert await mod._resolve_trigger_binding("org_1", PUBLIC_ID) is False
    repo.get_workspace.assert_awaited_once_with("org_1")


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


def test_authless_forward_passes_raw_body_and_whitelisted_headers(client):
    """The signed request forwards byte-verbatim with ONLY the signing
    headers + Content-Type; everything else (cookies, spoofed XFF) is
    dropped, and X-Forwarded-For is re-asserted with the true caller IP."""
    captured: dict = {}
    raw_body = b'{"ref": "main",\n  "x":  1}'  # odd spacing — must survive untouched

    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}?source=github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer trigger-secret",
                "X-Paperclip-Signature": "sig-value",
                "X-Paperclip-Timestamp": "1723200000",
                "X-Hub-Signature-256": "sha256=deadbeef",
                "Idempotency-Key": "idem-1",
                "Cookie": "clerk=should-not-forward",
                "X-Forwarded-For": "1.2.3.4",  # spoof — must be overwritten
                "X-Custom-Junk": "nope",
            },
        )

    assert r.status_code == 202
    assert r.json() == {"status": "queued"}
    assert captured["url"] == f"{UPSTREAM}/api/routine-triggers/public/{PUBLIC_ID}/fire?source=github"
    assert captured["body"] == raw_body  # exact bytes — HMAC-load-bearing
    h = captured["headers"]
    assert h["authorization"] == "Bearer trigger-secret"
    assert h["x-paperclip-signature"] == "sig-value"
    assert h["x-paperclip-timestamp"] == "1723200000"
    assert h["x-hub-signature-256"] == "sha256=deadbeef"
    assert h["idempotency-key"] == "idem-1"
    assert h["content-type"] == "application/json"
    assert "cookie" not in h
    assert "x-custom-junk" not in h
    assert h["x-forwarded-for"] != "1.2.3.4"  # rewritten, not passed through


def test_upstream_status_passes_through_verbatim(client):
    """Paperclip's own signature rejection (401) must reach the sender."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured, status=401, content=b'{"error":"bad signature"}'),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 401
    assert r.json() == {"error": "bad signature"}


def test_upstream_unreachable_returns_502(client):
    class FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("boom")

    with (
        _mock_billing("active"),
        _mock_binding(True),
        patch(
            "routers.paperclip_trigger_proxy._client_factory",
            lambda: httpx.AsyncClient(transport=FailTransport()),
        ),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 502


def test_missing_paperclip_url_returns_503(client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    with _mock_billing("active"), _mock_binding(True):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Stripe signature-header normalization (native verification — Paperclip
# does the actual verifying; the proxy only shims Stripe's combined
# ``Stripe-Signature: t=,v1=`` header into Paperclip's split
# ``X-Paperclip-Timestamp`` / ``X-Paperclip-Signature`` headers, and derives
# an Idempotency-Key from the Stripe event id).
# ---------------------------------------------------------------------------


def test_normalize_stripe_signature_splits_timestamp_and_first_v1():
    from routers.paperclip_trigger_proxy import _normalize_stripe_signature

    assert _normalize_stripe_signature("t=123,v1=abc") == ("123", "abc")
    assert _normalize_stripe_signature("t=123,v1=abc,v1=def") == ("123", "abc")  # first v1 only
    assert _normalize_stripe_signature("v1=abc") == (None, "abc")  # missing t
    assert _normalize_stripe_signature("t=123") == ("123", None)  # missing v1
    assert _normalize_stripe_signature("") == (None, None)
    assert _normalize_stripe_signature("garbage") == (None, None)


def test_stripe_signature_header_splits_into_paperclip_headers(client):
    """A ``Stripe-Signature: t=,v1=`` header is split into the separate
    ``X-Paperclip-Timestamp`` / ``X-Paperclip-Signature`` headers Paperclip's
    native hmac_sha256 mode reads. No verification happens here — Paperclip
    verifies (or rejects) it upstream."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"{}",
            headers={"Content-Type": "application/json", "Stripe-Signature": "t=123,v1=deadbeef"},
        )
    assert r.status_code == 202
    h = captured["headers"]
    assert h["x-paperclip-timestamp"] == "123"
    assert h["x-paperclip-signature"] == "deadbeef"


def test_stripe_event_id_becomes_idempotency_key(client):
    """A fire carrying a Stripe-Signature header derives an Idempotency-Key
    from the body's event id, so Stripe's delivery retries (which reuse the
    ``evt_...`` id and send no idempotency header of their own) dedupe to a
    single routine run upstream."""
    captured: dict = {}
    body = b'{"id":"evt_abc"}'
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=body,
            headers={"Content-Type": "application/json", "Stripe-Signature": "t=123,v1=deadbeef"},
        )
    assert r.status_code == 202
    assert captured["headers"]["idempotency-key"] == "evt_abc"


def test_caller_idempotency_key_not_overridden_by_stripe_event_id(client):
    """A caller-supplied Idempotency-Key must not be clobbered by the
    derived Stripe event id — the derivation only fills the gap Stripe
    itself leaves."""
    captured: dict = {}
    body = b'{"id":"evt_abc"}'
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=123,v1=deadbeef",
                "Idempotency-Key": "caller-x",
            },
        )
    assert r.status_code == 202
    assert captured["headers"]["idempotency-key"] == "caller-x"


def test_no_stripe_signature_forwards_without_injected_headers(client):
    """No Stripe-Signature header at all (a non-Stripe trigger, e.g.
    github_hmac or an unsigned one) forwards with no injected
    ``X-Paperclip-*`` or Idempotency-Key headers."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={"id": "evt_should_not_matter"})
    assert r.status_code == 202
    h = captured["headers"]
    assert "x-paperclip-timestamp" not in h
    assert "x-paperclip-signature" not in h
    assert "idempotency-key" not in h


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def _prime_binding(*pairs: tuple[str, str]):
    """Seed the binding cache as if these (owner, public_id) pairs had
    already been verified."""
    import time as _time

    from routers import paperclip_trigger_proxy as mod

    for owner_id, public_id in pairs:
        mod._BINDING_CACHE[(owner_id, public_id)] = _time.monotonic()


def test_rate_limiter_returns_429_after_60_per_trigger(client):
    captured: dict = {}
    _prime_binding(("u_1", PUBLIC_ID))
    with (
        _mock_billing("active"),
        _stub_client(captured),
    ):
        for _ in range(60):
            client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_abused_trigger_does_not_starve_sibling_trigger(client):
    """Buckets are per PUBLIC_ID: exhausting one trigger's budget must not
    429 the owner's other triggers."""
    captured: dict = {}
    _prime_binding(("u_1", PUBLIC_ID), ("u_1", SIBLING_ID))
    with (
        _mock_billing("active"),
        _stub_client(captured),
    ):
        for _ in range(61):
            exhausted = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
        sibling = client.post(f"/api/v1/hooks/t/u_1/{SIBLING_ID}", json={})
    assert exhausted.status_code == 429
    assert sibling.status_code == 202


def test_unresolved_flood_hits_resolve_bucket_but_not_bound_triggers(client):
    """Garbage public_ids burn the per-owner RESOLVE bucket (bounding the
    Paperclip fan-out), while an already-verified trigger keeps flowing —
    the pre-auth rate-limit DoS from review is closed."""
    captured: dict = {}
    _prime_binding(("u_1", PUBLIC_ID))
    with (
        _mock_billing("active"),
        _mock_binding(False),
        _stub_client(captured),
    ):
        # Flood with unknown ids: 404s while the resolve bucket lasts...
        for i in range(60):
            r = client.post(f"/api/v1/hooks/t/u_1/{i:024x}", json={})
            assert r.status_code == 404
        # ...then 429 for further UNRESOLVED lookups only.
        r = client.post(f"/api/v1/hooks/t/u_1/{61:024x}", json={})
        assert r.status_code == 429
        # The bound trigger is untouched by the flood.
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
        assert r.status_code == 202


def test_trigger_buckets_are_independent_of_container_hook_bucket(client):
    """Trigger fires must not consume the owner's container-hook bucket
    (and vice versa) — keys are namespaced."""
    from routers import _public_ingress

    captured: dict = {}
    _prime_binding(("u_1", PUBLIC_ID))
    with (
        _mock_billing("active"),
        _stub_client(captured),
    ):
        client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert len(_public_ingress._RATE._buckets[f"trigger:{PUBLIC_ID}"]) == 1
    assert "u_1" not in _public_ingress._RATE._buckets


# ---------------------------------------------------------------------------
# Registry-declared provider signature headers (Sentry / Linear / Attio)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_header",
    ["Sentry-Hook-Signature", "Linear-Signature", "Attio-Signature"],
)
def test_provider_signature_header_copied_to_paperclip_signature(client, provider_header):
    """Raw-body HMAC providers sign under their own header name; the proxy
    copies whichever registry-declared header a delivery carries into
    ``X-Paperclip-Signature`` so Paperclip's github_hmac mode reads it. The
    provider's original header is not in the forward allowlist and is
    dropped."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b'{"action":"created"}',
            headers={"Content-Type": "application/json", provider_header: "cafef00d"},
        )
    assert r.status_code == 202
    h = captured["headers"]
    assert h["x-paperclip-signature"] == "cafef00d"
    assert provider_header.lower() not in h


def test_caller_paperclip_signature_not_clobbered_by_provider_header(client):
    """A caller that already sends X-Paperclip-Signature (a hand-wired
    sender using Paperclip's own convention) keeps its value even when a
    provider-named header is also present — the more specific source wins."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Paperclip-Signature": "caller-sig",
                "Linear-Signature": "provider-sig",
            },
        )
    assert r.status_code == 202
    assert captured["headers"]["x-paperclip-signature"] == "caller-sig"


def test_stripe_split_takes_precedence_over_provider_header(client):
    """Never expected in a real delivery, but the precedence is pinned:
    the Stripe t=,v1= split wins over a generic provider header copy."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=99,v1=stripesig",
                "Sentry-Hook-Signature": "sentrysig",
            },
        )
    assert r.status_code == 202
    h = captured["headers"]
    assert h["x-paperclip-signature"] == "stripesig"
    assert h["x-paperclip-timestamp"] == "99"


# ---------------------------------------------------------------------------
# Google push channels (auto_google_watch)
# ---------------------------------------------------------------------------


def test_goog_channel_token_maps_to_bearer_authorization(client):
    """Google push channels echo the token set at watch time (= the trigger's
    secret) in X-Goog-Channel-Token and send no Authorization — the proxy
    maps it so Paperclip's native bearer mode does the timing-safe compare.
    The raw X-Goog-Channel-Token itself is not in the forward allowlist."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"",
            headers={
                "X-Goog-Channel-Token": "pcsec_g1",
                "X-Goog-Resource-State": "exists",
                "X-Goog-Changed": "children",
            },
        )
    assert r.status_code == 202
    h = captured["headers"]
    assert h["authorization"] == "Bearer pcsec_g1"
    assert "x-goog-channel-token" not in h
    # The informational pair rides through — the notification has no body, so
    # these headers ARE the run context.
    assert h["x-goog-resource-state"] == "exists"
    assert h["x-goog-changed"] == "children"


def test_goog_channel_token_does_not_clobber_caller_authorization(client):
    """A caller that already sends Authorization (a hand-wired bearer sender)
    keeps its value even when a Google channel token is also present."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"",
            headers={
                "Authorization": "Bearer caller-secret",
                "X-Goog-Channel-Token": "pcsec_g1",
            },
        )
    assert r.status_code == 202
    assert captured["headers"]["authorization"] == "Bearer caller-secret"


def test_goog_sync_ping_returns_204_without_forwarding(client):
    """X-Goog-Resource-State: sync is Google's channel-confirmation ping,
    fired on every watch create/renew — forwarding it would start a spurious
    routine run each time, so it's acknowledged at the edge."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"",
            headers={
                "X-Goog-Channel-Token": "pcsec_g1",
                "X-Goog-Resource-State": "sync",
            },
        )
    assert r.status_code == 204
    assert captured == {}  # nothing reached upstream


def test_goog_sync_ping_still_behind_the_gates(client):
    """The sync short-circuit sits AFTER the entitlement gate — a trial owner
    probing with a sync header still gets the structured 402, not a 204."""
    with _mock_billing("trial_local"):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"",
            headers={"X-Goog-Resource-State": "sync"},
        )
    assert r.status_code == 402


# ---------------------------------------------------------------------------
# Gmail push OIDC (owner_setup / gmail.message)
#
# Pub/Sub push rotates its OIDC JWT per delivery, so the proxy verifies it
# (JWKS mocked to a test RSA key; the REAL decode path — signature, iss, aud,
# exp, email_verified — runs) and swaps in the trigger's own bearer secret
# from the owner's gmail_watch entry. A JWT-shaped Authorization that fails
# verification is 401'd at the edge; non-JWT values ride the existing
# pass-through (pinned by test_authless_forward_* above).
# ---------------------------------------------------------------------------

# One module-level key: 2048-bit generation is ~100ms, no need per-test.
_OIDC_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _oidc_audience(owner_id: str = "u_1", public_id: str = PUBLIC_ID) -> str:
    from core.config import settings

    return f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}"


def _google_jwt(**over) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": _oidc_audience(),
        "email": "push@my-project.iam.gserviceaccount.com",
        "email_verified": True,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    claims.update(over)
    return jwt.encode(claims, _OIDC_KEY, algorithm="RS256")


def _mock_google_jwks():
    """Serve the test key as whatever kid the JWT names — the decode itself
    still enforces signature/claims against it."""
    from routers import paperclip_trigger_proxy as mod

    return patch.object(
        mod._google_jwks_client,
        "get_signing_key_from_jwt",
        return_value=MagicMock(key=_OIDC_KEY.public_key()),
    )


def _mock_gmail_entries(entries: list[dict]):
    return patch(
        "routers.paperclip_trigger_proxy.integrations_repo.get_google_watch_channels",
        AsyncMock(return_value=entries),
    )


_GMAIL_ENTRY = {
    "kind": "gmail_watch",
    "account_email": "ada@example.com",
    "trigger_public_id": PUBLIC_ID,
    "channel_token": "pcsec_gmail1",
}

_PUBSUB_BODY = json.dumps(
    {
        "message": {
            "data": "eyJlbWFpbEFkZHJlc3MiOiJhZGFAZXhhbXBsZS5jb20iLCJoaXN0b3J5SWQiOjk5fQ==",
            "messageId": "pubsub-msg-42",
        },
        "subscription": "projects/my-project/subscriptions/isol8-gmail",
    }
).encode()


def test_valid_oidc_jwt_swaps_in_entry_bearer_and_derives_idempotency_key(client):
    """The happy path: a Google-signed JWT for this exact proxy URL is
    verified, REPLACED with the gmail_watch entry's channel_token (so
    Paperclip's bearer mode does the final compare), and the Pub/Sub
    messageId becomes the Idempotency-Key (delivery retries reuse it)."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        _mock_gmail_entries([_GMAIL_ENTRY]),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=_PUBSUB_BODY,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_google_jwt()}",
            },
        )
    assert r.status_code == 202
    h = captured["headers"]
    assert h["authorization"] == "Bearer pcsec_gmail1"  # JWT never forwarded
    assert h["idempotency-key"] == "pubsub-msg-42"
    assert captured["body"] == _PUBSUB_BODY  # byte-verbatim, as ever


def test_oidc_jwt_with_wrong_audience_is_rejected_401(client):
    """aud must be THIS trigger's exact proxy URL — a JWT minted for another
    trigger (or any other audience) is refused, nothing forwarded."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        _mock_gmail_entries([_GMAIL_ENTRY]),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=_PUBSUB_BODY,
            headers={"Authorization": f"Bearer {_google_jwt(aud=_oidc_audience(public_id=SIBLING_ID))}"},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid_oidc_token"}
    assert captured == {}


@pytest.mark.parametrize(
    "bad_claims",
    [
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 60},
        {"email_verified": False},
    ],
)
def test_oidc_jwt_with_bad_claims_is_rejected_401(client, bad_claims):
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        _mock_gmail_entries([_GMAIL_ENTRY]),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=_PUBSUB_BODY,
            headers={"Authorization": f"Bearer {_google_jwt(**bad_claims)}"},
        )
    assert r.status_code == 401
    assert captured == {}


def test_garbage_jwt_shaped_authorization_is_rejected_401(client):
    """Three dot-separated segments that aren't a real JWT: the shape check
    routes it to verification, which fails — never forwarded as a bearer."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b"{}",
            headers={"Authorization": "Bearer aaa.bbb.ccc"},
        )
    assert r.status_code == 401
    assert captured == {}


def test_verified_oidc_without_gmail_entry_fails_closed_401(client):
    """A verified Google delivery for a trigger with no recorded gmail_watch
    entry has nothing to authenticate to Paperclip with — 401, not a forward
    of the raw JWT."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        _mock_gmail_entries([]),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=_PUBSUB_BODY,
            headers={"Authorization": f"Bearer {_google_jwt()}"},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "unknown_gmail_watch"}
    assert captured == {}


def test_oidc_caller_idempotency_key_not_overridden(client):
    """A caller-supplied Idempotency-Key wins over the derived messageId —
    same gap-filling posture as the Stripe event-id derivation."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        _mock_gmail_entries([_GMAIL_ENTRY]),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=_PUBSUB_BODY,
            headers={
                "Authorization": f"Bearer {_google_jwt()}",
                "Idempotency-Key": "caller-key",
            },
        )
    assert r.status_code == 202
    assert captured["headers"]["idempotency-key"] == "caller-key"


def test_gmail_entry_token_is_cached_across_deliveries(client):
    """The entry lookup rides an in-process TTL cache (same idiom as the
    binding cache) — a second delivery must not re-read the row."""
    captured: dict = {}
    entries = AsyncMock(return_value=[_GMAIL_ENTRY])
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        _mock_google_jwks(),
        patch("routers.paperclip_trigger_proxy.integrations_repo.get_google_watch_channels", entries),
    ):
        for _ in range(2):
            r = client.post(
                f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
                content=_PUBSUB_BODY,
                headers={"Authorization": f"Bearer {_google_jwt()}"},
            )
            assert r.status_code == 202
    assert entries.await_count == 1


def test_github_ping_acknowledged_at_edge_not_forwarded(client):
    """GitHub fires one `ping` delivery when a hook is created, signed with
    the hook's secret like a real event — without this short-circuit it
    starts a spurious run the moment auto-registration creates the hook
    (observed live). Any 2xx marks the hook healthy on GitHub's side."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b'{"zen":"Design for failure.","hook_id":1}',
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
    assert r.status_code == 204
    assert "url" not in captured  # never forwarded upstream


def test_github_issues_event_still_forwarded(client):
    """A real GitHub event (X-GitHub-Event: issues) is unaffected by the
    ping short-circuit."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=b'{"action":"opened"}',
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
    assert r.status_code == 202
    assert "url" in captured


@pytest.fixture(autouse=True)
def _no_hook_row(monkeypatch):
    """These pin the Paperclip fallback: a trigger with no `HOOK#` row (every workspace is on the super-agent path)."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.repositories import automations_repo

    monkeypatch.setattr(automations_repo, "get_hook", _AsyncMock(return_value=None))
