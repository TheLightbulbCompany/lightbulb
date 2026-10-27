"""OpenAI's plugin portal proves we own the MCP host by fetching its token, as plain text, at
/.well-known/openai-apps-challenge. No token configured: the path is not there."""

from fastapi.testclient import TestClient

from core.config import settings
from main import app

client = TestClient(app)


def test_serves_the_exact_token_as_plain_text(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_APPS_CHALLENGE_TOKEN", "tok_abc123")
    r = client.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200
    assert r.text == "tok_abc123"
    assert r.headers["content-type"].startswith("text/plain")


def test_no_token_is_a_404(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_APPS_CHALLENGE_TOKEN", "")
    assert client.get("/.well-known/openai-apps-challenge").status_code == 404
