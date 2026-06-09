"""Tests for service_token: the paperclip kind AND the new bedrock_meter kind.

The meter kind MUST NOT be accepted by verify() (the paperclip path the WS
authorizer trusts), and a paperclip token MUST NOT be accepted by
verify_meter() — a leaked meter token must never authorize container access.
"""

import jwt
import pytest

from core.services import service_token


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


def test_mint_meter_round_trips():
    token = service_token.mint_meter()
    claims = service_token.verify_meter(token)
    assert claims["kind"] == service_token.METER_TOKEN_KIND


def test_meter_token_rejected_by_paperclip_verify():
    token = service_token.mint_meter()
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)


def test_paperclip_token_rejected_by_verify_meter():
    token = service_token.mint("user_abc")
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify_meter(token)


def test_verify_meter_rejects_garbage():
    with pytest.raises(jwt.PyJWTError):
        service_token.verify_meter("not-a-jwt")
