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


@pytest.mark.parametrize(
    "provider,headers,body,want",
    [
        ("stripe", {}, BODY, "evt_1"),
        (
            "github",
            {"X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958"},
            b"{}",
            "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        ),
        ("gmail", {}, _PUBSUB, "1234567890"),
        ("google_calendar", {"X-Goog-Channel-ID": "ch-1", "X-Goog-Message-Number": "7"}, b"", "ch-1:7"),
        ("linear", {"Linear-Delivery": "d-1"}, b"{}", "d-1"),
        ("other", {"webhook-id": "msg_2"}, b"{}", "msg_2"),
        ("other", {}, b"hello", hashlib.sha256(b"hello").hexdigest()),
        # Stripe body id only counts for Stripe.
        ("other", {}, BODY, hashlib.sha256(BODY).hexdigest()),
        # A provider id outside the safe shape is hashed, not passed through.
        ("other", {"webhook-id": "a/b"}, b"{}", hashlib.sha256(b"a/b").hexdigest()),
    ],
)
def test_dedupe_key_per_provider(provider, headers, body, want):
    key = hs.dedupe_key(provider, headers, body)
    assert key == want
    assert _KEY_RE.match(key)


def test_dedupe_key_same_across_stripe_retries():
    # Stripe re-signs a retry with a new t=; the event id is the key.
    assert hs.dedupe_key("stripe", {"Stripe-Signature": "t=1,v1=a"}, BODY) == hs.dedupe_key(
        "stripe", {"Stripe-Signature": "t=2,v1=b"}, BODY
    )


@pytest.mark.parametrize(
    "provider,headers,body,want",
    [
        ("stripe", {}, BODY, "invoice.payment_failed"),
        ("github", {"X-GitHub-Event": "pull_request"}, b'{"action":"opened"}', "pull_request"),
        ("vercel", {}, b'{"type":"deployment.error"}', "deployment.error"),
        ("linear", {}, b'{"type":"Issue","action":"create"}', "Issue"),
        ("sentry", {"Sentry-Hook-Resource": "issue"}, b"{}", "issue"),
        ("stripe", {}, b"not json", None),
        ("stripe", {}, b"[1]", None),
        ("posthog", {}, b'{"type":"x"}', None),
    ],
)
def test_event_type_per_provider(provider, headers, body, want):
    assert hs.event_type(provider, headers, body) == want
