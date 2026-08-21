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


def test_webrtc_iframe_is_the_only_frame_exception():
    """The Neko iframe may embed, but no neighboring browser-view route may."""
    r = client.get("/api/v1/browser-view/webrtc/", cookies={"isol8_browser_view": "bad", "neko": "bad"})
    assert r.headers.get("x-frame-options") is None
    assert r.headers.get("content-security-policy") == "frame-ancestors http://localhost:3000 http://localhost:5173"

    session = client.post("/api/v1/browser-view/session")
    assert session.headers.get("x-frame-options") == "SAMEORIGIN"


def test_browser_view_session_uses_the_global_cors_policy():
    """The Clerk bootstrap POST remains callable from the configured frontend."""
    response = client.options(
        "/api/v1/browser-view/session",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert response.headers.get("access-control-allow-credentials") == "true"
