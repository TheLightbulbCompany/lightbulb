"""Baseline security response headers (SecurityHeadersMiddleware in main.py).

Covers the backend-served surfaces (JSON API + the Paperclip SPA proxy). The
user-facing app's headers live in the Next.js frontend config, not here.
"""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_response_carries_security_headers():
    r = client.get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "max-age=" in (r.headers.get("strict-transport-security") or "")


def test_headers_present_on_a_404_too():
    """The middleware wraps outermost, so even non-matched routes get the headers."""
    r = client.get("/definitely-not-a-route")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("strict-transport-security")
