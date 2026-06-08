"""Tests for the internal Bedrock meter endpoint.

This router does NOT use Depends(get_current_user); auth is a bearer
bedrock_meter service token (service_token.verify_meter). Mirrors the
hooks_proxy 401 template.
"""

import pytest
from fastapi.testclient import TestClient

from core.services import service_token
from main import app


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


@pytest.fixture
def client():
    return TestClient(app)


def _body(**overrides) -> dict:
    base = dict(
        request_id="req-1",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc",
        identity_arn="arn:aws:sts::877352799272:assumed-role/isol8-owner-org_abc/x",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        run_status="end",
        region="us-east-1",
        occurred_at="2026-06-07T12:00:00Z",
    )
    base.update(overrides)
    return base


def test_missing_authorization_returns_401(client):
    r = client.post("/api/v1/internal/usage/from-bedrock-log", json=_body())
    assert r.status_code == 401


def test_wrong_kind_token_returns_401(client):
    # A paperclip-kind token must be rejected on this surface.
    bad = service_token.mint("user_x")
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {bad}"},
        json=_body(),
    )
    assert r.status_code == 401


def test_garbage_token_returns_401(client):
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": "Bearer not-a-jwt"},
        json=_body(),
    )
    assert r.status_code == 401


def test_blank_request_id_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(request_id=""),
    )
    assert r.status_code == 422


def test_negative_tokens_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(input_tokens=-5),
    )
    assert r.status_code == 422
