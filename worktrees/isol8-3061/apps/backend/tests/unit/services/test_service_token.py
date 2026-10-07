"""Tests for service_token: the paperclip_service kind (the only kind).

The WS authorizer trusts exactly kind="paperclip_service"; verify() must
reject any other kind and any malformed token.
"""

import jwt
import pytest

from core.services import service_token


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


def test_mint_round_trips():
    token = service_token.mint("user_abc")
    claims = service_token.verify(token)
    assert claims["kind"] == service_token.SERVICE_TOKEN_KIND
    assert claims["sub"] == "user_abc"


def test_verify_rejects_wrong_kind():
    """A token signed with the right key but a foreign kind claim must never
    pass — kind is the privilege boundary, not the signature alone."""
    import os

    payload = {"sub": "user_abc", "kind": "some_other_kind"}
    token = jwt.encode(payload, os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"], algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)


def test_verify_rejects_missing_sub():
    import os

    payload = {"kind": service_token.SERVICE_TOKEN_KIND}
    token = jwt.encode(payload, os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"], algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)


def test_verify_rejects_garbage():
    with pytest.raises(jwt.PyJWTError):
        service_token.verify("not-a-jwt")
