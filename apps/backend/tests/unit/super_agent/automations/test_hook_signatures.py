"""Backend webhook signature checks, dedupe keys, event types (wave-2 lane A, Task 1)."""

import base64
import hashlib
import hmac
import json
import re
import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core.services import hook_signatures as hs

SECRET = "whsec_test"
BODY = b'{"id":"evt_1","type":"invoice.payment_failed"}'
NOW = 1_800_000_000


def _stripe(secret: str = SECRET, t: int = NOW, body: bytes = BODY) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def _hex(algo, body: bytes = BODY, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, algo).hexdigest()


# --- hmac_sha256 (Stripe's {t}.{body}) ---------------------------------------


def test_stripe_valid():
    h = {"Stripe-Signature": f"t={NOW},v1={_stripe()}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) is None


def test_stripe_any_v1_matches_during_rotation():
    h = {"Stripe-Signature": f"t={NOW},v0=x,v1=bad,v1={_stripe()}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) is None


def test_stripe_stale_timestamp_rejected():
    h = {"Stripe-Signature": f"t={NOW},v1={_stripe()}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW + 301) == "stale"
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW + 300) is None


def test_stripe_tampered_body_rejected():
    h = {"Stripe-Signature": f"t={NOW},v1={_stripe()}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY + b" ", now=NOW) == "bad_signature"


def test_paperclip_style_headers_and_ms_timestamp():
    ms = NOW * 1000
    sig = _stripe(t=ms)
    h = {"X-Paperclip-Timestamp": str(ms), "X-Paperclip-Signature": f"sha256={sig}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) is None
    h["X-Paperclip-Timestamp"] = "not-a-number"
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) == "stale"


def test_non_integer_timestamp_is_stale():
    huge = "1" + "0" * 400  # int() takes it; dividing or subtracting a float would overflow
    sig = hmac.new(SECRET.encode(), f"{huge}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert hs.verify("hmac_sha256", SECRET, {"Stripe-Signature": f"t={huge},v1={sig}"}, BODY, now=NOW) == "stale"
    h = {"webhook-id": "m", "webhook-timestamp": huge, "webhook-signature": "v1,AAAA"}
    assert hs.verify("standard_webhooks", "whsec_" + "QUFBQQ==", h, BODY, now=NOW) == "stale"
    for t in ("nan", "inf", "1.5", ""):
        h = {"X-Paperclip-Timestamp": t or " ", "X-Paperclip-Signature": _stripe()}
        assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) == "stale", t
    sig = _stripe(t="nan")  # even with a signature over "nan.{body}"
    h = {"Stripe-Signature": f"t=nan,v1={sig}"}
    assert hs.verify("hmac_sha256", SECRET, h, BODY, now=NOW) == "stale"


def test_stripe_header_without_v1_is_missing():
    assert hs.verify("hmac_sha256", SECRET, {"Stripe-Signature": f"t={NOW}"}, BODY, now=NOW) == "missing"


# --- raw-body HMAC -----------------------------------------------------------


def test_github_hmac_prefix_optional():
    sig = _hex(hashlib.sha256)
    for value in (f"sha256={sig}", sig):
        assert hs.verify("github_hmac", SECRET, {"X-Hub-Signature-256": value}, BODY) is None
    assert hs.verify("github_hmac", SECRET, {"X-Hub-Signature-256": "sha256=00"}, BODY) == "bad_signature"


@pytest.mark.parametrize("header", ["Sentry-Hook-Signature", "Linear-Signature", "Attio-Signature"])
def test_sentry_linear_attio_header(header):
    h = {header.lower(): _hex(hashlib.sha256)}
    assert hs.verify("github_hmac", SECRET, h, BODY, signature_header=header) is None
    # Their header is not read unless the registry names it.
    assert hs.verify("github_hmac", SECRET, h, BODY) == "missing"


def test_vercel_hmac_sha1():
    h = {"x-vercel-signature": _hex(hashlib.sha1)}
    assert hs.verify("hmac_sha1", SECRET, h, BODY, signature_header="x-vercel-signature") is None
    assert hs.verify("hmac_sha1", SECRET, {"x-vercel-signature": "nope"}, BODY) == "bad_signature"


# --- Standard Webhooks -------------------------------------------------------


def test_standard_webhooks():
    key = b"0123456789abcdef"
    secret = "whsec_" + base64.b64encode(key).decode()
    sig = base64.b64encode(hmac.new(key, f"msg_1.{NOW}.".encode() + BODY, hashlib.sha256).digest()).decode()
    h = {"webhook-id": "msg_1", "webhook-timestamp": str(NOW), "webhook-signature": f"v1,old v1,{sig}"}
    assert hs.verify("standard_webhooks", secret, h, BODY, now=NOW) is None
    assert hs.verify("standard_webhooks", secret, h, BODY, now=NOW + 301) == "stale"
    assert (
        hs.verify("standard_webhooks", secret, {**h, "webhook-signature": "v1,AAAA"}, BODY, now=NOW) == "bad_signature"
    )
    assert hs.verify("standard_webhooks", "whsec_!!!", h, BODY, now=NOW) == "bad_signature"
    assert hs.verify("standard_webhooks", secret, {"webhook-id": "x"}, BODY, now=NOW) == "missing"


# --- bearer / Google channel token -------------------------------------------


def test_bearer_timing_safe_and_length_mismatch():
    assert hs.verify("bearer", SECRET, {"Authorization": f"Bearer {SECRET}"}, b"") is None
    with patch.object(hs.hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
        assert hs.verify("bearer", SECRET, {"Authorization": "Bearer whsec"}, b"") == "bad_signature"
    assert cd.called
    assert hs.verify("bearer", SECRET, {"Authorization": f"Basic {SECRET}"}, b"") == "missing"


def test_goog_channel_token_as_bearer():
    assert hs.verify("bearer", SECRET, {"X-Goog-Channel-Token": SECRET}, b"") is None
    assert hs.verify("google_channel_token", SECRET, {"X-Goog-Channel-Token": SECRET}, b"") is None
    # google_channel_token mode does not fall back to Authorization.
    assert hs.verify("google_channel_token", SECRET, {"Authorization": f"Bearer {SECRET}"}, b"") == "missing"


def test_missing_header_is_missing():
    for mode in ("hmac_sha256", "github_hmac", "hmac_sha1", "standard_webhooks", "bearer"):
        assert hs.verify(mode, SECRET, {}, BODY, now=NOW) == "missing"


def test_not_armed_none_and_unknown_mode():
    assert hs.verify("github_hmac", "", {"X-Hub-Signature-256": "x"}, BODY) == "not_armed"
    assert hs.verify("none", "", {}, BODY) is None
    assert hs.verify("rot13", SECRET, {}, BODY) == "bad_signature"


# --- Gmail Pub/Sub OIDC ------------------------------------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
AUD = "https://api.example/api/v1/hooks/t/u_1/pub_1"


def _google_jwt(**over) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "email_verified": True,
        "exp": int(time.time()) + 3600,
    }
    claims.update(over)
    return jwt.encode(claims, _KEY, algorithm="RS256")


@pytest.fixture
def google_jwks():
    from routers import paperclip_trigger_proxy as mod

    with patch.object(
        mod._google_jwks_client, "get_signing_key_from_jwt", return_value=MagicMock(key=_KEY.public_key())
    ):
        yield


def test_gmail_oidc_valid(google_jwks):
    assert hs.verify_google_oidc(_google_jwt(), AUD) is True
    assert hs.verify_google_oidc(None, AUD) is False


def test_gmail_oidc_wrong_audience_false(google_jwks):
    assert hs.verify_google_oidc(_google_jwt(aud="https://evil/"), AUD) is False


def test_gmail_oidc_expired_false(google_jwks):
    assert hs.verify_google_oidc(_google_jwt(exp=int(time.time()) - 10), AUD) is False


# --- dedupe keys / event types -----------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_PUBSUB = json.dumps({"message": {"messageId": "1234567890", "data": "e30="}}).encode()


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.mark.parametrize(
    "provider,mode,headers,body,want",
    [
        ("stripe", "hmac_sha256", {}, BODY, "evt_1"),
        ("gmail", "bearer", {}, _PUBSUB, "1234567890"),
        # Standard Webhooks signs webhook-id, so it is trusted.
        ("other", "standard_webhooks", {"webhook-id": "msg_2"}, b"{}", "msg_2"),
        # Google channel pushes have no body; the token is the whole credential, so its headers are the key.
        ("google_calendar", "bearer", {"X-Goog-Channel-ID": "ch-1", "X-Goog-Message-Number": "7"}, b"", "ch-1:7"),
        (
            "google_calendar",
            "google_channel_token",
            {"X-Goog-Channel-ID": "c", "X-Goog-Message-Number": "8"},
            b"",
            "c:8",
        ),
        # Raw-body HMAC modes do not sign headers: delivery headers are ignored, the body is the key.
        ("github", "github_hmac", {"X-GitHub-Delivery": "d-1"}, b'{"a":1}', _h(b'{"a":1}')),
        ("linear", "github_hmac", {"Linear-Delivery": "d-1"}, b'{"a":1}', _h(b'{"a":1}')),
        ("vercel", "hmac_sha1", {"webhook-id": "w"}, b"{}", _h(b"{}")),
        ("other", "github_hmac", {"X-Goog-Channel-ID": "c", "X-Goog-Message-Number": "1"}, b"x", _h(b"x")),
        ("other", "bearer", {}, b"hello", _h(b"hello")),
        # Stripe body id only counts for Stripe.
        ("other", "hmac_sha256", {}, BODY, _h(BODY)),
        # A provider id outside the safe shape is hashed, not passed through.
        ("other", "standard_webhooks", {"webhook-id": "a/b"}, b"{}", _h(b"a/b")),
    ],
)
def test_dedupe_key_per_provider(provider, mode, headers, body, want):
    key = hs.dedupe_key(provider, headers, body, mode=mode)
    assert key == want
    assert _KEY_RE.match(key)


def test_unsigned_delivery_header_cannot_mint_a_new_key():
    """A captured GitHub delivery resent with a fresh X-GitHub-Delivery is still the same delivery."""
    body = b'{"ref":"refs/heads/main"}'
    first = hs.dedupe_key("github", {"X-GitHub-Delivery": "d-1"}, body, mode="github_hmac")
    replay = hs.dedupe_key("github", {"X-GitHub-Delivery": "d-2"}, body, mode="github_hmac")
    assert first == replay


def test_dedupe_key_same_across_stripe_retries():
    # Stripe re-signs a retry with a new t=; the event id is the key.
    assert hs.dedupe_key("stripe", {"Stripe-Signature": "t=1,v1=a"}, BODY, mode="hmac_sha256") == hs.dedupe_key(
        "stripe", {"Stripe-Signature": "t=2,v1=b"}, BODY, mode="hmac_sha256"
    )


@pytest.mark.parametrize(
    "provider,headers,body,want",
    [
        ("stripe", {}, BODY, "invoice.payment_failed"),
        # GitHub and Sentry name the event only in an unsigned header: not trusted (events are
        # subscribed at the provider instead), so a resent delivery cannot change its event type.
        ("github", {"X-GitHub-Event": "pull_request"}, b'{"action":"opened"}', None),
        ("vercel", {}, b'{"type":"deployment.error"}', "deployment.error"),
        ("linear", {}, b'{"type":"Issue","action":"create"}', "Issue"),
        ("sentry", {"Sentry-Hook-Resource": "issue"}, b"{}", None),
        ("stripe", {}, b"not json", None),
        ("stripe", {}, b"[1]", None),
        ("posthog", {}, b'{"type":"x"}', None),
    ],
)
def test_event_type_per_provider(provider, headers, body, want):
    assert hs.event_type(provider, headers, body) == want
