"""Tests for the public Paperclip routine-trigger ingress proxy.

Like hooks_proxy, this router deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have Clerk
tokens. The real auth is Paperclip's per-trigger signing, verified
upstream. The owner_id segment drives edge gating only, and is BOUND to
the trigger via the instance-admin binding resolver before any
entitlement it names is honoured.
"""

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

UPSTREAM = "http://paperclip.internal:3100"
PUBLIC_ID = "abc123def456abc123def456"
SIBLING_ID = "fedcba987654fedcba987654"


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Rate buckets live in hooks_proxy module state (shared helper), and
    the binding + Stripe-whsec caches (plus its TOCTOU generation counters)
    in the trigger router — all must start empty or tests pollute their
    neighbours."""
    from core.services.credentials.stripe_edge import _STRIPE_SECRET_CACHE_GENERATION
    from routers import hooks_proxy, paperclip_trigger_proxy

    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()
    paperclip_trigger_proxy._BINDING_CACHE.clear()
    paperclip_trigger_proxy._STRIPE_SECRET_CACHE.clear()
    _STRIPE_SECRET_CACHE_GENERATION.clear()
    yield
    hooks_proxy._RATE_BUCKETS_PER_OWNER.clear()
    paperclip_trigger_proxy._BINDING_CACHE.clear()
    paperclip_trigger_proxy._STRIPE_SECRET_CACHE.clear()
    _STRIPE_SECRET_CACHE_GENERATION.clear()


@pytest.fixture(autouse=True)
def _paperclip_url(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", UPSTREAM)


@pytest.fixture(autouse=True)
def _no_stripe_whsec_by_default(monkeypatch):
    """Every trigger's per-(owner, public_id) Stripe whsec_ lookup returns
    "not stored" unless a test overrides it with ``_mock_stripe_secret``.
    This is what makes every OTHER test in this file (forwarding, rate
    limits, audit, resolver) represent an ordinary trigger — including a
    deliberately-unsigned Phase 1 ``none`` one — with ZERO special-casing
    per test, and it's the fix for the false-positive-401 regression: a
    trigger only gets Stripe-verified once a secret has actually been
    stored (T3/T6), never off signingMode alone. Also keeps every test
    off real boto3 (Secrets Manager) calls."""
    monkeypatch.setattr(
        "routers.paperclip_trigger_proxy.integration_secret_store.get",
        AsyncMock(return_value=None),
    )


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
# Stripe edge signature verification (known-vector)
# ---------------------------------------------------------------------------


def test_verify_stripe_signature_valid_and_tampered():
    from routers.paperclip_trigger_proxy import verify_stripe_signature

    secret = "whsec_test"
    body = b'{"id":"evt_1"}'
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    hdr = f"t={t},v1={sig}"
    assert verify_stripe_signature(secret, body, hdr, now=t) is True
    assert verify_stripe_signature(secret, b'{"id":"evt_2"}', hdr, now=t) is False  # tampered body
    assert verify_stripe_signature(secret, body, hdr, now=t + 400) is False  # stale
    assert verify_stripe_signature("whsec_wrong", body, hdr, now=t) is False  # wrong secret


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
    hooks_proxy traversal test)."""
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
    repo.get = AsyncMock(return_value=MagicMock(company_id="c_1", status="active"))
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
    repo.get = AsyncMock(return_value=None)
    with patch("routers.paperclip_trigger_proxy.PaperclipRepo", return_value=repo):
        assert await mod._resolve_trigger_binding("u_nobody", PUBLIC_ID) is False


async def test_resolver_uses_org_lookup_for_org_owner():
    from routers import paperclip_trigger_proxy as mod

    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value=None)
    with patch("routers.paperclip_trigger_proxy.PaperclipRepo", return_value=repo):
        assert await mod._resolve_trigger_binding("org_1", PUBLIC_ID) is False
    repo.get_org_company_id.assert_awaited_once_with("org_1")


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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
# Stripe edge verification (proxy integration)
#
# Discrimination is by STORED PER-TRIGGER WHSEC PRESENCE ONLY — never by
# Paperclip's signingMode, which is also Phase 1's own "None (unsigned)"
# user-selectable option (webhookSigning.ts SIGNING_MODE_OPTIONS) and would
# otherwise 401 every legitimate unsigned trigger fire.
# ---------------------------------------------------------------------------


def _mock_stripe_secret(secret: str | None):
    """Mock the per-trigger whsec_ lookup. ``secret=None`` == "nothing
    stored for this trigger" — an ordinary trigger, verified normally (or
    not at all, if it's a plain unsigned one)."""
    cred = {"webhook_secret": secret} if secret else None
    return patch(
        "routers.paperclip_trigger_proxy.integration_secret_store.get",
        AsyncMock(return_value=cred),
    )


def _stripe_header(secret: str, body: bytes, t: int) -> str:
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


_STRIPE_SECRET = "whsec_test"


def test_no_stored_whsec_forwards_without_demanding_stripe_signature(client):
    """THE regression test: a trigger with NO stored per-trigger whsec_ —
    which includes every plain Phase 1 ``none`` (unsigned) trigger, not
    just an unconnected Stripe one — forwards untouched with no
    Stripe-Signature header at all. Before the T2-review fix this 401'd
    every such trigger forever."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(None),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={"ref": "main"})
    assert r.status_code == 202
    assert captured != {}  # actually reached upstream


def test_stripe_edge_valid_signature_forwards(client):
    """A trigger WITH a stored whsec_ and a correctly-signed
    Stripe-Signature forwards verbatim, same as any other verified
    trigger."""
    captured: dict = {}
    body = b'{"id":"evt_1","type":"invoice.payment_failed"}'
    t = int(time.time())
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(_STRIPE_SECRET),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _stripe_header(_STRIPE_SECRET, body, t),
            },
        )
    assert r.status_code == 202
    assert captured["body"] == body  # forwarded byte-verbatim


def test_stripe_edge_tampered_body_returns_401(client):
    """A trigger with a stored whsec_: signature computed over a
    different body than the one that actually arrives — must be
    rejected, nothing forwarded."""
    captured: dict = {}
    signed_body = b'{"id":"evt_1"}'
    tampered_body = b'{"id":"evt_1","amount":999999}'
    t = int(time.time())
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(_STRIPE_SECRET),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=tampered_body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _stripe_header(_STRIPE_SECRET, signed_body, t),
            },
        )
    assert r.status_code == 401
    assert captured == {}


def test_stripe_edge_stale_timestamp_returns_401(client):
    """A trigger with a stored whsec_: a signature outside the replay
    tolerance is rejected even if the HMAC itself is correct for that
    (old) timestamp."""
    captured: dict = {}
    body = b'{"id":"evt_1"}'
    stale_t = int(time.time()) - 400
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(_STRIPE_SECRET),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _stripe_header(_STRIPE_SECRET, body, stale_t),
            },
        )
    assert r.status_code == 401
    assert captured == {}


def test_stripe_edge_missing_header_returns_401(client):
    """A trigger with a stored whsec_ — an attacker cannot skip
    verification by simply omitting the Stripe-Signature header; a
    stored secret always demands one."""
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(_STRIPE_SECRET),
        _stub_client(captured),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 401
    assert captured == {}


def test_stripe_edge_wrong_secret_returns_401(client):
    """An attacker cannot forge a signature by guessing/omitting the
    secret — a signature computed with the wrong whsec_ is rejected even
    though it's structurally well-formed."""
    captured: dict = {}
    body = b'{"id":"evt_1"}'
    t = int(time.time())
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _mock_stripe_secret(_STRIPE_SECRET),
        _stub_client(captured),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _stripe_header("whsec_attacker_guess", body, t),
            },
        )
    assert r.status_code == 401
    assert captured == {}


# ---------------------------------------------------------------------------
# Stripe whsec lookup: caching + store failures
# ---------------------------------------------------------------------------


def test_stripe_secret_lookup_is_cached_across_fires(client):
    """A stored whsec_ is fetched from Secrets Manager at most once per
    TTL — repeat fires of the same trigger reuse the cached value rather
    than making a live GetSecretValue call every time."""
    captured: dict = {}
    body = b'{"id":"evt_1"}'
    header = _stripe_header(_STRIPE_SECRET, body, int(time.time()))
    get_mock = AsyncMock(return_value={"webhook_secret": _STRIPE_SECRET})
    with (
        _mock_billing("active"),
        _mock_binding(True),
        patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        for _ in range(3):
            r = client.post(
                f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
                content=body,
                headers={"Content-Type": "application/json", "Stripe-Signature": header},
            )
            assert r.status_code == 202
    assert get_mock.await_count == 1


def test_no_whsec_lookup_is_also_cached_across_fires(client):
    """The common case — an ordinary trigger with nothing stored — must be
    free after the first fire too, not just the Stripe-armed case."""
    captured: dict = {}
    get_mock = AsyncMock(return_value=None)
    with (
        _mock_billing("active"),
        _mock_binding(True),
        patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        for _ in range(3):
            r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
            assert r.status_code == 202
    assert get_mock.await_count == 1


async def test_negative_cache_expires_after_short_ttl_positive_persists_to_long_ttl():
    """Split-TTL: a cached ``None`` ("nothing stored") expires after the
    short 60s negative TTL and re-hits Secrets Manager, while a cached
    found-secret value is still honoured from the cache well past that
    same age — it only expires at the long 3600s positive TTL."""
    from core.services.credentials.stripe_edge import STRIPE_SECRET_CACHE
    from routers.paperclip_trigger_proxy import _stripe_trigger_secret

    owner_id, public_id = "u_1", PUBLIC_ID
    stale_but_within_positive_ttl = time.monotonic() - 120  # > 60s, < 3600s

    # A cached negative older than the 60s negative TTL is treated as
    # expired — the store is re-read.
    STRIPE_SECRET_CACHE[(owner_id, public_id)] = (stale_but_within_positive_ttl, None)
    get_mock = AsyncMock(return_value=None)
    with patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock):
        result = await _stripe_trigger_secret(owner_id, public_id)
    assert result is None
    assert get_mock.await_count == 1  # cache miss — re-fetched

    # A cached positive of the same age is still within its (much longer)
    # positive TTL and is served straight from the cache.
    STRIPE_SECRET_CACHE[(owner_id, public_id)] = (stale_but_within_positive_ttl, "whsec_cached")
    get_mock2 = AsyncMock(return_value={"webhook_secret": "whsec_should_not_be_read"})
    with patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock2):
        result = await _stripe_trigger_secret(owner_id, public_id)
    assert result == "whsec_cached"
    get_mock2.assert_not_awaited()  # cache hit — no re-fetch


async def test_toctou_write_during_read_does_not_recache_stale_value():
    """P1 regression: a cache-miss read's Secrets Manager call can start
    BEFORE a concurrent PUT /webhook-secret's store()+invalidate() and
    complete AFTER it. Without the generation guard, the read's own
    now-stale result (what it saw before the write landed) would
    unconditionally overwrite the cache, silently restoring the exact
    "nothing stored" verdict the invalidate just cleared — reopening the
    fail-open window the invalidate exists to close."""
    from core.services.credentials.stripe_edge import STRIPE_SECRET_CACHE, invalidate_stripe_secret_cache
    from routers.paperclip_trigger_proxy import _stripe_trigger_secret

    owner_id, public_id = "u_1", PUBLIC_ID

    async def _stale_get(*args, **kwargs):
        # Simulate the concurrent write racing ahead of this read: a store()
        # + invalidate() for the SAME key lands while this read's own
        # Secrets Manager call is still in flight.
        invalidate_stripe_secret_cache(owner_id, public_id)
        return None  # what THIS read saw before the write — now stale

    with patch("routers.paperclip_trigger_proxy.integration_secret_store.get", AsyncMock(side_effect=_stale_get)):
        result = await _stripe_trigger_secret(owner_id, public_id)

    # The in-flight caller still gets its own (stale) read result...
    assert result is None
    # ...but it must never have cached it — the invalidate wins, and the
    # NEXT fire re-fetches for real instead of reusing this stale verdict.
    assert (owner_id, public_id) not in STRIPE_SECRET_CACHE


async def test_toctou_write_before_read_starts_is_cached_normally():
    """Sanity check the guard isn't overzealous: an ordinary read with no
    concurrent write in flight still caches its result as usual."""
    from core.services.credentials.stripe_edge import STRIPE_SECRET_CACHE
    from routers.paperclip_trigger_proxy import _stripe_trigger_secret

    owner_id, public_id = "u_1", PUBLIC_ID
    get_mock = AsyncMock(return_value={"webhook_secret": "whsec_fresh"})
    with patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock):
        result = await _stripe_trigger_secret(owner_id, public_id)
    assert result == "whsec_fresh"
    assert STRIPE_SECRET_CACHE[(owner_id, public_id)][1] == "whsec_fresh"


def test_stripe_secret_store_error_returns_503_not_500_and_is_not_cached(client):
    """A genuine Secrets Manager failure (throttle/network/IAM — anything
    other than "definitively no secret", which integration_secret_store
    already collapses to None) must never 500 and must never silently
    treat the trigger as unsigned: it 503s, nothing is forwarded, and the
    failure is NOT cached — a second fire retries against the store
    rather than reusing a stale error."""
    captured: dict = {}
    get_mock = AsyncMock(side_effect=RuntimeError("Secrets Manager throttled"))
    with (
        _mock_billing("active"),
        _mock_binding(True),
        patch("routers.paperclip_trigger_proxy.integration_secret_store.get", get_mock),
        _stub_client(captured),
    ):
        r1 = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
        r2 = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r1.status_code == 503
    assert r1.json() == {"detail": "stripe_verification_unavailable"}
    assert r2.status_code == 503
    assert captured == {}  # nothing ever reached upstream
    assert get_mock.await_count == 2  # not cached — retried on the second fire


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_row_written_with_trigger_kind(client):
    captured: dict = {}
    audit_writes = []
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        patch(
            "routers.paperclip_trigger_proxy.hook_event_repo.write_event",
            side_effect=lambda **kw: audit_writes.append(kw),
        ),
    ):
        r = client.post(
            f"/api/v1/hooks/t/u_1/{PUBLIC_ID}",
            json={},
            headers={"User-Agent": "GitHub-Hookshot/1.0"},
        )
    assert r.status_code == 202
    assert len(audit_writes) == 1
    assert audit_writes[0]["kind"] == "paperclip_trigger"
    assert audit_writes[0]["hook_path"] == PUBLIC_ID
    assert audit_writes[0]["status_code"] == 202
    assert audit_writes[0]["user_agent"] == "GitHub-Hookshot/1.0"


def test_audit_write_failure_does_not_drop_proxied_call(client):
    captured: dict = {}
    with (
        _mock_billing("active"),
        _mock_binding(True),
        _stub_client(captured),
        patch(
            "routers.paperclip_trigger_proxy.hook_event_repo.write_event",
            side_effect=Exception("DDB down"),
        ),
    ):
        r = client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert r.status_code == 202


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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
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
    from routers import hooks_proxy

    captured: dict = {}
    _prime_binding(("u_1", PUBLIC_ID))
    with (
        _mock_billing("active"),
        _stub_client(captured),
        patch("routers.paperclip_trigger_proxy.hook_event_repo.write_event"),
    ):
        client.post(f"/api/v1/hooks/t/u_1/{PUBLIC_ID}", json={})
    assert len(hooks_proxy._RATE_BUCKETS_PER_OWNER[f"trigger:{PUBLIC_ID}"]) == 1
    assert "u_1" not in hooks_proxy._RATE_BUCKETS_PER_OWNER
