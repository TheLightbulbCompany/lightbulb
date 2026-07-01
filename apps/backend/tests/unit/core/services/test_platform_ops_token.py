import os
import jwt
import pytest

# Ensure a dedicated key is present BEFORE importing the module under test.
os.environ.setdefault("PLATFORM_OPS_TOKEN_KEY", "test-ops-key-abc")
os.environ.setdefault("PAPERCLIP_SERVICE_TOKEN_KEY", "test-paperclip-key-xyz")

from core.services import platform_ops_token
from core.services import service_token


def test_mint_and_verify_roundtrip():
    tok = platform_ops_token.mint("org_founder")
    claims = platform_ops_token.verify(tok, expected_owner_id="org_founder")
    assert claims["kind"] == "platform_ops"
    assert claims["sub"] == "org_founder"
    assert "jti" in claims


def test_verify_rejects_wrong_owner():
    tok = platform_ops_token.mint("org_founder")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(tok, expected_owner_id="org_someone_else")


def test_verify_rejects_paperclip_service_token():
    # THE hole this closes: a tenant's paperclip_service token must NOT be
    # accepted by the ops verifier, even for the founder's owner_id, because
    # it is signed with a different key AND carries the wrong kind.
    tenant_tok = service_token.mint("org_founder")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(tenant_tok, expected_owner_id="org_founder")


def test_verify_rejects_wrong_kind_same_key():
    # A token signed with the ops key but the wrong kind is rejected.
    import datetime as _dt

    payload = {
        "sub": "org_founder",
        "kind": "not_platform_ops",
        "iat": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
        "exp": int((_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)).timestamp()),
    }
    bad = jwt.encode(payload, os.environ["PLATFORM_OPS_TOKEN_KEY"], algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        platform_ops_token.verify(bad, expected_owner_id="org_founder")
