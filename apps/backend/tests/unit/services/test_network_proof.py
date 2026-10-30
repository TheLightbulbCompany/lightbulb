"""The app pass: the same issuer key as computer proofs, but never mistaken for one."""

import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import network_proof  # noqa: E402


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "proof-key")


def _pass(now=1_000):
    return network_proof.mint_app_pass(
        "user_1", name="Ada", email="ada@example.com", ws="org_a", room="room_1", app="crew", now=now
    )


def test_mint_app_pass_verifies_with_public_key_and_expires_in_900s():
    token = _pass()
    h, p, s = token.split(".")
    assert json.loads(network_proof._unb64(h)) == {
        "alg": "EdDSA",
        "kid": network_proof.kid(),
        "typ": "lightbulb-app-pass",
    }
    assert json.loads(network_proof._unb64(p)) == {
        "iat": 1_000,
        "exp": 1_900,
        "sub": "user_1",
        "name": "Ada",
        "email": "ada@example.com",
        "ws": "org_a",
        "room": "room_1",
        "app": "crew",
    }
    # The door's key: LIGHTBULB_ISSUER_KEY = {"kty":"OKP","crv":"Ed25519","x": public_key()["key"], "kid"}.
    x = network_proof.public_key()["key"]
    Ed25519PublicKey.from_public_bytes(network_proof._unb64(x)).verify(network_proof._unb64(s), f"{h}.{p}".encode())


def test_app_pass_is_never_a_computer_proof():
    token = _pass(now=None)
    for member_key in (None, "", "mk_1"):
        assert network_proof.verify(token, member_key) is None


def test_verify_refuses_any_token_typed_as_an_app_pass():
    """R61: even with a computer claim, an app-pass-typed token is never a sign-in proof."""
    import time

    now = int(time.time())
    claims = {"iat": now, "exp": now + 60, "sub": "user_1", "computer": "mk_1", "auth_time": now}
    assert network_proof.verify(network_proof._sign(claims), "mk_1") is not None
    assert network_proof.verify(network_proof._sign(claims, typ="lightbulb-app-pass"), "mk_1") is None
