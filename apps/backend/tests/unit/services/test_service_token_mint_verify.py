"""Tests for service_token mint/verify."""

import pytest
import jwt as pyjwt

from core.services import service_token


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-not-real")


def test_mint_then_verify_round_trips():
    token = service_token.mint("user_123")
    claims = service_token.verify(token)
    assert claims["sub"] == "user_123"
    assert claims["kind"] == "paperclip_service"
    assert "jti" in claims
    assert claims["iat"] < claims["exp"]


def test_mint_includes_default_ttl():
    token = service_token.mint("user_x")
    claims = service_token.verify(token)
    # ~1 year (allow some slack)
    assert claims["exp"] - claims["iat"] >= 360 * 86400
    assert claims["exp"] - claims["iat"] <= 366 * 86400


def test_mint_respects_custom_ttl():
    token = service_token.mint("user_y", ttl_days=30)
    claims = service_token.verify(token)
    assert claims["exp"] - claims["iat"] >= 29 * 86400
    assert claims["exp"] - claims["iat"] <= 31 * 86400


def test_verify_wrong_kind_rejected():
    bad = pyjwt.encode(
        {"sub": "user_x", "kind": "clerk_user"},
        "test-secret-not-real",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        service_token.verify(bad)


def test_verify_missing_sub_rejected():
    bad = pyjwt.encode(
        {"kind": "paperclip_service"},
        "test-secret-not-real",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        service_token.verify(bad)


def test_verify_expired_rejected():
    expired = pyjwt.encode(
        {"sub": "u", "kind": "paperclip_service", "exp": 1, "iat": 0},
        "test-secret-not-real",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        service_token.verify(expired)


def test_verify_wrong_signing_secret_rejected():
    other = pyjwt.encode(
        {"sub": "u", "kind": "paperclip_service"},
        "different-secret",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        service_token.verify(other)


def test_jti_is_unique_across_mints():
    a = service_token.verify(service_token.mint("u1"))
    b = service_token.verify(service_token.mint("u1"))
    assert a["jti"] != b["jti"]


def test_mint_raises_if_signing_key_unset(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_SERVICE_TOKEN_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PAPERCLIP_SERVICE_TOKEN_KEY"):
        service_token.mint("u")
